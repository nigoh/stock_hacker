#!/usr/bin/env python3
"""ポートフォリオ評価レポートを生成する CLI。

使い方（リポジトリルートから）:
    python3 analysis/portfolio_review.py [--file data/portfolio.csv] [--period 1y]
        [--in-currency USD|EUR|GBP] [--drift-band 5.0] [--synthetic]

保有銘柄 CSV（列: code,shares,avg_cost,acquired_date,memo,fx_at_cost,account,
target_weight,manual_price,proxy_ticker。memo・fx_at_cost・account・target_weight・
manual_price・proxy_ticker は省略可）を読み込み、評価額・損益・セクター配分・加重β・
相関・年率ボラ・VaR・HHI 集中度・下落ストレス感応度（β近似）をまとめた
reports/portfolio-<日付>.md を生成し、そのパスを stdout に出力する。

任意列 account（nisa_tsumitate / nisa_growth / taxable。空欄・列なしは taxable 扱い）
を持つ CSV では、レポートに「NISA口座状況」節を追加する: 口座区分別の評価額・損益、
NISA枠の使用状況（年間投資枠 つみたて120万・成長240万、生涯投資枠1,800万うち成長
1,200万に対する簿価ベース使用率、2024年制度）、非課税メリットの推計
（NISA口座の含み益 × 20.315%、2025年時点税率）。account 列の無い既存 CSV は
従来どおり動作する（節ごと省略）。

任意列 target_weight（目標ウエイト%。全行入力・合計ほぼ100%が必要）を持つ CSV では、
レポートに「目標配分とのドリフト」節を追加する: 銘柄別/セクター別の現状ウエイトと
目標の乖離（%pt）、目標に戻すための機械的な調整額試算（円）、閾値バンド
（--drift-band、既定 ±5%pt）による超過/圏内判定、リバランスの摩擦（課税口座の
売却課税 20.315%〔2025年時点〕、NISA 年間枠は当年復活せず生涯枠は翌年復活
〔2024年制度〕）の注記。売買の推奨はしない（乖離の測定と機械的試算のみ）。

任意列 manual_price（手入力の現在値。投信の基準価額や現金 1 を想定）を持つ行は
yfinance を引かず手入力値で評価に組み入れる（現金・国内投信を含む全体資産ビュー。
yfinance は国内投信の基準価額を取得できない）。手入力行に限り code は4桁以外の
任意の識別子（emaxis-slim-allcountry、cash 等）を許容する。手入力行のうち任意列
proxy_ticker（連動対象とみなす上場プロキシ。例: 全世界株投信 → 2559.T、TOPIX投信 →
1306.T。指定はユーザーの判断）を持つ行は、プロキシの価格系列で β・年率ボラ・VaR・
相関・下落ストレス感応度に組み込む（評価額は従来どおり manual_price で計算。
信託報酬差・為替ヘッジ差・基準価額の1営業日ズレは反映されない近似である旨が
レポートに自動で注記される）。proxy_ticker 未指定の手入力行は従来どおり β・年率ボラ・
VaR・相関の計算対象外（リスク指標は対象外の行を除くウエイト再正規化で計算）で、
手入力値の取得日・鮮度の管理はユーザーの責任（レポートに自動で注記される）。

レポートには「下落ストレス感応度（β近似）」節が常に入る: ベンチマークが
-10%/-20%/-30% 下落した場合の推定損益・推定評価額を ΔV ≈ Σ MV_i・β_i・Δm で
機械的に試算する（β不明の手入力行は対象外＝変動ゼロ扱いの近似と明記。予測ではなく
β一定仮定の感応度試算であり、ストレス時はβ・相関が上昇しがちという限界も併記）。
また account=nisa_tsumitate に上場銘柄コードがある場合は「つみたて投資枠では個別株は
購入できない（2024年制度）——口座区分の入力ミスの可能性」という警告をレポートに出す
（エラーにはしない）。

--in-currency USD|EUR|GBP（海外投資家視点。--in-usd は --in-currency USD の後方互換
エイリアス）は基準通貨建て評価節を追加する。基準通貨建ての評価額とリスク指標
（年率ボラ・VaR）は全銘柄で計算する。損益の基準通貨建て換算は、任意列 fx_at_cost
（取得時のクロス円レート、円/基準通貨。指定した基準通貨のレートで入力すること）を
持つ銘柄に限り行い、恒等式 (1+r_B) = (1+r_JPY)/(1+r_FX) に基づいて株価要因
（円建て損益 ÷ 直近為替）と為替要因（残差）に分解して併記する。fx_at_cost の無い
銘柄は損益を円建てのみとする——現在為替での損益換算は購入時からの為替損益を無視した
近似にしかならないため、近似で誤魔化さない。為替はクロス円レート（USDJPY=X・
EURJPY=X 等）の同日終値・ヘッジなしの近似（stocklib.currency を利用）。

保有情報は既定で data/portfolio.csv に置く（data/ は gitignore 対象のため git 管理外）。
テンプレート: analysis/templates/portfolio-example.csv
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from stocklib import currency, report
from stocklib.data import REPO_ROOT, DataFetchError
from stocklib.portfolio import (
    PortfolioReview,
    PortfolioValidationError,
    evaluate_portfolio,
    load_portfolio,
)

DEFAULT_PORTFOLIO_CSV: Path = REPO_ROOT / "data" / "portfolio.csv"
TEMPLATE_CSV: Path = REPO_ROOT / "analysis" / "templates" / "portfolio-example.csv"


def build_report(review: PortfolioReview, source: Path) -> str:
    """レポート本文（Markdown）を構築する。"""
    lines: list[str] = [report.report_header("ポートフォリオ評価レポート")]
    lines.append(f"- 保有情報: {source}（{len(review.positions)} 銘柄）")
    lines.append(f"- 価格期間: {review.period} / ベンチマーク: {review.benchmark}")
    if review.synthetic:
        lines.append(
            "- **データ: 合成データ（--synthetic）による手法デモであり、実データではありません**"
        )
    lines.append("")
    lines.append(review.to_markdown())
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="保有銘柄 CSV からポートフォリオ評価レポートを生成する")
    parser.add_argument(
        "--file", type=Path, default=None,
        help=f"ポートフォリオ CSV のパス（既定: {DEFAULT_PORTFOLIO_CSV}）",
    )
    parser.add_argument("--period", default="1y", help="価格取得期間（既定: 1y）")
    parser.add_argument("--benchmark", default="^N225", help="β計算のベンチマーク（既定: ^N225）")
    parser.add_argument(
        "--in-currency",
        type=str.upper,
        choices=sorted(currency.SUPPORTED_CURRENCIES),
        default=None,
        help="基準通貨建て評価節を追加する（海外投資家視点。評価額・年率ボラ・VaR を "
        "クロス円レート（例: EURJPY=X）の同日終値で換算。CSV の任意列 fx_at_cost"
        "（取得時のクロス円レート）がある銘柄は損益も基準通貨建てで併記し、"
        "株価要因と為替要因に分解。fx_at_cost の無い銘柄の損益は円建てのみ）",
    )
    parser.add_argument(
        "--in-usd",
        action="store_true",
        help="--in-currency USD のエイリアス（後方互換）",
    )
    parser.add_argument(
        "--drift-band",
        type=float,
        default=5.0,
        help="目標配分ドリフトの閾値バンド（%%pt、既定: 5.0。CSV に target_weight 列が"
        "ある場合のみ使用。バンド内の乖離は「圏内」、超えると「超過」と判定する）",
    )
    parser.add_argument("--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）")
    args = parser.parse_args(argv)
    in_currency: str | None = args.in_currency or ("USD" if args.in_usd else None)
    if args.drift_band <= 0:
        parser.error("--drift-band は正の数（%pt）を指定してください")

    path: Path = args.file if args.file is not None else DEFAULT_PORTFOLIO_CSV
    if not path.exists():
        if args.file is None:
            print(
                "data/portfolio.csv を作成してください。"
                f"テンプレート: analysis/templates/portfolio-example.csv（{TEMPLATE_CSV}）",
                file=sys.stderr,
            )
        else:
            print(
                f"エラー: ポートフォリオ CSV が見つかりません: {path}\n"
                "テンプレート: analysis/templates/portfolio-example.csv",
                file=sys.stderr,
            )
        return 1

    try:
        positions = load_portfolio(path)
        review = evaluate_portfolio(
            positions,
            period=args.period,
            benchmark=args.benchmark,
            synthetic=args.synthetic,
            in_currency=in_currency,
            drift_band=args.drift_band / 100.0,  # %pt → 割合
        )
        content = build_report(review, path)
    except (PortfolioValidationError, DataFetchError, ValueError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    filename = f"portfolio-{dt.date.today().isoformat()}.md"
    out = report.save_report(content, filename)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

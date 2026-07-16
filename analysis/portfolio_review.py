#!/usr/bin/env python3
"""ポートフォリオ評価レポートを生成する CLI。

使い方（リポジトリルートから）:
    python3 analysis/portfolio_review.py [--file data/portfolio.csv] [--period 1y]
        [--in-currency USD|EUR|GBP] [--synthetic]

保有銘柄 CSV（列: code,shares,avg_cost,acquired_date,memo,fx_at_cost。memo と
fx_at_cost は省略可）を読み込み、評価額・損益・セクター配分・加重β・相関・年率ボラ・
VaR・HHI 集中度をまとめた reports/portfolio-<日付>.md を生成し、そのパスを stdout に
出力する。

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
    parser.add_argument("--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）")
    args = parser.parse_args(argv)
    in_currency: str | None = args.in_currency or ("USD" if args.in_usd else None)

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

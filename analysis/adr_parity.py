#!/usr/bin/env python3
"""ADRパリティ・モニタ CLI（東証現地株 × 米国ADR × ドル円の乖離計算）。

使い方（リポジトリルートから）:
    python3 analysis/adr_parity.py 7203 [6758 ...]   # 指定銘柄のみ
    python3 analysis/adr_parity.py --all             # adr_map.csv の全銘柄
    python3 analysis/adr_parity.py --all --synthetic # 合成データ（ネットワーク不要）

理論ADR価格 = 東証終値 × ADR比率 ÷ ドル円、乖離% = ADR終値/理論値 − 1、
円換算ADR価格 = ADR終値 × ドル円 ÷ ADR比率（翌朝の寄り付き水準の目安）を
テーブルにまとめ、reports/adr-<日付>.md に保存する。
対応表は analysis/universe/adr_map.csv（ADR比率は2025年時点、要確認）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from stocklib import adr, report
from stocklib.currency import fetch_usdjpy
from stocklib.data import DataFetchError


def build_report(
    mappings: list[adr.AdrMapping], period: str, synthetic: bool
) -> tuple[str, int]:
    """パリティレポート本文（Markdown）を構築する。

    Returns:
        ``(レポート本文, 評価に成功した銘柄数)``。個別銘柄の取得失敗はスキップして
        続行し、レポートに失敗銘柄を明記する。
    """
    fx_df = fetch_usdjpy(period, synthetic=synthetic)

    rows: list[list[object]] = []
    failures: list[tuple[adr.AdrMapping, str]] = []
    for m in mappings:
        try:
            result, tse_date, adr_date, fx_date = adr.evaluate_mapping(
                m, period=period, synthetic=synthetic, fx_df=fx_df
            )
        except (DataFetchError, ValueError) as exc:
            failures.append((m, str(exc)))
            print(f"警告: {m.code}/{m.adr_ticker} の評価をスキップします: {exc}", file=sys.stderr)
            continue
        rows.append([
            m.code,
            m.adr_ticker,
            m.listing,
            report.fmt_num(m.ratio, 2),
            f"{report.fmt_num(result.tse_close)} ({tse_date})",
            f"{report.fmt_num(result.adr_close)} ({adr_date})",
            report.fmt_num(result.theoretical_adr_usd),
            report.fmt_pct(result.premium_pct),
            report.fmt_num(result.adr_implied_jpy),
        ])

    lines: list[str] = [report.report_header("ADRパリティ・モニタ（東証現地株 × 米国ADR）")]
    lines.append(f"- 期間指定: {period}（各系列の**直近終値**を使用）")
    if synthetic:
        lines.append(
            "- **データ: 合成データ（--synthetic）による手法デモであり実データではありません。"
            "乖離の数値に意味はなく、計算ロジックの検証用です。**"
        )
    else:
        lines.append(
            f"- データソース: yfinance（非公式API、調整後終値）、取得日: {dt.date.today().isoformat()}"
        )
    lines.append(
        "- ADR比率（1ADRあたり現地株数）: `analysis/universe/adr_map.csv`（2025年時点。"
        "株式分割・預託銀行の比率変更で変わりうるため要確認）"
    )
    lines.append("")

    lines.append("## 乖離テーブル")
    lines.append("")
    if rows:
        lines.append(report.markdown_table(
            [
                "コード", "ADR", "上場", "比率",
                "東証終値(円)", "ADR終値($)", "理論ADR($)", "乖離%", "円換算ADR(円)",
            ],
            rows,
        ))
    else:
        lines.append("（評価できた銘柄がありません）")
    lines.append("")
    lines.append(
        "- 理論ADR($) = 東証終値 × 比率 ÷ ドル円。乖離% = ADR終値 ÷ 理論ADR − 1"
        "（正 = ADRが東証終値換算より高い）。"
    )
    lines.append(
        f"- ドル円終値: {report.fmt_num(float(fx_df['Close'].dropna().iloc[-1]))}"
        f"（{fx_df['Close'].dropna().index[-1].date()}、USDJPY=X）"
    )
    lines.append("")

    if failures:
        lines.append("## 取得失敗（スキップ）")
        lines.append("")
        for m, msg in failures:
            lines.append(f"- {m.code}/{m.adr_ticker}: {msg}")
        lines.append("")

    lines.append("## 解釈上の注意")
    lines.append("")
    lines.append(
        "- **東証終値とNY終値は同一暦日ではない**（時差により最大1営業日ずれる）。"
        "東京の取引終了後にNYで付いたADR価格は「NY時間に更新された理論値」であり、"
        "ここに示す乖離の大半は裁定機会ではなく、米国時間の株価・為替の動きを反映した"
        "情報の先行にすぎない。円換算ADR(円) は翌朝の東証寄り付き水準の目安として読む"
        "（詳細: `knowledge/market-structure/foreign-investor-access-channels.md` のADR節）。"
    )
    lines.append(
        "- 実際の裁定には預託手数料・転換コスト・決済時差がかかるため、"
        "小さな乖離は経済的に意味を持たない。OTC銘柄（アンスポンサードADR）は"
        "流動性が薄く、終値の情報価値自体が低いことがある。"
    )
    lines.append(
        "- yfinance の調整後終値は配当・分割調整のタイミングが現地株とADRでずれることが"
        "あり、権利落ち日近辺の乖離はデータ品質を疑う"
        "（`knowledge/data-sources/data-apis-and-tools.md` 参照）。"
    )
    lines.append("")
    return "\n".join(lines), len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="東証現地株と米国ADRの乖離（ADRパリティ）を計算する"
    )
    parser.add_argument(
        "codes", nargs="*",
        help="東証の銘柄コード（例: 7203。adr_map.csv に登録済みの銘柄のみ）",
    )
    parser.add_argument("--all", action="store_true", help="adr_map.csv の全銘柄を評価する")
    parser.add_argument("--period", default="1mo", help="取得期間（既定: 1mo。直近終値のみ使用）")
    parser.add_argument("--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）")
    args = parser.parse_args(argv)

    if not args.all and not args.codes:
        parser.error("銘柄コードを指定するか --all を付けてください")

    try:
        all_mappings = adr.load_adr_map()
    except (OSError, ValueError) as exc:
        print(f"エラー: ADR対応表の読み込みに失敗しました: {exc}", file=sys.stderr)
        return 1

    if args.all:
        mappings = all_mappings
    else:
        by_code = {m.code: m for m in all_mappings}
        mappings = []
        for code in args.codes:
            if code not in by_code:
                print(
                    f"エラー: {code} は analysis/universe/adr_map.csv に未登録です"
                    f"（登録済み: {', '.join(sorted(by_code))}）",
                    file=sys.stderr,
                )
                return 1
            mappings.append(by_code[code])

    try:
        content, n_ok = build_report(mappings, args.period, args.synthetic)
    except (DataFetchError, ValueError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    if n_ok == 0:
        print("エラー: 評価できた銘柄がありません（--synthetic でロジック検証は可能）", file=sys.stderr)
        return 1

    print(content)
    path = report.save_report(content, f"adr-{dt.date.today().isoformat()}.md")
    print(f"レポート: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

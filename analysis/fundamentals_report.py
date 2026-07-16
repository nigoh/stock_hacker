#!/usr/bin/env python3
"""単一銘柄の業績推移（決算）分析レポートを生成する CLI。

使い方（リポジトリルートから）:
    python3 analysis/fundamentals_report.py 7203 [--years 5] [--synthetic]

reports/fundamentals-<code>-<日付>.md を生成し、そのパスを stdout に出力する。

- 業績数値は yfinance（income_stmt / balance_sheet / cashflow）を第一ソースとする。
- 環境変数 EDINET_API_KEY が設定されていれば、EDINET API v2 で直近の有価証券報告書・
  四半期/半期報告書の一覧（docID 付き）をレポートに付ける（原文確認の導線）。
- --synthetic は決定論的な合成業績で全機能を動かす（ネットワーク不要）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

import pandas as pd

from stocklib import fundamentals, report
from stocklib.data import DataFetchError, normalize_code
from stocklib.fundamentals import analyze_growth, fetch_financial_history, is_nan


def _history_section(history: pd.DataFrame) -> list[str]:
    lines = ["## 業績推移（単位: 百万円）", ""]
    rows = []
    for idx, row in history.iterrows():
        rows.append(
            [
                idx.strftime("%Y-%m"),
                *[
                    report.fmt_num(float(v) / 1e6, 0) if pd.notna(v) else "-"
                    for v in row[list(fundamentals.HISTORY_COLUMNS)]
                ],
            ]
        )
    lines.append(report.markdown_table(["期末", *fundamentals.HISTORY_COLUMNS], rows))
    lines.append("")
    return lines


def _growth_section(history: pd.DataFrame) -> list[str]:
    g = analyze_growth(history)
    lines = ["## 成長性・収益性", ""]
    lines.append(report.markdown_table(
        ["指標", "値"],
        [
            [f"売上高CAGR（{g['years']}期）", report.fmt_pct(g["revenue_cagr"])],
            [f"営業利益CAGR（{g['years']}期）", report.fmt_pct(g["op_income_cagr"])],
            [f"純利益CAGR（{g['years']}期）", report.fmt_pct(g["net_income_cagr"])],
            ["連続増収", f"{g['revenue_streak']} 期"],
            ["連続営業増益", f"{g['op_income_streak']} 期"],
            ["連続純増益", f"{g['net_income_streak']} 期"],
        ],
    ))
    lines.append("")
    if any(is_nan(g[k]) for k in ("revenue_cagr", "op_income_cagr", "net_income_cagr")):
        lines.append("※ CAGR が `-` の項目は、始点・終点のいずれかが非正（赤字等）または期数不足のため計算していない。")
        lines.append("")

    lines.append("### マージン・ROE の推移")
    lines.append("")
    op_margin: pd.Series = g["op_margin"]  # type: ignore[assignment]
    net_margin: pd.Series = g["net_margin"]  # type: ignore[assignment]
    roe: pd.Series = g["roe"]  # type: ignore[assignment]
    rows = [
        [
            idx.strftime("%Y-%m"),
            report.fmt_pct(op_margin.loc[idx]) if pd.notna(op_margin.loc[idx]) else "-",
            report.fmt_pct(net_margin.loc[idx]) if pd.notna(net_margin.loc[idx]) else "-",
            report.fmt_pct(roe.loc[idx]) if pd.notna(roe.loc[idx]) else "-",
        ]
        for idx in history.index
    ]
    lines.append(report.markdown_table(["期末", "営業利益率", "純利益率", "ROE"], rows))
    lines.append("")
    lines.append(
        "※ 会社予想（ガイダンス）・コンセンサスとの比較は本レポートのデータには含まれない"
        "（`knowledge/fundamental/earnings-guidance-and-consensus.md` 参照）。"
    )
    lines.append("")
    return lines


def _edinet_section(code: str, synthetic: bool) -> list[str]:
    lines = ["## EDINET 法定開示書類（原文確認用）", ""]
    if synthetic:
        lines.append("合成データモードのため EDINET への問い合わせは行っていない。")
        lines.append("")
        return lines
    if not os.environ.get("EDINET_API_KEY", "").strip():
        lines.append(
            "環境変数 `EDINET_API_KEY` が未設定のため書類一覧は取得していない。"
            "APIキー（無料、2024年以降必須）を設定すると、直近の有価証券報告書・半期報告書の"
            "一覧と docID をここに出力する（`stocklib/edinet.py` 参照）。"
        )
        lines.append("")
        return lines

    from stocklib.edinet import DOC_TYPE_LABELS, EdinetError, search_documents

    try:
        docs = search_documents(code, days=365)
    except EdinetError as exc:
        lines.append(f"EDINET 書類一覧の取得に失敗した: {exc}")
        lines.append("")
        return lines
    if docs.empty:
        lines.append("直近365日に該当する有価証券報告書・四半期/半期報告書は見つからなかった。")
        lines.append("")
        return lines
    rows = [
        [
            row["docID"],
            DOC_TYPE_LABELS.get(str(row["docTypeCode"]), str(row["docTypeCode"])),
            row["docDescription"] or "-",
            row["submitDateTime"] or "-",
        ]
        for _, row in docs.head(10).iterrows()
    ]
    lines.append(report.markdown_table(["docID", "書類種別", "書類名", "提出日時"], rows))
    lines.append("")
    lines.append(
        "原文（財務諸表CSV）は `stocklib.edinet.fetch_document_csv(\"<docID>\")` で取得できる。"
        "決算短信は適時開示（TDnet）であり EDINET には含まれない。"
    )
    lines.append("")
    return lines


def build_report(code: str, years: int, synthetic: bool) -> str:
    """業績分析レポート本文（Markdown）を構築する。"""
    history = fetch_financial_history(code, years=years, synthetic=synthetic)

    lines: list[str] = []
    lines.append(report.report_header(f"決算・業績分析レポート: {code}（{normalize_code(code)}）"))
    lines.append(f"- 対象期間: {history.index[0].strftime('%Y-%m')} 〜 {history.index[-1].strftime('%Y-%m')}（{len(history)} 期、年次）")
    lines.append("- 数値ソース: " + ("**合成データ（--synthetic、実在企業の業績ではありません。手法デモ用）**" if synthetic else "yfinance（非公式ソース。会計基準・組替により開示原文と差異がありうる）"))
    lines.append("")
    lines.extend(_history_section(history))
    lines.extend(_growth_section(history))
    lines.extend(_edinet_section(code, synthetic))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="単一銘柄の業績推移・成長分析レポートを生成する")
    parser.add_argument("code", help="銘柄コード（4桁数字、例: 7203）")
    parser.add_argument("--years", type=int, default=5, help="取得する最大年数（既定: 5）")
    parser.add_argument("--synthetic", action="store_true", help="合成業績で実行（ネットワーク不要）")
    args = parser.parse_args(argv)

    try:
        content = build_report(args.code, args.years, args.synthetic)
    except (DataFetchError, ValueError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    filename = f"fundamentals-{args.code}-{dt.date.today().isoformat()}.md"
    path = report.save_report(content, filename)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

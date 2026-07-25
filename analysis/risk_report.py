#!/usr/bin/env python3
"""リスク/ボラティリティ指標 CLI — 単一銘柄の下方・テールリスクを1本のレポートに集計する。

年率換算のリターン・ボラだけでは見えない「下方への偏り」と「テールの厚み」に着目し、
下方偏差・ソルティノレシオ・ヒストリカル VaR/ES（95%/99%）・最大ドローダウンと
その継続日数・直近ボラティリティのレジーム（過去分布での位置）を機械的に集計する。

使い方（リポジトリルートから）:

    python3 analysis/risk_report.py 7203                 # 既定 period=2y
    python3 analysis/risk_report.py 7203 --period 5y
    python3 analysis/risk_report.py 7203 --synthetic     # 合成データ（ネット不要）

自動実行（Routine / cron）向けの機械可読な契約（market_breadth.py に準拠）:

- stdout の最終行に
  ``RESULT var95=<値> maxdd=<値> data=<real|synthetic|unavailable>``
- 実データ取得に失敗した場合（--synthetic なし）は stderr 出力 / exit code 2 /
  ``data=unavailable``（レポート非生成）。
- 引数・期間指定の不正等は exit code 1（RESULT 行なし）。

**本レポートは機械的なリスク計測であり、将来の損失を予測するものでも投資助言でもない。**
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import pandas as pd

from stocklib import risk, report
from stocklib.data import (
    DataFetchError,
    add_source_argument,
    fetch_prices,
    set_default_source,
)


def build_report(
    code: str, result: risk.RiskResult, period: str, synthetic: bool
) -> str:
    """リスク指標一式を Markdown レポート文字列に整形する。"""
    today = dt.date.today().isoformat()
    lines = [report.report_header(f"リスク/ボラティリティ指標 {code}（{today}）")]
    lines.append(f"- 銘柄コード: {code}")
    lines.append(f"- 取得期間: {period}（有効リターン {result.n} 日）")
    lines.append(f"- データ出所: {'合成データ' if synthetic else 'yfinance'}")
    if synthetic:
        lines.append(
            "- **データ: 合成データ（--synthetic）による手法デモであり実際の値動きではありません**"
        )
    lines.append("")

    lines.append("## 下方リスク")
    lines.append("")
    lines.append(report.markdown_table(
        ["指標", "値", "備考"],
        [
            ["下方偏差（年率, MAR=0）", report.fmt_pct(result.downside_dev),
             "負のリターンのみの標準偏差（上方変動は罰しない）"],
            ["ソルティノレシオ（年率）", report.fmt_num(result.sortino),
             "超過リターン ÷ 下方偏差"],
        ],
    ))
    lines.append("")

    lines.append("## バリュー・アット・リスク / 期待ショートフォール（日次）")
    lines.append("")
    lines.append(report.markdown_table(
        ["指標", "値", "読み方"],
        [
            ["ヒストリカル VaR 95%", report.fmt_pct(result.var95),
             "95% の日はこの損失以内"],
            ["ヒストリカル VaR 99%", report.fmt_pct(result.var99),
             "99% の日はこの損失以内"],
            ["条件付き VaR / ES 95%", report.fmt_pct(result.cvar95),
             "最悪 5% の日の平均損失"],
            ["条件付き VaR / ES 99%", report.fmt_pct(result.cvar99),
             "最悪 1% の日の平均損失"],
        ],
    ))
    lines.append("")

    lines.append("## ドローダウン")
    lines.append("")
    dur_note = "回復済み" if result.dd_recovered else "継続中（末尾がアンダーウォーター）"
    lines.append(report.markdown_table(
        ["指標", "値", "備考"],
        [
            ["最大ドローダウン", report.fmt_pct(result.max_drawdown),
             "高値からの最大下落率"],
            ["最長ドローダウン継続日数", f"{result.max_dd_duration} 営業日",
             f"ピークから回復までの最長期間（{dur_note}）"],
        ],
    ))
    lines.append("")

    lines.append("## ボラティリティ・レジーム")
    lines.append("")
    lines.append(report.markdown_table(
        ["指標", "値", "備考"],
        [
            [f"直近ボラ（{result.vol_window}日, 年率）", report.fmt_pct(result.current_vol),
             "過去 N 日のローリング年率ボラの最新値"],
            ["全期間分布でのパーセンタイル", report.fmt_num(result.vol_percentile, 1) + " / 100",
             "高いほど過去比で高ボラ局面"],
        ],
    ))
    lines.append("")

    lines.append(
        "解釈の枠組みは `knowledge/math/` のリスク指標・`knowledge/fundamental/` の"
        "リスク管理関連文書を参照。VaR/ES は過去のリターン分布に基づく推定であり、"
        "分布外の急変（テール）を過小評価しうる。下方偏差・ソルティノは下方変動に、"
        "ドローダウンは高値からの下落の深さと長さに着目した指標。"
        "**いずれも過去データの機械的なリスク計測であり、将来の損失を予測するものでも"
        "投資助言でもない。**"
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="単一銘柄の下方・テールリスク指標（VaR/ES・下方偏差・ドローダウン・ボラレジーム）を集計する"
    )
    parser.add_argument("code", help="銘柄コード（4桁数字。例: 7203。内部で .T 正規化）")
    parser.add_argument("--period", default="2y",
                        help="取得期間（既定: 2y。VaR・ドローダウンの安定には1y以上を推奨）")
    parser.add_argument("--vol-window", type=int, default=21,
                        help="ローリング年率ボラの窓（営業日、既定: 21 ≒ 1ヶ月）")
    parser.add_argument("--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）")
    add_source_argument(parser)
    args = parser.parse_args(argv)
    set_default_source(args.source)

    if args.vol_window < 2:
        print("エラー: --vol-window は 2 以上を指定してください。", file=sys.stderr)
        return 1

    try:
        prices = fetch_prices(args.code, period=args.period, synthetic=args.synthetic)[args.code]
    except (DataFetchError, ValueError) as exc:
        if isinstance(exc, ValueError):
            print(f"エラー: {exc}", file=sys.stderr)
            return 1
        print(f"エラー: {args.code} の実データを取得できませんでした: {exc}", file=sys.stderr)
        print("Yahoo（query1/2.finance.yahoo.com）への到達性を確認してください"
              "（オフライン検証は --synthetic）。", file=sys.stderr)
        print(f"RESULT var95=na maxdd=na data=unavailable")
        return 2

    close = prices["Close"] if "Close" in prices.columns else prices.iloc[:, 0]
    result = risk.compute_risk(close, vol_window=args.vol_window)

    content = build_report(args.code, result, args.period, args.synthetic)
    print(report.with_disclaimer(content))
    path = report.save_report(content, f"risk-{args.code}-{dt.date.today().isoformat()}.md")
    print(f"レポート: {path}")
    var95 = f"{result.var95:.4f}" if pd.notna(result.var95) else "na"
    maxdd = f"{result.max_drawdown:.4f}" if pd.notna(result.max_drawdown) else "na"
    print(f"RESULT var95={var95} maxdd={maxdd} "
          f"data={'synthetic' if args.synthetic else 'real'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""季節性・カレンダーアノマリー CLI — 単一銘柄/指数の暦効果を集計する。

長期の価格系列から、月別・曜日別・月内（月初/月末）・半期（Sell in May）の
リターンの季節性を機械的に集計し、標本数 n を併記したレポートを生成する。

使い方（リポジトリルートから）:

    python3 analysis/seasonality_report.py ^N225                 # 日経平均（既定 period=max）
    python3 analysis/seasonality_report.py 7203 --period 10y     # 個別銘柄・期間指定
    python3 analysis/seasonality_report.py 7203 --synthetic      # 合成データ（ネット不要）

自動実行（Routine / cron）向けの機械可読な契約:

- stdout の最終行に ``RESULT months=<集計月数> years=<年数> data=<real|synthetic|unavailable>``
- 実データが取れなかった場合（--synthetic なし）は stderr ＋ exit 2 ／ ``data=unavailable``
  （レポート非生成）。銘柄コード未指定・不正な引数等は exit 1（RESULT 行なし）。正常は exit 0。

**季節性は過去の標本統計であり、将来の再現を保証しない。** 月×曜日×期間の探索は本質的に
多重検定であり、真の効果がなくてもデータスヌーピングで偶然のアノマリーが生じうる。
本レポートは投資助言ではない（解釈の枠組み: `knowledge/strategies/market-anomalies-and-seasonality.md`）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from stocklib import report, seasonality
from stocklib.data import (
    DataFetchError,
    add_source_argument,
    fetch_prices,
    set_default_source,
)


def _fmt_stat(stat: seasonality.GroupStat) -> list[str]:
    """GroupStat をテーブル行のセル列（平均・勝率・標準偏差・n）に整形する。"""
    return [
        report.fmt_pct(stat.mean_return),
        report.fmt_pct(stat.win_rate) if stat.n else "-",
        report.fmt_pct(stat.std) if stat.n > 1 else "-",
        report.fmt_num(stat.n),
    ]


def build_report(
    code: str, result: seasonality.SeasonalityResult, period: str, synthetic: bool
) -> str:
    today = dt.date.today().isoformat()
    lines = [report.report_header(f"季節性・カレンダーアノマリー {code}（{today}）")]
    span = ""
    if result.start is not None and result.end is not None:
        span = f"{result.start.date()} 〜 {result.end.date()}"
    lines.append(f"- 対象: {code}（取得期間 {period}{'、' + span if span else ''}）")
    lines.append(f"- 標本: 月次リターン {result.n_months} 件・日次リターン {result.n_days} 件"
                 f"・跨ぐ暦年 {result.years} 年")
    lines.append(f"- データ出所: {'合成データ' if synthetic else 'yfinance'}")
    if synthetic:
        lines.append("- **データ: 合成データ（--synthetic）による手法デモであり実データではありません。"
                     "合成の幾何ブラウン運動には暦効果を作り込んでいないため、下表の季節性は"
                     "偶然のばらつきです（実証には実データが必要）。**")
    lines.append("")

    # 月別効果
    lines.append("## 月別効果（Month-of-the-Year）")
    lines.append("")
    lines.append("月末終値どうしの月次リターンを暦月ごとに集計。平均・勝率（正の月の割合）・"
                 "標準偏差・標本数 n を併記する。")
    lines.append("")
    month_rows = [[s.label, *_fmt_stat(s)] for s in result.monthly]
    lines.append(report.markdown_table(
        ["月", "平均リターン", "勝率", "標準偏差", "n（年数）"], month_rows))
    lines.append("")

    # 曜日効果
    lines.append("## 曜日効果（Day-of-the-Week）")
    lines.append("")
    lines.append("日次リターンを曜日ごとに集計。1980〜90年代に見られた月曜の負リターン等は"
                 "近年多くの検証で有意性を失っている（`market-anomalies-and-seasonality.md`）。")
    lines.append("")
    wd_rows = [[s.label, *_fmt_stat(s)] for s in result.weekday]
    lines.append(report.markdown_table(
        ["曜日", "平均リターン", "勝率", "標準偏差", "n（日数）"], wd_rows))
    lines.append("")

    # 月内効果
    tom = result.turn_of_month
    if tom is not None:
        lines.append("## 月内（月初/月末）効果（Turn-of-the-Month）")
        lines.append("")
        lines.append(f"各暦月の最初の {tom.first_days} 立会日と最後の {tom.last_days} 立会日を"
                     "「月替わり窓」とし、窓内と窓外（月中）の日次リターン平均を対比する。")
        lines.append("")
        lines.append(report.markdown_table(
            ["区分", "平均リターン", "勝率", "標準偏差", "n（日数）"],
            [["月替わり窓", *_fmt_stat(tom.tom)], ["月中", *_fmt_stat(tom.rest)]]))
        lines.append("")
        lines.append(f"窓内 − 窓外の差（エッジ）: {report.fmt_pct(tom.edge)}"
                     "（正なら月替わり窓の方が高リターン。給与日後の積立買付・月初の資金配分等の"
                     "キャッシュフロー同期が説明として挙げられるが、標本と期間で符号は変わりうる）。")
        lines.append("")

    # 半期効果（Sell in May）
    if result.winter is not None and result.summer is not None:
        lines.append("## 半期効果（Sell in May / ハロウィン効果）")
        lines.append("")
        lines.append("月次リターンを 11〜4月 と 5〜10月 の2群に分けて平均を対比する。"
                     "長期データでは 11〜4月 が優位（ハロウィン効果）とされることが多いが、"
                     "年ごとのばらつきが大きく単年での再現は保証されない。")
        lines.append("")
        lines.append(report.markdown_table(
            ["半期", "平均月次リターン", "勝率", "標準偏差", "n（月数）"],
            [["11〜4月", *_fmt_stat(result.winter)], ["5〜10月", *_fmt_stat(result.summer)]]))
        lines.append("")
        lines.append(f"11〜4月 − 5〜10月 の差: {report.fmt_pct(result.sell_in_may_edge)}"
                     "（正ならハロウィン効果と整合）。")
        lines.append("")

    # 注意書き
    lines.append("## 注意 — 多重検定とデータスヌーピング")
    lines.append("")
    lines.append(
        "上記はいずれも**過去に観測された標本統計であり、将来の再現を保証しない**。"
        "月（12通り）×曜日（5通り）×期間の探索は本質的に多重検定で、真の効果がなくても"
        "偶然「有意に見える」組み合わせが生じる（データスヌーピング）。$m$ 個の独立な検定を"
        "有意水準 $\\alpha$ で行うとき少なくとも1件偽陽性が出る確率は $1-(1-\\alpha)^m$ で、"
        "急速に1へ近づく。少数標本（n が小さい集計）の平均は特に不安定である。"
        "公表済みアノマリーは公表後に減衰しやすく、売買コスト控除後に消えることも多い。"
        "季節性は単独の売買根拠ではなく、タイミング調整やイベント週のリスク管理の補助に留めるのが"
        "現実的な位置づけである（`knowledge/strategies/market-anomalies-and-seasonality.md`）。"
        "**本レポートは機械的な集計であり投資助言ではない。**"
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="単一銘柄/指数の季節性・カレンダーアノマリー（月別・曜日別・月内・半期）を集計する"
    )
    parser.add_argument("code", help="銘柄コード（4桁数字は内部で .T 正規化）または指数（例: ^N225）")
    parser.add_argument("--period", default="max",
                        help="取得期間（既定: max。季節性の実証には長期を推奨。例: 10y, 20y, max）")
    parser.add_argument("--first-days", type=int, default=seasonality.DEFAULT_FIRST_DAYS,
                        help=f"月替わり窓に含める月初の立会日数（既定: {seasonality.DEFAULT_FIRST_DAYS}）")
    parser.add_argument("--last-days", type=int, default=seasonality.DEFAULT_LAST_DAYS,
                        help=f"月替わり窓に含める月末の立会日数（既定: {seasonality.DEFAULT_LAST_DAYS}）")
    parser.add_argument("--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）")
    add_source_argument(parser)
    args = parser.parse_args(argv)
    set_default_source(args.source)

    if args.first_days < 0 or args.last_days < 0:
        parser.error("--first-days / --last-days には 0 以上を指定してください")

    try:
        prices = fetch_prices(args.code, period=args.period, synthetic=args.synthetic)[args.code]
    except DataFetchError as exc:
        print(f"エラー: {args.code} の実データを取得できませんでした: {exc}", file=sys.stderr)
        print("Yahoo（query1/2.finance.yahoo.com）への到達性を確認してください"
              "（オフライン検証は --synthetic）。", file=sys.stderr)
        print("RESULT months=0 years=0 data=unavailable")
        return 2
    except ValueError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    result = seasonality.compute_seasonality(prices, args.first_days, args.last_days)
    content = build_report(args.code, result, args.period, args.synthetic)
    print(content)
    safe_code = args.code.replace("^", "_").replace("=", "_").replace("/", "_")
    path = report.save_report(content, f"seasonality-{safe_code}-{dt.date.today().isoformat()}.md")
    print(f"レポート: {path}")
    print(f"RESULT months={result.n_months} years={result.years} "
          f"data={'synthetic' if args.synthetic else 'real'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

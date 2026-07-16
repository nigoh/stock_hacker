#!/usr/bin/env python3
"""複数銘柄の相対パフォーマンス・相関比較 CLI。

使い方（リポジトリルートから）:
    python3 analysis/compare.py 7203 6758 9984 [--period 1y] [--synthetic]

比較レポートを stdout に出力し、reports/compare-<日付>.md にも保存する。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import pandas as pd

from stocklib import charts, metrics, report
from stocklib.data import DataFetchError, fetch_prices


def _chart_lines(prices: dict[str, pd.DataFrame], img_stem: str) -> list[str]:
    """相対パフォーマンス図 PNG を生成し、埋め込み用 Markdown 行を返す（失敗時は警告して空リスト）。"""
    if not charts.charts_available():
        print("警告: matplotlib が利用できないため、チャートなしで続行します", file=sys.stderr)
        return []
    try:
        path = charts.plot_relative_performance(
            prices, charts.IMG_DIR / f"{img_stem}-relative.png"
        )
    except Exception as exc:  # チャートは補助情報。失敗してもレポート生成は続行する
        print(f"警告: チャート生成に失敗しました（チャートなしで続行します）: {exc}", file=sys.stderr)
        return []
    return [f"![chart](img/{path.name})", ""]


def build_report(
    codes: list[str], period: str, synthetic: bool, img_stem: str | None = None
) -> str:
    """比較レポート本文（Markdown）を構築する。

    Args:
        img_stem: チャート PNG のファイル名接頭辞（``reports/img/<img_stem>-relative.png``）。
            ``None`` の場合はチャートを生成しない。
    """
    prices = fetch_prices(codes, period=period, synthetic=synthetic)
    closes = pd.concat({c: prices[c]["Close"] for c in codes}, axis=1).dropna()
    if closes.empty:
        raise DataFetchError("共通の取引日が存在せず、比較できませんでした。")
    returns = pd.DataFrame({c: metrics.daily_returns(closes[c]) for c in codes}).dropna()

    lines: list[str] = [report.report_header(f"銘柄比較レポート: {' / '.join(codes)}")]
    lines.append(f"- 期間: {period}（{closes.index[0].date()} 〜 {closes.index[-1].date()}、{len(closes)} 営業日）")
    if synthetic:
        lines.append("- **データ: 合成データ（--synthetic、実在の株価ではありません）**")
    lines.append("")

    # 相対パフォーマンス（期首=100）
    normalized = closes / closes.iloc[0] * 100.0
    lines.append("## 相対パフォーマンス（期首 = 100）")
    lines.append("")
    if img_stem is not None:
        lines.extend(_chart_lines(prices, img_stem))
    rows = []
    for code in codes:
        row: list[object] = [code]
        for lag_name, lag in (("1ヶ月前", 21), ("3ヶ月前", 63), ("6ヶ月前", 126)):
            row.append(report.fmt_num(float(normalized[code].iloc[-1 - lag])) if len(normalized) > lag else "-")
        row.append(report.fmt_num(float(normalized[code].iloc[-1])))
        rows.append(row)
    lines.append(report.markdown_table(["銘柄", "1ヶ月前", "3ヶ月前", "6ヶ月前", "直近"], rows))
    lines.append("")

    lines.append("## リスク・リターン指標")
    lines.append("")
    rows = []
    for code in codes:
        r = returns[code]
        rows.append([
            code,
            report.fmt_pct(float(closes[code].iloc[-1] / closes[code].iloc[0] - 1.0)),
            report.fmt_pct(metrics.ann_return(r)),
            report.fmt_pct(metrics.ann_vol(r)),
            report.fmt_num(metrics.sharpe(r)),
            report.fmt_pct(metrics.max_drawdown(closes[code])),
        ])
    lines.append(report.markdown_table(
        ["銘柄", "期間リターン", "年率リターン", "年率ボラ", "シャープ", "最大DD"], rows,
    ))
    lines.append("")

    lines.append("## 日次リターン相関行列")
    lines.append("")
    corr = metrics.correlation_matrix(returns)
    lines.append(report.df_to_markdown(corr, digits=3, index_name="銘柄"))
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="複数銘柄の相対パフォーマンスと相関を比較する")
    parser.add_argument("codes", nargs="+", help="銘柄コード（2つ以上、例: 7203 6758 9984）")
    parser.add_argument("--period", default="1y", help="取得期間（既定: 1y）")
    parser.add_argument("--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）")
    parser.add_argument("--no-charts", action="store_true", help="チャート画像の生成・埋め込みを無効化する")
    args = parser.parse_args(argv)

    if len(args.codes) < 2:
        print("エラー: 比較には2つ以上の銘柄コードを指定してください", file=sys.stderr)
        return 1

    filename = f"compare-{'-'.join(args.codes)}-{dt.date.today().isoformat()}.md"
    img_stem = None if args.no_charts else filename.removesuffix(".md")
    try:
        content = build_report(args.codes, args.period, args.synthetic, img_stem=img_stem)
    except (DataFetchError, ValueError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    print(content)
    path = report.save_report(content, filename)
    print(f"レポート: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

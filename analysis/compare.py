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

from stocklib import charts, currency, metrics, report
from stocklib.data import (
    DataFetchError,
    add_source_argument,
    fetch_prices,
    set_default_source,
)


def _chart_lines(
    prices: dict[str, pd.DataFrame], img_stem: str, title: str | None = None
) -> list[str]:
    """相対パフォーマンス図 PNG を生成し、埋め込み用 Markdown 行を返す（失敗時は警告して空リスト）。"""
    if not charts.charts_available():
        print("警告: matplotlib が利用できないため、チャートなしで続行します", file=sys.stderr)
        return []
    try:
        path = charts.plot_relative_performance(
            prices, charts.IMG_DIR / f"{img_stem}-relative.png", title=title
        )
    except Exception as exc:  # チャートは補助情報。失敗してもレポート生成は続行する
        print(f"警告: チャート生成に失敗しました（チャートなしで続行します）: {exc}", file=sys.stderr)
        return []
    return [f"![chart](img/{path.name})", ""]


def build_report(
    codes: list[str],
    period: str,
    synthetic: bool,
    img_stem: str | None = None,
    in_currency: str | None = None,
) -> str:
    """比較レポート本文（Markdown）を構築する。

    Args:
        img_stem: チャート PNG のファイル名接頭辞（``reports/img/<img_stem>-relative.png``）。
            ``None`` の場合はチャートを生成しない。
        in_currency: 基準通貨コード（``"USD"`` / ``"EUR"`` / ``"GBP"``）。指定すると
            各銘柄の円建て価格をクロス円レート（例: EURJPY=X）の同日終値で除した
            基準通貨建て系列（海外投資家視点）に換算してから比較する。
    """
    prices = fetch_prices(codes, period=period, synthetic=synthetic)
    label: str | None = None
    chart_title: str | None = None
    if in_currency is not None:
        label = currency.currency_label(in_currency)
        fx_ticker = currency.get_fx_ticker(in_currency)
        fx_df = currency.fetch_fx(in_currency, period, synthetic=synthetic)
        prices = {c: currency.to_base_currency(prices[c], fx_df) for c in codes}
        chart_title = (
            f"Relative Performance in {in_currency.upper()}: {' / '.join(codes)}"
        )
    closes = pd.concat({c: prices[c]["Close"] for c in codes}, axis=1).dropna()
    if closes.empty:
        raise DataFetchError("共通の取引日が存在せず、比較できませんでした。")
    returns = pd.DataFrame({c: metrics.daily_returns(closes[c]) for c in codes}).dropna()

    title_suffix = f"（{label}建て）" if label is not None else ""
    lines: list[str] = [report.report_header(f"銘柄比較レポート: {' / '.join(codes)}{title_suffix}")]
    lines.append(f"- 期間: {period}（{closes.index[0].date()} 〜 {closes.index[-1].date()}、{len(closes)} 営業日）")
    if in_currency is not None:
        lines.append(
            f"- **表示通貨: {label}建て換算（海外投資家視点）。各営業日の円建て価格を"
            f"同日の {fx_ticker} 終値（1{label}あたり円）で除して換算。"
            "為替ヘッジ・配当・売買コストは考慮していない。**"
        )
    if synthetic:
        lines.append("- **データ: 合成データ（--synthetic、実在の株価ではありません）**")
    lines.append("")

    # 相対パフォーマンス（期首=100）
    normalized = closes / closes.iloc[0] * 100.0
    lines.append("## 相対パフォーマンス（期首 = 100）")
    lines.append("")
    if img_stem is not None:
        lines.extend(_chart_lines(prices, img_stem, title=chart_title))
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
    add_source_argument(parser)
    parser.add_argument("--no-charts", action="store_true", help="チャート画像の生成・埋め込みを無効化する")
    parser.add_argument(
        "--in-currency",
        type=str.upper,
        choices=sorted(currency.SUPPORTED_CURRENCIES),
        default=None,
        help="円建て価格をクロス円レートで除した基準通貨建て系列（海外投資家視点）で"
        "比較する（例: EUR → EURJPY=X で換算）",
    )
    parser.add_argument(
        "--in-usd",
        action="store_true",
        help="--in-currency USD のエイリアス（後方互換）",
    )
    args = parser.parse_args(argv)
    set_default_source(args.source)
    in_currency: str | None = args.in_currency or ("USD" if args.in_usd else None)

    if len(args.codes) < 2:
        print("エラー: 比較には2つ以上の銘柄コードを指定してください", file=sys.stderr)
        return 1

    ccy_part = f"-{in_currency.lower()}" if in_currency is not None else ""
    filename = f"compare-{'-'.join(args.codes)}{ccy_part}-{dt.date.today().isoformat()}.md"
    img_stem = None if args.no_charts else filename.removesuffix(".md")
    try:
        content = build_report(
            args.codes, args.period, args.synthetic, img_stem=img_stem, in_currency=in_currency
        )
    except (DataFetchError, ValueError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    print(report.with_disclaimer(content))
    path = report.save_report(content, filename)
    print(f"レポート: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

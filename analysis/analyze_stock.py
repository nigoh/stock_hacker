#!/usr/bin/env python3
"""単一銘柄の総合分析レポートを生成する CLI。

使い方（リポジトリルートから）:
    python3 analysis/analyze_stock.py 7203 [--period 2y] [--benchmark ^N225] [--synthetic]

reports/analyze-<code>-<日付>.md を生成し、そのパスを stdout に出力する。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import pandas as pd

from stocklib import backtest  # noqa: F401  （依存確認用）
from stocklib import indicators, metrics, report
from stocklib.data import DataFetchError, fetch_info, fetch_prices, normalize_code


def build_report(
    code: str,
    period: str,
    benchmark: str,
    synthetic: bool,
) -> str:
    """分析レポート本文（Markdown）を構築する。"""
    prices = fetch_prices([code, benchmark], period=period, synthetic=synthetic)
    df = prices[code]
    bench_df = prices[benchmark]
    close = df["Close"]

    try:
        info = fetch_info(code, synthetic=synthetic)
    except DataFetchError:
        info = {}

    rets = metrics.daily_returns(close)
    bench_rets = metrics.daily_returns(bench_df["Close"])

    sma25 = indicators.sma(close, 25)
    sma75 = indicators.sma(close, 75)
    sma200 = indicators.sma(close, 200)
    rsi14 = indicators.rsi(close, 14)
    macd_df = indicators.macd(close)
    bb = indicators.bollinger(close)
    ichi = indicators.ichimoku(df)
    atr14 = indicators.atr(df, 14)

    last = close.iloc[-1]
    lines: list[str] = []
    lines.append(report.report_header(f"銘柄分析レポート: {code}（{normalize_code(code)}）"))
    lines.append(f"- 期間: {period}（{df.index[0].date()} 〜 {df.index[-1].date()}、{len(df)} 営業日）")
    lines.append(f"- ベンチマーク: {benchmark}")
    if synthetic:
        lines.append("- **データ: 合成データ（--synthetic、実在の株価ではありません）**")
    lines.append("")

    if info:
        lines.append("## 基本情報")
        lines.append("")
        lines.append(report.markdown_table(
            ["項目", "値"],
            [[k, report.fmt_num(v)] for k, v in info.items()],
        ))
        lines.append("")

    lines.append("## 価格サマリー")
    lines.append("")
    period_return = float(last / close.iloc[0] - 1.0)
    lines.append(report.markdown_table(
        ["項目", "値"],
        [
            ["終値（直近）", report.fmt_num(float(last))],
            ["期間高値", report.fmt_num(float(df['High'].max()))],
            ["期間安値", report.fmt_num(float(df['Low'].min()))],
            ["期間リターン", report.fmt_pct(period_return)],
            ["ATR(14)", report.fmt_num(float(atr14.iloc[-1]))],
        ],
    ))
    lines.append("")

    def _vs(ma: pd.Series, name: str) -> list[str]:
        v = ma.iloc[-1]
        if pd.isna(v):
            return [name, "-", "データ不足"]
        rel = "上" if last > v else "下"
        return [name, report.fmt_num(float(v)), f"終値は{rel}に位置"]

    rsi_last = float(rsi14.iloc[-1]) if not pd.isna(rsi14.iloc[-1]) else float("nan")
    if rsi_last >= 70:
        rsi_note = "買われすぎ圏（70以上）"
    elif rsi_last <= 30:
        rsi_note = "売られすぎ圏（30以下）"
    else:
        rsi_note = "中立圏"
    macd_last = macd_df.iloc[-1]
    macd_note = "MACD > シグナル（上向き）" if macd_last["macd"] > macd_last["signal"] else "MACD < シグナル（下向き）"

    ichi_last = ichi.iloc[-1]
    if pd.notna(ichi_last["senkou_a"]) and pd.notna(ichi_last["senkou_b"]):
        cloud_top = max(ichi_last["senkou_a"], ichi_last["senkou_b"])
        cloud_bottom = min(ichi_last["senkou_a"], ichi_last["senkou_b"])
        if last > cloud_top:
            ichi_note = "雲の上（強気圏）"
        elif last < cloud_bottom:
            ichi_note = "雲の下（弱気圏）"
        else:
            ichi_note = "雲の中（方向感なし）"
    else:
        ichi_note = "データ不足"

    lines.append("## テクニカル指標")
    lines.append("")
    lines.append(report.markdown_table(
        ["指標", "値", "状態"],
        [
            _vs(sma25, "SMA(25)"),
            _vs(sma75, "SMA(75)"),
            _vs(sma200, "SMA(200)"),
            ["RSI(14)", report.fmt_num(rsi_last), rsi_note],
            ["MACD(12,26,9)", report.fmt_num(float(macd_last["macd"])), macd_note],
            [
                "ボリンジャーバンド(20,2σ)",
                f"{report.fmt_num(float(bb['lower'].iloc[-1]))} 〜 {report.fmt_num(float(bb['upper'].iloc[-1]))}",
                "バンド内" if bb["lower"].iloc[-1] <= last <= bb["upper"].iloc[-1] else "バンド外",
            ],
            ["一目均衡表", f"基準線 {report.fmt_num(float(ichi_last['kijun'])) if pd.notna(ichi_last['kijun']) else '-'}", ichi_note],
        ],
    ))
    lines.append("")

    lines.append("## リスク・リターン指標")
    lines.append("")
    lines.append(report.markdown_table(
        ["指標", "値"],
        [
            ["年率リターン", report.fmt_pct(metrics.ann_return(rets))],
            ["年率ボラティリティ", report.fmt_pct(metrics.ann_vol(rets))],
            ["シャープレシオ", report.fmt_num(metrics.sharpe(rets))],
            ["ソルティノレシオ", report.fmt_num(metrics.sortino(rets))],
            ["最大ドローダウン", report.fmt_pct(metrics.max_drawdown(close))],
            [f"ベータ（vs {benchmark}）", report.fmt_num(metrics.beta(rets, bench_rets))],
            ["ヒストリカルVaR（95%、日次）", report.fmt_pct(metrics.var_historical(rets, 0.95))],
        ],
    ))
    lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="単一銘柄の総合分析レポートを生成する")
    parser.add_argument("code", help="銘柄コード（4桁数字、例: 7203）")
    parser.add_argument("--period", default="2y", help="取得期間（既定: 2y）")
    parser.add_argument("--benchmark", default="^N225", help="ベンチマーク（既定: ^N225）")
    parser.add_argument("--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）")
    args = parser.parse_args(argv)

    try:
        content = build_report(args.code, args.period, args.benchmark, args.synthetic)
    except (DataFetchError, ValueError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    filename = f"analyze-{args.code}-{dt.date.today().isoformat()}.md"
    path = report.save_report(content, filename)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

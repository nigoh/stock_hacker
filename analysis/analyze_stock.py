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
from stocklib import charts, currency, indicators, metrics, report
from stocklib.data import DataFetchError, fetch_info, fetch_prices, normalize_code


def _chart_lines(df: pd.DataFrame, code: str, img_stem: str) -> list[str]:
    """価格チャート PNG を生成し、埋め込み用 Markdown 行を返す（失敗時は警告して空リスト）。"""
    if not charts.charts_available():
        print("警告: matplotlib が利用できないため、チャートなしで続行します", file=sys.stderr)
        return []
    try:
        path = charts.plot_price_chart(df, code, charts.IMG_DIR / f"{img_stem}-price.png")
    except Exception as exc:  # チャートは補助情報。失敗してもレポート生成は続行する
        print(f"警告: チャート生成に失敗しました（チャートなしで続行します）: {exc}", file=sys.stderr)
        return []
    return [
        "## チャート",
        "",
        f"![chart](img/{path.name})",
        "",
        "（上段: ローソク足 + SMA + ボリンジャーバンド、中段: 出来高、下段: RSI(14)。陽線=赤系・陰線=青系）",
        "",
    ]


def _fx_section(close: pd.Series, period: str, synthetic: bool, ccy: str) -> list[str]:
    """「<基準通貨>建てパフォーマンス（海外投資家視点）」節の Markdown 行を構築する。

    円建て終値を基準通貨のクロス円レート（例: USDJPY=X・EURJPY=X）の同日終値で
    除した基準通貨建て系列で主要指標を再計算し、円建てとの差（為替寄与）を併記する。
    """
    label = currency.currency_label(ccy)
    ticker = currency.get_fx_ticker(ccy)
    pair = ticker.removesuffix("=X")
    fx_df = currency.fetch_fx(ccy, period, synthetic=synthetic)
    fx = currency.align_fx(close.index, fx_df["Close"])
    base_close = close / fx

    rets_jpy = metrics.daily_returns(close)
    rets_base = metrics.daily_returns(base_close)
    period_jpy = float(close.iloc[-1] / close.iloc[0] - 1.0)
    period_base = float(base_close.iloc[-1] / base_close.iloc[0] - 1.0)
    fx_change = float(fx.iloc[-1] / fx.iloc[0] - 1.0)
    fx_contrib_pt = period_jpy - period_base  # 円建て − 基準通貨建て（パーセントポイント）

    lines: list[str] = []
    lines.append(f"## {label}建てパフォーマンス（海外投資家視点）")
    lines.append("")
    lines.append(
        f"円建て終値を同日の {ticker} 終値（1{label}あたり円）で除した"
        f"{label}建て系列に基づく再計算（為替ヘッジ・配当・売買コストは考慮しない）。"
        "本レポートの主表示は円建てであり、本節は海外投資家視点の参考情報。"
    )
    lines.append("")
    lines.append(report.markdown_table(
        ["指標", "円建て", f"{label}建て"],
        [
            ["期間リターン", report.fmt_pct(period_jpy), report.fmt_pct(period_base)],
            ["年率リターン", report.fmt_pct(metrics.ann_return(rets_jpy)), report.fmt_pct(metrics.ann_return(rets_base))],
            ["年率ボラティリティ", report.fmt_pct(metrics.ann_vol(rets_jpy)), report.fmt_pct(metrics.ann_vol(rets_base))],
            ["シャープレシオ", report.fmt_num(metrics.sharpe(rets_jpy)), report.fmt_num(metrics.sharpe(rets_base))],
            ["最大ドローダウン", report.fmt_pct(metrics.max_drawdown(close)), report.fmt_pct(metrics.max_drawdown(base_close))],
        ],
    ))
    lines.append("")
    if fx_change > 0:
        direction = f"円安方向＝{label}建てリターンの押し下げ要因"
    elif fx_change < 0:
        direction = f"円高方向＝{label}建てリターンの押し上げ要因"
    else:
        direction = "為替は横ばい"
    lines.append(
        f"- 為替（{pair}）期間変動: {report.fmt_pct(fx_change)}"
        f"（{fx.iloc[0]:.2f} → {fx.iloc[-1]:.2f} 円/{label}、{direction}）"
    )
    lines.append(
        f"- 為替寄与（円建て期間リターン − {label}建て期間リターン）: {fx_contrib_pt * 100:+.2f} ポイント"
    )
    lines.append(
        f"- 厳密には $(1 + r^{{{ccy}}}) = (1 + r^{{JPY}}) / (1 + r^{{FX}})$ の関係が成り立ち、"
        "上記の差分はその近似値。"
    )
    lines.append("")
    return lines


def build_report(
    code: str,
    period: str,
    benchmark: str,
    synthetic: bool,
    img_stem: str | None = None,
    in_currency: str | None = None,
) -> str:
    """分析レポート本文（Markdown）を構築する。

    Args:
        img_stem: チャート PNG のファイル名接頭辞（``reports/img/<img_stem>-price.png``）。
            ``None`` の場合はチャートを生成しない。
        in_currency: 基準通貨コード（``"USD"`` / ``"EUR"`` / ``"GBP"``）。指定すると
            「<通貨>建てパフォーマンス（海外投資家視点）」節を追加する
            （主表示は円建てのまま）。``None`` なら節を追加しない。
    """
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

    if img_stem is not None:
        lines.extend(_chart_lines(df, code, img_stem))

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

    if in_currency is not None:
        lines.extend(_fx_section(close, period, synthetic, in_currency))

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="単一銘柄の総合分析レポートを生成する")
    parser.add_argument("code", help="銘柄コード（4桁数字、例: 7203）")
    parser.add_argument("--period", default="2y", help="取得期間（既定: 2y）")
    parser.add_argument("--benchmark", default="^N225", help="ベンチマーク（既定: ^N225）")
    parser.add_argument("--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）")
    parser.add_argument("--no-charts", action="store_true", help="チャート画像の生成・埋め込みを無効化する")
    parser.add_argument(
        "--in-currency",
        type=str.upper,
        choices=sorted(currency.SUPPORTED_CURRENCIES),
        default=None,
        help="基準通貨建てパフォーマンス節（海外投資家視点、クロス円レートで換算）を"
        "レポートに追加する（例: EUR → EURJPY=X で換算）",
    )
    parser.add_argument(
        "--in-usd",
        action="store_true",
        help="--in-currency USD のエイリアス（後方互換）",
    )
    args = parser.parse_args(argv)
    in_currency: str | None = args.in_currency or ("USD" if args.in_usd else None)

    filename = f"analyze-{args.code}-{dt.date.today().isoformat()}.md"
    img_stem = None if args.no_charts else filename.removesuffix(".md")
    try:
        content = build_report(
            args.code,
            args.period,
            args.benchmark,
            args.synthetic,
            img_stem=img_stem,
            in_currency=in_currency,
        )
    except (DataFetchError, ValueError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    path = report.save_report(content, filename)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

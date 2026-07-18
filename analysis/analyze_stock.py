#!/usr/bin/env python3
"""単一銘柄の総合分析レポートを生成する CLI。

使い方（リポジトリルートから）:
    python3 analysis/analyze_stock.py 7203 [--period 2y] [--benchmark ^N225] \
        [--horizon short|mid|long] [--synthetic]

reports/analyze-<code>[-<horizon>]-<日付>.md を生成し、そのパスを stdout に出力する。
--horizon を指定すると時間軸（短期/中期/長期）別の「視点」節が追加され、
--period 未指定時の取得期間が short=6mo / mid=2y / long=5y になる。
省略時は従来どおりの全部入り（期間 2y、ファイル名も従来どおり）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys

import pandas as pd

from stocklib import backtest  # noqa: F401  （依存確認用）
from stocklib import charts, currency, indicators, metrics, report, signals
from stocklib.data import (
    DataFetchError,
    add_source_argument,
    fetch_info,
    fetch_prices,
    normalize_code,
    set_default_source,
)

# --horizon の選択肢と、--period 未指定時の取得期間・レポート表記
HORIZON_CHOICES: tuple[str, ...] = ("short", "mid", "long")
HORIZON_DEFAULT_PERIODS: dict[str, str] = {"short": "6mo", "mid": "2y", "long": "5y"}
HORIZON_LABELS: dict[str, str] = {
    "short": "短期（〜数週間）",
    "mid": "中期（数ヶ月〜1年）",
    "long": "長期（数年〜）",
}
DEFAULT_PERIOD: str = "2y"

# 短期視点で参照する「直近高安値」の窓（営業日）
RECENT_RANGE_WINDOW: int = 20


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


def _ma_row(last: float, ma: pd.Series, name: str) -> list[str]:
    """移動平均テーブルの1行（値と終値の位置関係）を構築する。"""
    v = ma.iloc[-1]
    if pd.isna(v):
        return [name, "-", "データ不足"]
    rel = "上" if last > float(v) else "下"
    return [name, report.fmt_num(float(v)), f"終値は{rel}に位置"]


def _rsi_note(value: float) -> str:
    """RSI(14) の状態ラベル（NaN は従来動作どおり中立圏として扱う）。"""
    if value >= 70:
        return "買われすぎ圏（70以上）"
    if value <= 30:
        return "売られすぎ圏（30以下）"
    return "中立圏"


def _trailing_return(close: pd.Series, days: int) -> float:
    """直近終値の ``days`` 営業日前比リターン。データ不足時は NaN。"""
    if len(close) <= days:
        return float("nan")
    return float(close.iloc[-1] / close.iloc[-1 - days] - 1.0)


def _fmt_dividend_yield(value: object) -> str:
    """配当利回りを整形する。

    yfinance の ``dividendYield`` には比率（0.025）と百分率（2.5）の表記揺れが
    あるため、0.5 以下は比率とみなして百分率化し、それより大きい値は既に
    百分率とみなしてそのまま ``%`` を付ける。数値でなければ ``-``。
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "-"
    v = float(value)
    if math.isnan(v):
        return "-"
    return report.fmt_pct(v) if v <= 0.5 else f"{v:.2f}%"


def _horizon_short_section(df: pd.DataFrame, atr14: pd.Series, rsi14: pd.Series) -> list[str]:
    """「短期の視点（〜数週間）」節を構築する。

    5/25日線・RSI(14)・ATR・出来高急増・直近高安値と、ATR倍数によるストップ
    （撤退水準）の目安を提示する。
    """
    close = df["Close"]
    last = float(close.iloc[-1])
    sma5 = indicators.sma(close, 5)
    sma25 = indicators.sma(close, 25)
    atr_last = float(atr14.iloc[-1]) if pd.notna(atr14.iloc[-1]) else float("nan")
    rsi_last = float(rsi14.iloc[-1]) if pd.notna(rsi14.iloc[-1]) else float("nan")

    lines: list[str] = ["## 短期の視点（〜数週間）", ""]
    lines.append(
        "**この時間軸で見るべきもの**: 5/25日線の向きとクロス・RSI(14)・ATRに基づく値幅・"
        "出来高の変化・直近の高安値。 "
        "**見るべきでないもの**: PER/PBR等のバリュエーションや年率換算の長期統計は、"
        "数週間の値動きをほとんど説明しない。"
    )
    lines.append("")

    s5, s25 = sma5.iloc[-1], sma25.iloc[-1]
    if pd.isna(s5) or pd.isna(s25):
        cross_note = "データ不足"
    elif float(s5) > float(s25):
        cross_note = "SMA5 > SMA25（短期上向き配列）"
    elif float(s5) < float(s25):
        cross_note = "SMA5 < SMA25（短期下向き配列）"
    else:
        cross_note = "SMA5 = SMA25（拮抗）"

    vol_value, vol_note = "-", "データ不足"
    if "Volume" in df.columns and len(df) > signals.VOLUME_AVG_WINDOW:
        vol = df["Volume"]
        avg = float(vol.iloc[-(signals.VOLUME_AVG_WINDOW + 1):-1].mean())
        if math.isfinite(avg) and avg > 0:
            ratio = float(vol.iloc[-1]) / avg
            vol_value = f"{ratio:.2f} 倍"
            vol_note = (
                f"出来高急増（> {signals.VOLUME_SURGE_RATIO:g} 倍）"
                if ratio > signals.VOLUME_SURGE_RATIO
                else f"平常圏（急増の目安: {signals.VOLUME_SURGE_RATIO:g} 倍超）"
            )

    hi_recent = float(df["High"].iloc[-RECENT_RANGE_WINDOW:].max())
    lo_recent = float(df["Low"].iloc[-RECENT_RANGE_WINDOW:].min())

    lines.append(report.markdown_table(
        ["指標", "値", "状態"],
        [
            _ma_row(last, sma5, "SMA(5)"),
            _ma_row(last, sma25, "SMA(25)"),
            ["5日線と25日線", "-", cross_note],
            ["RSI(14)", report.fmt_num(rsi_last), _rsi_note(rsi_last) if math.isfinite(rsi_last) else "データ不足"],
            [
                "ATR(14)",
                report.fmt_num(atr_last),
                f"終値比 {report.fmt_pct(atr_last / last)}（平均的な1日の値幅）"
                if math.isfinite(atr_last) and last > 0
                else "データ不足",
            ],
            [f"出来高（直近 / {signals.VOLUME_AVG_WINDOW}日平均）", vol_value, vol_note],
            [
                f"直近{RECENT_RANGE_WINDOW}日高値",
                report.fmt_num(hi_recent),
                f"終値との距離 {report.fmt_pct((hi_recent - last) / hi_recent)}",
            ],
            [
                f"直近{RECENT_RANGE_WINDOW}日安値",
                report.fmt_num(lo_recent),
                f"終値との距離 {report.fmt_pct((last - lo_recent) / lo_recent)}",
            ],
        ],
    ))
    lines.append("")

    if math.isfinite(atr_last) and atr_last > 0:
        lines.append("ストップ（撤退水準）設定の目安（ATR倍数）:")
        lines.append("")
        lines.append(report.markdown_table(
            ["ATR倍数", "水準（終値 − k×ATR）", "終値からの距離"],
            [
                [
                    f"{k:g}×ATR",
                    report.fmt_num(last - k * atr_last),
                    report.fmt_pct(-(k * atr_last) / last),
                ]
                for k in (1.0, 2.0, 3.0)
            ],
        ))
        lines.append("")
        lines.append(
            "- ATR倍数によるストップは「平均的な1日の値幅（ATR）の何倍の逆行で当初想定が崩れたと"
            "みなすか」という機械的な目安であり、特定水準での売買を推奨するものではない。"
            "ポジションサイズ（許容損失額 ÷ ストップ幅）とセットで考える。"
        )
    else:
        lines.append("- ATR がデータ不足のため、ストップ目安（ATR倍数）は計算できない。")
    lines.append("")
    return lines


def _horizon_mid_section(
    df: pd.DataFrame,
    sma25: pd.Series,
    sma75: pd.Series,
    sma200: pd.Series,
) -> list[str]:
    """「中期の視点（数ヶ月〜1年）」節を構築する。

    25/75/200日線の並び・3/6/12ヶ月モメンタム・52週高値からの距離と、
    決算スケジュールへの注意喚起を提示する。
    """
    close = df["Close"]
    last = float(close.iloc[-1])

    lines: list[str] = ["## 中期の視点（数ヶ月〜1年）", ""]
    lines.append(
        "**この時間軸で見るべきもの**: 25/75/200日線の並びと傾き・3/6/12ヶ月モメンタム・"
        "52週高値からの距離・決算イベント。 "
        "**見るべきでないもの**: 数日単位のRSI過熱や単日の出来高急増は短期ノイズであり、"
        "中期の判断材料としては弱い。"
    )
    lines.append("")

    s25, s75, s200 = sma25.iloc[-1], sma75.iloc[-1], sma200.iloc[-1]
    if pd.isna(s25) or pd.isna(s75) or pd.isna(s200):
        order_note = "データ不足（期間を延ばすと200日線まで計算できる）"
    elif float(s25) > float(s75) > float(s200):
        order_note = "SMA25 > SMA75 > SMA200（上昇配列）"
    elif float(s25) < float(s75) < float(s200):
        order_note = "SMA25 < SMA75 < SMA200（下降配列）"
    else:
        order_note = "混在（トレンド転換局面の可能性を含む）"

    win52 = close.iloc[-signals.WEEK52_WINDOW:]
    high52 = float(win52.max())
    dist52 = last / high52 - 1.0 if high52 > 0 else float("nan")
    note52 = "" if len(win52) >= signals.WEEK52_WINDOW else f"（データ{len(win52)}営業日分で計算）"

    lines.append(report.markdown_table(
        ["指標", "値", "備考"],
        [
            _ma_row(last, sma25, "SMA(25)"),
            _ma_row(last, sma75, "SMA(75)"),
            _ma_row(last, sma200, "SMA(200)"),
            ["移動平均の並び", "-", order_note],
            ["3ヶ月リターン（63営業日）", report.fmt_pct(_trailing_return(close, 63)), "モメンタム"],
            ["6ヶ月リターン（126営業日）", report.fmt_pct(_trailing_return(close, 126)), "モメンタム"],
            ["12ヶ月リターン（252営業日）", report.fmt_pct(_trailing_return(close, 252)), "モメンタム"],
            ["52週高値からの距離", report.fmt_pct(dist52), f"52週高値 {report.fmt_num(high52)}{note52}"],
        ],
    ))
    lines.append("")
    lines.append(
        "- **決算スケジュールへの注意**: 数ヶ月〜1年の時間軸では、四半期決算・期初ガイダンス・"
        "通期予想の修正が株価の主要イベントになる（日本企業の期初ガイダンスには保守的傾向が"
        "指摘される。`knowledge/fundamental/earnings-guidance-and-consensus.md` 参照）。"
        "決算発表日をまたぐ保有はギャップリスクを伴うため、発表日を会社IR・適時開示（TDnet）で"
        "事前に確認する。"
    )
    lines.append("")
    return lines


def _horizon_long_section(close: pd.Series, rets: pd.Series, info: dict[str, object]) -> list[str]:
    """「長期の視点（数年〜）」節を構築する。

    年率リターン/ボラ・最大ドローダウン・配当利回り・PBR/PERの長期文脈と、
    積立（時間分散）適性の観点を提示する。
    """
    ann_vol = metrics.ann_vol(rets)

    lines: list[str] = ["## 長期の視点（数年〜）", ""]
    lines.append(
        "**この時間軸で見るべきもの**: 年率リターン/ボラティリティ・最大ドローダウン・"
        "配当と株主還元の持続性・PBR/PERの長期レンジ。 "
        "**見るべきでないもの**: RSI(14)や出来高急増などの短期オシレーターを"
        "数年スケールの判断に使う意味は薄い。"
    )
    lines.append("")
    lines.append(report.markdown_table(
        ["指標", "値"],
        [
            ["年率リターン（幾何平均）", report.fmt_pct(metrics.ann_return(rets))],
            ["年率ボラティリティ（√252換算）", report.fmt_pct(ann_vol)],
            ["最大ドローダウン", report.fmt_pct(metrics.max_drawdown(close))],
            ["配当利回り", _fmt_dividend_yield(info.get("配当利回り"))],
            ["PER（実績）", report.fmt_num(info.get("PER（実績）"))],
            ["PBR", report.fmt_num(info.get("PBR"))],
        ],
    ))
    lines.append("")
    lines.append(
        "- **PBR/PERの長期文脈**: 単年のPER/PBR水準だけでは割安・割高を判定できない。"
        "自社の過去レンジ・業種水準・金利環境との比較で読む"
        "（`knowledge/fundamental/valuation-metrics.md`）。低PBRが資本コストを上回れない"
        "ROEの裏返しになっていないか、PBR ≒ ROE × PER の分解で確認する。"
    )
    lines.append(
        "- **積立（時間分散）適性の観点**: 定額積立の平均取得単価は購入時価格の調和平均 "
        "$\\bar P_H = n / \\sum_t (1/P_t)$ となり、同じ期間の算術平均 $\\bar P_A$ を上回らない"
        "（$\\bar P_H \\le \\bar P_A$、等号は無変動時のみ）。価格のばらつきが大きいほどこの差は"
        "広がるため「ボラティリティが高いほど時間分散の効果」が語られるが、これは取得単価の"
        "平準化に関する数学的性質であって、一括投資より期待リターンが高くなることを意味しない"
        "（期待リターンが正なら、市場滞在期間の長い早期一括投資が平均的には有利という比較結果が"
        f"知られる）。本銘柄の年率ボラティリティは {report.fmt_pct(ann_vol)}（√252換算）。"
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
    horizon: str | None = None,
) -> str:
    """分析レポート本文（Markdown）を構築する。

    Args:
        img_stem: チャート PNG のファイル名接頭辞（``reports/img/<img_stem>-price.png``）。
            ``None`` の場合はチャートを生成しない。
        in_currency: 基準通貨コード（``"USD"`` / ``"EUR"`` / ``"GBP"``）。指定すると
            「<通貨>建てパフォーマンス（海外投資家視点）」節を追加する
            （主表示は円建てのまま）。``None`` なら節を追加しない。
        horizon: 時間軸フレーム（``"short"`` / ``"mid"`` / ``"long"``）。指定すると
            該当時間軸の「視点」節（見るべきもの/見るべきでないものの注記付き）を
            追加する。``None`` なら従来どおりの全部入り（節を追加しない）。
    """
    if horizon is not None and horizon not in HORIZON_LABELS:
        raise ValueError(
            f"不正な horizon 指定です: {horizon!r}（'short' / 'mid' / 'long' のいずれか）"
        )
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
    if horizon is not None:
        lines.append(f"- 時間軸フレーム: {HORIZON_LABELS[horizon]}")
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

    if horizon == "short":
        lines.extend(_horizon_short_section(df, atr14, rsi14))
    elif horizon == "mid":
        lines.extend(_horizon_mid_section(df, sma25, sma75, sma200))
    elif horizon == "long":
        lines.extend(_horizon_long_section(close, rets, info))

    rsi_last = float(rsi14.iloc[-1]) if not pd.isna(rsi14.iloc[-1]) else float("nan")
    rsi_note = _rsi_note(rsi_last)
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
            _ma_row(float(last), sma25, "SMA(25)"),
            _ma_row(float(last), sma75, "SMA(75)"),
            _ma_row(float(last), sma200, "SMA(200)"),
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
    parser.add_argument(
        "--period",
        default=None,
        help="取得期間（既定: 2y。--horizon 指定時の既定は short=6mo / mid=2y / long=5y）",
    )
    parser.add_argument("--benchmark", default="^N225", help="ベンチマーク（既定: ^N225）")
    parser.add_argument(
        "--horizon",
        choices=HORIZON_CHOICES,
        default=None,
        help="時間軸フレーム（short: 〜数週間 / mid: 数ヶ月〜1年 / long: 数年〜）。"
        "指定すると該当時間軸の「視点」節をレポートに追加し、出力ファイル名は "
        "analyze-<code>-<horizon>-<日付>.md になる（省略時は従来どおりの全部入り）",
    )
    parser.add_argument("--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）")
    add_source_argument(parser)
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
    set_default_source(args.source)
    in_currency: str | None = args.in_currency or ("USD" if args.in_usd else None)
    period: str = args.period if args.period is not None else (
        HORIZON_DEFAULT_PERIODS[args.horizon] if args.horizon else DEFAULT_PERIOD
    )

    horizon_part = f"-{args.horizon}" if args.horizon else ""
    filename = f"analyze-{args.code}{horizon_part}-{dt.date.today().isoformat()}.md"
    img_stem = None if args.no_charts else filename.removesuffix(".md")
    try:
        content = build_report(
            args.code,
            period,
            args.benchmark,
            args.synthetic,
            img_stem=img_stem,
            in_currency=in_currency,
            horizon=args.horizon,
        )
    except (DataFetchError, ValueError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    path = report.save_report(content, filename)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

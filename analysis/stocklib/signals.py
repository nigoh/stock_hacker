"""ウォッチ銘柄のテクニカルシグナル検出モジュール。

OHLCV DataFrame（少なくとも ``Close`` 列。``Volume`` 列があれば出来高シグナルも判定）を
受け取り、直近営業日時点で成立しているシグナルを :class:`Signal` のリストで返す。
各シグナルの数式・閾値は :func:`detect_signals` の docstring に明記する。
判定はあくまで機械的な条件検出であり、売買の推奨ではない。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from stocklib import indicators

# --- 閾値定数（変更時は detect_signals の docstring も更新すること） ---
RSI_WINDOW: int = 14
RSI_OVERSOLD: float = 30.0
RSI_OVERBOUGHT: float = 70.0
FAST_SMA_WINDOW: int = 25
SLOW_SMA_WINDOW: int = 75
CROSS_LOOKBACK: int = 5
VOLUME_AVG_WINDOW: int = 20
VOLUME_SURGE_RATIO: float = 2.0
WEEK52_WINDOW: int = 252
WEEK52_PROXIMITY: float = 0.03
PRICE_MOVE_THRESHOLD: float = 0.03


@dataclass(frozen=True)
class Signal:
    """検出されたシグナル1件。

    Attributes:
        kind: シグナル種別。``"rsi"`` / ``"ma_cross"`` / ``"volume"`` /
            ``"week52"`` / ``"price_move"`` のいずれか。
        direction: 教科書的な解釈での方向感。``"bullish"``（強気）/
            ``"bearish"``（弱気）/ ``"neutral"``（方向性なし）。
            機械的なラベルであり、将来の騰落を予測するものではない。
        detail: 日本語の説明文（観測値・閾値を含む）。
    """

    kind: str
    direction: str
    detail: str


def _rsi_signal(close: pd.Series) -> Signal | None:
    """RSI(14) の過熱シグナル。閾値: 30以下（売られすぎ）/ 70以上（買われすぎ）。"""
    if len(close) <= RSI_WINDOW:
        return None
    value = indicators.rsi(close, RSI_WINDOW).iloc[-1]
    if pd.isna(value):
        return None
    value = float(value)
    if value <= RSI_OVERSOLD:
        return Signal(
            kind="rsi",
            direction="bullish",
            detail=f"RSI({RSI_WINDOW}) = {value:.1f} ≤ {RSI_OVERSOLD:g}（売られすぎ水準）",
        )
    if value >= RSI_OVERBOUGHT:
        return Signal(
            kind="rsi",
            direction="bearish",
            detail=f"RSI({RSI_WINDOW}) = {value:.1f} ≥ {RSI_OVERBOUGHT:g}（買われすぎ水準）",
        )
    return None


def _ma_cross_signal(close: pd.Series) -> Signal | None:
    """SMA(25)/SMA(75) のゴールデンクロス・デッドクロス（直近5営業日以内）。"""
    if len(close) <= SLOW_SMA_WINDOW:
        return None
    diff = indicators.sma(close, FAST_SMA_WINDOW) - indicators.sma(close, SLOW_SMA_WINDOW)
    prev = diff.shift(1)
    golden = (prev <= 0) & (diff > 0)  # NaN を含む比較は False
    dead = (prev >= 0) & (diff < 0)
    for mask, direction, label in (
        (golden, "bullish", "ゴールデンクロス（SMA25 が SMA75 を上抜け）"),
        (dead, "bearish", "デッドクロス（SMA25 が SMA75 を下抜け）"),
    ):
        recent = mask.iloc[-CROSS_LOOKBACK:]
        if bool(recent.any()):
            days_ago = len(recent) - 1 - int(recent.to_numpy().nonzero()[0][-1])
            when = "当日" if days_ago == 0 else f"{days_ago}営業日前"
            return Signal(kind="ma_cross", direction=direction, detail=f"{label}、{when}に発生")
    return None


def _volume_signal(volume: pd.Series | None) -> Signal | None:
    """出来高急増。直近出来高が過去20日（直近日を除く）平均の2倍超。"""
    if volume is None or len(volume) <= VOLUME_AVG_WINDOW:
        return None
    avg = float(volume.iloc[-(VOLUME_AVG_WINDOW + 1):-1].mean())
    if not math.isfinite(avg) or avg <= 0:
        return None
    ratio = float(volume.iloc[-1]) / avg
    if ratio > VOLUME_SURGE_RATIO:
        return Signal(
            kind="volume",
            direction="neutral",
            detail=(
                f"出来高急増: 過去{VOLUME_AVG_WINDOW}日平均の {ratio:.1f} 倍"
                f"（> {VOLUME_SURGE_RATIO:g} 倍）"
            ),
        )
    return None


def _week52_signals(close: pd.Series) -> list[Signal]:
    """52週高値/安値（終値ベース、過去252営業日）から3%以内。"""
    window = close.iloc[-WEEK52_WINDOW:]
    high = float(window.max())
    low = float(window.min())
    last = float(close.iloc[-1])
    if not math.isfinite(high) or high <= low:  # 無変動系列は判定しない
        return []
    note = f"、データ{len(window)}営業日分で計算" if len(window) < WEEK52_WINDOW else ""
    out: list[Signal] = []
    if (high - last) / high <= WEEK52_PROXIMITY:
        out.append(Signal(
            kind="week52",
            direction="bullish",
            detail=f"52週高値圏: 高値 {high:,.1f} から {(high - last) / high:.2%} 以内{note}",
        ))
    if (last - low) / low <= WEEK52_PROXIMITY:
        out.append(Signal(
            kind="week52",
            direction="bearish",
            detail=f"52週安値圏: 安値 {low:,.1f} から {(last - low) / low:.2%} 以内{note}",
        ))
    return out


def _price_move_signal(close: pd.Series) -> Signal | None:
    """前日比 ±3% 超の急変動。"""
    if len(close) < 2:
        return None
    prev = float(close.iloc[-2])
    if prev == 0:
        return None
    change = float(close.iloc[-1]) / prev - 1.0
    if abs(change) > PRICE_MOVE_THRESHOLD:
        direction = "bullish" if change > 0 else "bearish"
        return Signal(
            kind="price_move",
            direction=direction,
            detail=f"前日比 {change:+.2%} の急変動（±{PRICE_MOVE_THRESHOLD:.0%} 超）",
        )
    return None


def detect_signals(df: pd.DataFrame) -> list[Signal]:
    """OHLCV DataFrame から直近営業日時点のテクニカルシグナルを検出する。

    検出対象と数式・閾値（$C_t$: 終値、$V_t$: 出来高）:

    1. **RSI 過熱**（kind ``"rsi"``）: Wilder 平滑化の RSI(14)
       （:func:`stocklib.indicators.rsi`）が
       $\\mathrm{RSI}_{14} \\le 30$（売られすぎ、bullish）または
       $\\mathrm{RSI}_{14} \\ge 70$（買われすぎ、bearish）。
    2. **移動平均クロス**（kind ``"ma_cross"``）: 直近5営業日以内に
       $$ \\mathrm{SMA}^{25}_{t-1} \\le \\mathrm{SMA}^{75}_{t-1}
          \\ \\wedge\\ \\mathrm{SMA}^{25}_{t} > \\mathrm{SMA}^{75}_{t} $$
       が成立（ゴールデンクロス、bullish）。不等号を反転した条件は
       デッドクロス（bearish）。両方あれば新しい方のみ…ではなく先に
       成立を検出したゴールデンクロスを優先し、1件のみ返す。
    3. **出来高急増**（kind ``"volume"``、neutral）:
       $$ V_t > 2 \\times \\frac{1}{20} \\sum_{i=1}^{20} V_{t-i} $$
       （直近日を除く20日平均の2倍超。厳密な不等号）。
    4. **52週高値/安値接近**（kind ``"week52"``）: 過去252営業日の終値の
       最高値 $H$・最安値 $L$ に対し
       $(H - C_t)/H \\le 0.03$（高値圏、bullish）/
       $(C_t - L)/L \\le 0.03$（安値圏、bearish）。データが252営業日に満たない
       場合は利用可能な範囲で計算し、detail にその旨を付記する。
    5. **急変動**（kind ``"price_move"``）: 前日比
       $|C_t / C_{t-1} - 1| > 0.03$（±3%超。厳密な不等号。+側 bullish / −側 bearish）。

    Args:
        df: ``Close`` 列必須、``Volume`` 列は任意の DataFrame
            （:func:`stocklib.data.fetch_prices` の返す OHLCV 形式）。

    Returns:
        成立したシグナルのリスト（kind の定義順）。データ不足で判定できない
        シグナルは黙ってスキップする。系列が2点未満なら空リスト。

    Raises:
        ValueError: ``Close`` 列が無い場合。
    """
    if "Close" not in df.columns:
        raise ValueError("detect_signals には Close 列を持つ DataFrame を渡してください")
    close = df["Close"].dropna()
    if len(close) < 2:
        return []
    volume = df["Volume"].dropna() if "Volume" in df.columns else None

    out: list[Signal] = []
    for sig in (_rsi_signal(close), _ma_cross_signal(close), _volume_signal(volume)):
        if sig is not None:
            out.append(sig)
    out.extend(_week52_signals(close))
    sig = _price_move_signal(close)
    if sig is not None:
        out.append(sig)
    return out

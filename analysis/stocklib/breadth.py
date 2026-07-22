"""市場ブレッシュ（市場の内部＝マーケット・インターナルズ）の集計モジュール。

指数の水準だけでは分からない「市場全体の内部の強弱」を、ユニバース（複数銘柄）の
OHLCV から機械的に集計する。指数が数銘柄の大型株で押し上げられていても、
ブレッシュ（値上がり銘柄の広がり）が弱ければ地合いは脆い——といった読みに使う。

集計する指標（解釈は :file:`knowledge/technical/volume-and-market-internals.md`）:

- **移動平均超の銘柄割合**: 終値が SMA(25/75/200) 以上の銘柄の割合。
- **騰落数**: 前日比で値上がり / 値下がり / 変わらずの銘柄数。
- **騰落レシオ（25日）**: 直近25営業日の値上がり銘柄数合計 ÷ 値下がり銘柄数合計 × 100。
  一般に 120 以上で過熱、70 以下で売られすぎとされる（2025年時点の目安。閾値は経験則）。
- **新高値 / 新安値**: 直近252営業日の高値（安値）を更新した銘柄数。

いずれも将来の騰落を予測するものではなく、機械的な内部状態の記述である。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from stocklib import indicators  # noqa: F401 - 将来の指標拡張用に予約

# SMA の対象窓（この順でレポートに出す）。
SMA_WINDOWS: tuple[int, ...] = (25, 75, 200)
# 新高値・新安値の判定窓（営業日）。
WEEK52_WINDOW: int = 252
# 騰落レシオの集計窓（営業日）。
AD_RATIO_WINDOW: int = 25
# 騰落レシオの目安閾値（経験則、2025年時点）。
AD_RATIO_OVERHEATED: float = 120.0
AD_RATIO_OVERSOLD: float = 70.0


@dataclass
class BreadthResult:
    """ユニバース全体のブレッシュ集計。"""

    n: int  # 集計に使えた銘柄数（Close が2本以上ある銘柄）
    advancers: int
    decliners: int
    unchanged: int
    pct_above_sma: dict[int, float] = field(default_factory=dict)  # 窓 → 割合（0..1）
    sma_base: dict[int, int] = field(default_factory=dict)  # 窓 → 判定できた銘柄数
    new_highs: int = 0
    new_lows: int = 0
    ad_ratio_25: float | None = None

    @property
    def advance_pct(self) -> float:
        """値上がり銘柄の割合（値上がり＋値下がり＋変わらずに対する。0..1）。"""
        return self.advancers / self.n if self.n else float("nan")

    def ad_ratio_label(self) -> str:
        """騰落レシオの水準ラベル（過熱 / 売られすぎ / 中立 / 不足）。"""
        if self.ad_ratio_25 is None:
            return "算出不可（データ期間不足）"
        if self.ad_ratio_25 >= AD_RATIO_OVERHEATED:
            return f"過熱圏（≥ {AD_RATIO_OVERHEATED:g}）"
        if self.ad_ratio_25 <= AD_RATIO_OVERSOLD:
            return f"売られすぎ圏（≤ {AD_RATIO_OVERSOLD:g}）"
        return "中立"


def _closes_frame(prices: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """各銘柄の Close を1つの DataFrame（列=銘柄）に束ねる（日付 index の和集合）。"""
    series = {code: df["Close"] for code, df in prices.items() if "Close" in df.columns}
    if not series:
        return pd.DataFrame()
    return pd.DataFrame(series).sort_index()


def _ad_ratio(prices: dict[str, pd.DataFrame], window: int = AD_RATIO_WINDOW) -> float | None:
    """騰落レシオ（window 日）。値下がり合計が0、またはデータ不足なら None。"""
    mat = _closes_frame(prices)
    if mat.shape[1] == 0 or len(mat) < window + 1:
        return None
    diff = mat.diff()
    advancers = (diff > 0).sum(axis=1)
    decliners = (diff < 0).sum(axis=1)
    adv_sum = float(advancers.iloc[-window:].sum())
    dec_sum = float(decliners.iloc[-window:].sum())
    if dec_sum <= 0:
        return None
    return 100.0 * adv_sum / dec_sum


def compute_breadth(prices: dict[str, pd.DataFrame]) -> BreadthResult:
    """ユニバースの OHLCV 辞書から市場ブレッシュを集計する。

    Args:
        prices: ``{code: OHLCV DataFrame}``（``Close`` 列必須）。
            :func:`stocklib.data.fetch_prices` の戻り値をそのまま渡せる。

    Returns:
        :class:`BreadthResult`。銘柄が1つも集計できない場合は ``n=0``。
    """
    above: dict[int, int] = {w: 0 for w in SMA_WINDOWS}
    base: dict[int, int] = {w: 0 for w in SMA_WINDOWS}
    n = advancers = decliners = unchanged = new_highs = new_lows = 0

    for df in prices.values():
        if "Close" not in df.columns:
            continue
        close = df["Close"].dropna()
        if len(close) < 2:
            continue
        n += 1
        last = float(close.iloc[-1])
        change = last - float(close.iloc[-2])
        if change > 0:
            advancers += 1
        elif change < 0:
            decliners += 1
        else:
            unchanged += 1

        for window in SMA_WINDOWS:
            if len(close) >= window:
                base[window] += 1
                if last >= float(close.iloc[-window:].mean()):
                    above[window] += 1

        if len(close) >= WEEK52_WINDOW:
            win = close.iloc[-WEEK52_WINDOW:]
            hi, lo = float(win.max()), float(win.min())
            if math.isfinite(hi) and last >= hi * (1 - 1e-9):
                new_highs += 1
            if math.isfinite(lo) and lo > 0 and last <= lo * (1 + 1e-9):
                new_lows += 1

    pct_above = {w: (above[w] / base[w] if base[w] else float("nan")) for w in SMA_WINDOWS}
    return BreadthResult(
        n=n,
        advancers=advancers,
        decliners=decliners,
        unchanged=unchanged,
        pct_above_sma=pct_above,
        sma_base=dict(base),
        new_highs=new_highs,
        new_lows=new_lows,
        ad_ratio_25=_ad_ratio(prices, AD_RATIO_WINDOW),
    )

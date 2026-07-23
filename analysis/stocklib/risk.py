"""リスク・ボラティリティ指標モジュール。

日次リターン系列（下方偏差・VaR・条件付き VaR・ボラティリティレジーム）または
価格系列（ドローダウンとその継続日数）から、テール・下方に着目したリスク指標を計算する。
全関数は ``numpy`` / ``pandas`` のみに依存する純関数でネットワークを使わない。
年率換算には営業日数 252（``metrics.TRADING_DAYS``）を用いる。

ソルティノレシオは :func:`stocklib.metrics.sortino` を、ヒストリカル VaR は
:func:`stocklib.metrics.var_historical` を再利用する（本モジュールから再エクスポートする）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from stocklib import metrics
from stocklib.metrics import TRADING_DAYS, sortino, var_historical

__all__ = [
    "TRADING_DAYS",
    "sortino",
    "var_historical",
    "downside_deviation",
    "cvar_historical",
    "drawdown_series",
    "drawdown_stats",
    "DrawdownStats",
    "rolling_ann_vol",
    "vol_regime",
    "RiskResult",
    "compute_risk",
]


def downside_deviation(
    returns: pd.Series, mar: float = 0.0, periods: int = TRADING_DAYS
) -> float:
    """下方偏差（Downside Deviation、年率換算）。

    最低許容リターン（MAR、既定 0）を下回る日のみの二乗平均平方根を年率換算する:

    $$ \\mathrm{DD} = \\sqrt{\\frac{1}{n}\\sum_{t=1}^{n} \\min(r_t - \\mathrm{MAR}, 0)^2}
       \\cdot \\sqrt{252} $$

    上方の変動を罰しない点で標準偏差と異なり、ソルティノレシオの分母に対応する。
    ``mar`` は日次の目標リターン（既定 0）。観測が 1 未満なら NaN。
    """
    returns = returns.dropna()
    if len(returns) < 1:
        return float("nan")
    shortfall = np.minimum(returns.to_numpy() - mar, 0.0)
    daily = float(np.sqrt(np.mean(np.square(shortfall))))
    return daily * np.sqrt(periods)


def cvar_historical(returns: pd.Series, level: float = 0.95) -> float:
    """条件付き VaR ＝ 期待ショートフォール（Expected Shortfall、日次・負の値で返す）。

    信頼水準 $c$ の VaR（$(1-c)$ 分位点）を超える損失側テールの平均:

    $$ \\mathrm{ES}_{c} = \\mathbb{E}\\left[\\, r \\mid r \\le \\mathrm{VaR}_{c} \\,\\right] $$

    VaR が「$c$% の日はこの損失以内」を表すのに対し、ES は「その外側（最悪 $(1-c)$%）に
    落ちたときの平均的な損失」を表し、テールの厚みを VaR より捉えやすい。
    例: ``level=0.95`` で ``-0.05`` なら「最悪 5% の日の平均損失は 5%」と読む。
    """
    returns = returns.dropna()
    if len(returns) == 0:
        return float("nan")
    threshold = float(returns.quantile(1.0 - level))
    tail = returns[returns <= threshold]
    if len(tail) == 0:
        return threshold
    return float(tail.mean())


def drawdown_series(prices: pd.Series) -> pd.Series:
    """各時点のドローダウン $P_t / \\max_{s \\le t} P_s - 1$（0 以下）の系列を返す。"""
    prices = prices.dropna()
    if len(prices) == 0:
        return pd.Series(dtype=float)
    return prices / prices.cummax() - 1.0


@dataclass
class DrawdownStats:
    """ドローダウンの集計結果。

    Attributes:
        max_drawdown: 最大ドローダウン（負の値。例: 高値から半値まで下げたら ``-0.5``）。
        max_duration: 最長のアンダーウォーター継続日数（営業日）。あるピークから、
            そのピークを回復する（＝新高値を付ける）までの最長のバー数。
        recovered: 最長のアンダーウォーター区間が最終的に回復したか。系列末尾が
            アンダーウォーターのまま終わっている場合は ``False``（継続中）。
    """

    max_drawdown: float
    max_duration: int
    recovered: bool


def drawdown_stats(prices: pd.Series) -> DrawdownStats:
    """最大ドローダウンと最長のアンダーウォーター継続日数を計算する。

    最大ドローダウンは :func:`stocklib.metrics.max_drawdown` と同じ

    $$ \\mathrm{MaxDD} = \\min_t \\left( \\frac{P_t}{\\max_{s \\le t} P_s} - 1 \\right) $$

    継続日数（drawdown duration）は最長の「アンダーウォーター本数」（ピーク割れの状態で
    経過した営業日数）。回復済み区間は新高値バー間隔 − 1（回復バー自身は含めない）、
    末尾がアンダーウォーターで終わる区間はピーク翌日から系列末尾までのバー数として、
    両者を同一基準で数える（``recovered=False`` の継続中区間も候補）。
    最大ドローダウンの深さと最長の継続区間は必ずしも同一区間ではない点に注意。
    """
    prices = prices.dropna()
    n = len(prices)
    if n == 0:
        return DrawdownStats(float("nan"), 0, True)
    values = prices.to_numpy(dtype=float)
    peak = np.maximum.accumulate(values)
    max_dd = float((values / peak - 1.0).min())

    # 新高値（同値以上）を付けたバーの位置。price >= running-peak はここでのみ真になる。
    at_peak = np.flatnonzero(values >= peak)
    max_duration = 0
    recovered = True
    # 回復済み区間の「アンダーウォーター本数」= 新高値バー間隔 − 1（回復バー自身は除く）。
    # 末尾の継続中区間 trailing（= 最終バーまでの本数、最終バーもアンダーウォーター）と
    # 同じ基準（アンダーウォーター実本数）で数えないと、同じ深さの継続中DDが+1のゲタで
    # 隠れて recovered を誤る。
    for prev, nxt in zip(at_peak[:-1], at_peak[1:]):
        span = int(nxt - prev) - 1  # アンダーウォーター本数
        if span >= 1 and span > max_duration:
            max_duration = span
            recovered = True
    # 末尾が最後の新高値以降もアンダーウォーターのままなら継続中区間として計上する。
    last_peak = int(at_peak[-1])
    trailing = (n - 1) - last_peak
    if trailing > max_duration:
        max_duration = trailing
        recovered = False
    return DrawdownStats(max_dd, max_duration, recovered)


def rolling_ann_vol(
    returns: pd.Series, window: int = 21, periods: int = TRADING_DAYS
) -> pd.Series:
    """ローリング年率ボラティリティ $\\sigma_t^{ann} = \\mathrm{std}(r_{t-w+1..t})\\sqrt{252}$。

    窓 ``window`` の標本標準偏差（ddof=1）を年率換算した系列（先頭 ``window-1`` 本は NaN）。
    """
    return returns.rolling(window=window, min_periods=window).std() * np.sqrt(periods)


def vol_regime(
    returns: pd.Series, window: int = 21, periods: int = TRADING_DAYS
) -> tuple[float, float]:
    """直近ボラティリティのレジーム（全期間分布におけるパーセンタイル）。

    過去 ``window`` 日のローリング年率ボラ（:func:`rolling_ann_vol`）を全期間で計算し、
    最新値が過去の分布のどのパーセンタイルに位置するかを返す:

    $$ p = 100 \\cdot \\frac{\\#\\{\\, s : \\sigma_s^{ann} \\le \\sigma_{\\text{latest}}^{ann} \\,\\}}
       {\\#\\{s\\}} $$

    Returns:
        ``(current_vol, percentile)``。``current_vol`` は最新の年率ボラ、``percentile`` は
        0〜100（高いほど過去比で高ボラ局面）。有効なローリング値が無ければ ``(nan, nan)``。
    """
    roll = rolling_ann_vol(returns, window, periods).dropna()
    if len(roll) == 0:
        return float("nan"), float("nan")
    current = float(roll.iloc[-1])
    percentile = float((roll <= current).mean() * 100.0)
    return current, percentile


@dataclass
class RiskResult:
    """1 銘柄のリスク指標一式（:func:`compute_risk` の返り値）。"""

    n: int
    downside_dev: float
    sortino: float
    var95: float
    var99: float
    cvar95: float
    cvar99: float
    max_drawdown: float
    max_dd_duration: int
    dd_recovered: bool
    vol_window: int
    current_vol: float
    vol_percentile: float


def compute_risk(prices: pd.Series, vol_window: int = 21) -> RiskResult:
    """終値系列からリスク指標一式（下方偏差・VaR/ES・ドローダウン・ボラレジーム）を計算する。

    Args:
        prices: 終値の ``pd.Series``（分割・配当調整済みを想定）。
        vol_window: ローリング年率ボラの窓（既定 21 営業日 ≒ 1 ヶ月）。

    Returns:
        :class:`RiskResult`。
    """
    prices = prices.dropna()
    returns = metrics.daily_returns(prices)
    dd = drawdown_stats(prices)
    cur_vol, vol_pct = vol_regime(returns, vol_window)
    return RiskResult(
        n=int(len(returns)),
        downside_dev=downside_deviation(returns),
        sortino=sortino(returns),
        var95=var_historical(returns, 0.95),
        var99=var_historical(returns, 0.99),
        cvar95=cvar_historical(returns, 0.95),
        cvar99=cvar_historical(returns, 0.99),
        max_drawdown=dd.max_drawdown,
        max_dd_duration=dd.max_duration,
        dd_recovered=dd.recovered,
        vol_window=vol_window,
        current_vol=cur_vol,
        vol_percentile=vol_pct,
    )

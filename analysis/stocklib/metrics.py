"""リスク・リターン指標モジュール。

日次リターンを入力とする年率換算指標（年率リターン・ボラティリティ・シャープレシオ等）と、
最大ドローダウン・ベータ・ヒストリカル VaR・相関行列を提供する。
年率換算には営業日数 252 を用いる。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS: int = 252


def daily_returns(prices: pd.Series) -> pd.Series:
    """終値系列から日次単純リターン $r_t = P_t / P_{t-1} - 1$ を計算する（先頭の NaN は除去）。"""
    return (prices / prices.shift(1) - 1.0).dropna()


def ann_return(returns: pd.Series, periods: int = TRADING_DAYS) -> float:
    """幾何平均ベースの年率リターン。

    $$ R_{ann} = \\left( \\prod_t (1 + r_t) \\right)^{N/n} - 1 $$

    （$n$: 観測数、$N$: 年間営業日数 252）
    """
    returns = returns.dropna()
    if len(returns) == 0:
        return float("nan")
    total = float((1.0 + returns).prod())
    if total <= 0:
        return -1.0
    return total ** (periods / len(returns)) - 1.0


def ann_vol(returns: pd.Series, periods: int = TRADING_DAYS) -> float:
    """年率ボラティリティ $\\sigma_{ann} = \\sigma_{daily} \\sqrt{252}$（標本標準偏差）。"""
    returns = returns.dropna()
    if len(returns) < 2:
        return float("nan")
    return float(returns.std() * np.sqrt(periods))


def sharpe(returns: pd.Series, rf_annual: float = 0.0, periods: int = TRADING_DAYS) -> float:
    """シャープレシオ（年率）。

    $$ \\mathrm{Sharpe} = \\frac{\\bar r \\cdot 252 - r_f}{\\sigma_{daily}\\sqrt{252}} $$
    """
    returns = returns.dropna()
    vol = ann_vol(returns, periods)
    if not np.isfinite(vol) or vol == 0.0:
        return float("nan")
    excess = float(returns.mean()) * periods - rf_annual
    return excess / vol


def sortino(returns: pd.Series, rf_annual: float = 0.0, periods: int = TRADING_DAYS) -> float:
    """ソルティノレシオ（年率）。分母は下方偏差（負のリターンのみの二乗平均平方根）。

    $$ \\mathrm{Sortino} = \\frac{\\bar r \\cdot 252 - r_f}
       {\\sqrt{\\frac{1}{n}\\sum_t \\min(r_t, 0)^2} \\cdot \\sqrt{252}} $$
    """
    returns = returns.dropna()
    if len(returns) < 2:
        return float("nan")
    downside = float(np.sqrt(np.mean(np.square(np.minimum(returns.to_numpy(), 0.0)))))
    if downside == 0.0:
        return float("nan")
    excess = float(returns.mean()) * periods - rf_annual
    return excess / (downside * np.sqrt(periods))


def max_drawdown(prices: pd.Series) -> float:
    """最大ドローダウン（負の値で返す）。

    $$ \\mathrm{MaxDD} = \\min_t \\left( \\frac{P_t}{\\max_{s \\le t} P_s} - 1 \\right) $$

    例: 高値から半値まで下落した場合は ``-0.5``。
    """
    prices = prices.dropna()
    if len(prices) == 0:
        return float("nan")
    peak = prices.cummax()
    return float((prices / peak - 1.0).min())


def beta(returns: pd.Series, benchmark_returns: pd.Series) -> float:
    """ベンチマークに対するベータ。

    $$ \\beta = \\frac{\\mathrm{Cov}(r, r_b)}{\\mathrm{Var}(r_b)} $$

    2系列は日付で内部結合してから計算する。
    """
    df = pd.concat([returns, benchmark_returns], axis=1, join="inner").dropna()
    if len(df) < 2:
        return float("nan")
    x = df.iloc[:, 1].to_numpy()
    y = df.iloc[:, 0].to_numpy()
    var_b = float(np.var(x, ddof=1))
    if var_b == 0.0:
        return float("nan")
    cov = float(np.cov(y, x, ddof=1)[0, 1])
    return cov / var_b


def var_historical(returns: pd.Series, level: float = 0.95) -> float:
    """ヒストリカル VaR（日次、負の値で返す）。

    信頼水準 $c$ に対しリターン分布の $(1-c)$ 分位点:

    $$ \\mathrm{VaR}_{c} = Q_{1-c}(r) $$

    例: ``level=0.95`` で ``-0.03`` なら「95%の日は 1 日の損失が 3% 以内」と読む。
    """
    returns = returns.dropna()
    if len(returns) == 0:
        return float("nan")
    return float(returns.quantile(1.0 - level))


def correlation_matrix(returns: pd.DataFrame) -> pd.DataFrame:
    """複数銘柄の日次リターン DataFrame（列=銘柄）からピアソン相関行列を計算する。"""
    return returns.corr()

"""テクニカル指標モジュール。

全関数は ``pd.Series`` / ``pd.DataFrame`` を受け取り、同じインデックスの
``pd.Series`` / ``pd.DataFrame`` を返す純関数。数式は docstring に記載する。
"""

from __future__ import annotations

import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    """単純移動平均（Simple Moving Average）。

    $$ \\mathrm{SMA}_t(n) = \\frac{1}{n} \\sum_{i=0}^{n-1} P_{t-i} $$
    """
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    """指数移動平均（Exponential Moving Average）。

    平滑化係数 $\\alpha = 2/(n+1)$ を用いて

    $$ \\mathrm{EMA}_t = \\alpha P_t + (1-\\alpha)\\,\\mathrm{EMA}_{t-1} $$
    """
    return series.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    """RSI（Relative Strength Index、Wilder 平滑化）。

    値上がり幅 $U_t = \\max(P_t - P_{t-1}, 0)$、値下がり幅 $D_t = \\max(P_{t-1} - P_t, 0)$
    を Wilder 平滑化（$\\alpha = 1/n$ の指数平滑）した平均 $\\bar U, \\bar D$ から

    $$ \\mathrm{RSI} = 100 \\cdot \\frac{\\bar U}{\\bar U + \\bar D} $$

    0〜100 の値。一般に 70 以上で買われすぎ、30 以下で売られすぎとされる。
    """
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    denom = avg_gain + avg_loss
    out = 100.0 * avg_gain / denom
    out[denom == 0] = 50.0  # 変動ゼロの区間は中立値
    out.name = f"RSI{window}"
    return out


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD（Moving Average Convergence Divergence）。

    $$ \\mathrm{MACD}_t = \\mathrm{EMA}_t(\\text{fast}) - \\mathrm{EMA}_t(\\text{slow}) $$
    $$ \\mathrm{Signal}_t = \\mathrm{EMA}(\\mathrm{MACD})_t(\\text{signal}) $$
    $$ \\mathrm{Hist}_t = \\mathrm{MACD}_t - \\mathrm{Signal}_t $$

    Returns:
        列 ``macd`` / ``signal`` / ``hist`` を持つ DataFrame。
    """
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame(
        {"macd": macd_line, "signal": signal_line, "hist": macd_line - signal_line}
    )


def bollinger(series: pd.Series, window: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    """ボリンジャーバンド。

    中心線を $\\mathrm{SMA}(n)$、標準偏差を $\\sigma_t$（標本標準偏差）として

    $$ \\text{upper} = \\mathrm{SMA}_t + k\\sigma_t, \\quad
       \\text{lower} = \\mathrm{SMA}_t - k\\sigma_t $$

    Returns:
        列 ``middle`` / ``upper`` / ``lower`` を持つ DataFrame。
    """
    mid = sma(series, window)
    std = series.rolling(window=window, min_periods=window).std()
    return pd.DataFrame(
        {"middle": mid, "upper": mid + num_std * std, "lower": mid - num_std * std}
    )


def ichimoku(
    df: pd.DataFrame,
    tenkan: int = 9,
    kijun: int = 26,
    senkou_b: int = 52,
) -> pd.DataFrame:
    """一目均衡表。

    ``High`` / ``Low`` / ``Close`` 列を持つ DataFrame を受け取る。

    - 転換線: $(\\max H_9 + \\min L_9)/2$
    - 基準線: $(\\max H_{26} + \\min L_{26})/2$
    - 先行スパン1: $(転換線 + 基準線)/2$ を 26 日先行（未来へシフト）
    - 先行スパン2: $(\\max H_{52} + \\min L_{52})/2$ を 26 日先行
    - 遅行スパン: 終値を 26 日遅行（過去へシフト）

    Returns:
        列 ``tenkan`` / ``kijun`` / ``senkou_a`` / ``senkou_b`` / ``chikou`` を持つ DataFrame。
        先行スパンのシフトにより末尾・先頭に NaN が生じる（インデックスは入力と同一）。
    """

    def _mid(window: int) -> pd.Series:
        hi = df["High"].rolling(window=window, min_periods=window).max()
        lo = df["Low"].rolling(window=window, min_periods=window).min()
        return (hi + lo) / 2.0

    tenkan_line = _mid(tenkan)
    kijun_line = _mid(kijun)
    senkou_a_line = ((tenkan_line + kijun_line) / 2.0).shift(kijun)
    senkou_b_line = _mid(senkou_b).shift(kijun)
    chikou_line = df["Close"].shift(-kijun)
    return pd.DataFrame(
        {
            "tenkan": tenkan_line,
            "kijun": kijun_line,
            "senkou_a": senkou_a_line,
            "senkou_b": senkou_b_line,
            "chikou": chikou_line,
        }
    )


def adx(df: pd.DataFrame, window: int = 14) -> pd.DataFrame:
    """ADX（Average Directional Index、Wilder）と方向性指数 +DI / −DI。

    上昇・下降の変動幅（Directional Movement）から方向性指数を作る:

    $$ +\\mathrm{DM}_t = \\begin{cases} H_t - H_{t-1} & (H_t - H_{t-1} > L_{t-1} - L_t \\ \\wedge\\ >0) \\\\ 0 & \\text{otherwise} \\end{cases} $$

    真の値幅 $\\mathrm{TR}$ を Wilder 平滑化した $\\mathrm{ATR}$ で正規化して
    $+\\mathrm{DI} = 100\\cdot \\mathrm{Wilder}(+\\mathrm{DM})/\\mathrm{ATR}$（−DI も同様）。
    さらに $\\mathrm{DX} = 100\\cdot |{+\\mathrm{DI}} - {-\\mathrm{DI}}| / ({+\\mathrm{DI}} + {-\\mathrm{DI}})$ を
    Wilder 平滑化したものが $\\mathrm{ADX}$。ADX はトレンドの「強さ」（方向は問わない）を表し、
    一般に 25 以上で明確なトレンド、20 未満でトレンドレスとされる。

    Args:
        df: ``High`` / ``Low`` / ``Close`` 列を持つ DataFrame。

    Returns:
        列 ``plus_di`` / ``minus_di`` / ``adx`` を持つ DataFrame（入力と同一 index）。
    """
    high, low, close = df["High"], df["Low"], df["Close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)

    alpha = 1.0 / window
    atr_n = tr.ewm(alpha=alpha, adjust=False, min_periods=window).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=window).mean() / atr_n
    minus_di = 100.0 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=window).mean() / atr_n
    di_sum = plus_di + minus_di
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum.where(di_sum != 0.0)
    adx_line = dx.ewm(alpha=alpha, adjust=False, min_periods=window).mean()
    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "adx": adx_line})


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """ATR（Average True Range、Wilder 平滑化）。

    真の値幅 $\\mathrm{TR}_t = \\max(H_t - L_t,\\ |H_t - C_{t-1}|,\\ |L_t - C_{t-1}|)$ を
    Wilder 平滑化（$\\alpha = 1/n$）した平均:

    $$ \\mathrm{ATR}_t = \\frac{(n-1)\\,\\mathrm{ATR}_{t-1} + \\mathrm{TR}_t}{n} $$

    Args:
        df: ``High`` / ``Low`` / ``Close`` 列を持つ DataFrame。
    """
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out = tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    out.name = f"ATR{window}"
    return out

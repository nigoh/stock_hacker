"""indicators モジュールの数値検証（手計算値との一致）。合成データのみ使用。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stocklib import indicators
from stocklib.data import synthetic_prices


@pytest.fixture()
def ohlcv() -> pd.DataFrame:
    return synthetic_prices("7203", days=300)


def test_sma_hand_calculated() -> None:
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = indicators.sma(s, 3)
    assert np.isnan(out.iloc[0]) and np.isnan(out.iloc[1])
    assert out.iloc[2] == pytest.approx(2.0)  # (1+2+3)/3
    assert out.iloc[4] == pytest.approx(4.0)  # (3+4+5)/3


def test_ema_recursion() -> None:
    s = pd.Series([10.0, 20.0, 30.0])
    out = indicators.ema(s, span=3)  # alpha = 0.5
    assert out.iloc[0] == pytest.approx(10.0)
    assert out.iloc[1] == pytest.approx(15.0)   # 0.5*20 + 0.5*10
    assert out.iloc[2] == pytest.approx(22.5)   # 0.5*30 + 0.5*15


def test_rsi_hand_calculated_window2() -> None:
    # 差分: +1, +1, -1, +1。Wilder 平滑化（alpha=1/2, adjust=False）:
    #   avg_gain: 1, 1, 0.5, 0.75 / avg_loss: 0, 0, 0.5, 0.25
    #   最終 RSI = 100 * 0.75 / (0.75 + 0.25) = 75
    s = pd.Series([1.0, 2.0, 3.0, 2.0, 3.0])
    out = indicators.rsi(s, window=2)
    assert out.iloc[-1] == pytest.approx(75.0)


def test_rsi_bounds_and_extremes(ohlcv: pd.DataFrame) -> None:
    up = pd.Series(np.arange(1.0, 51.0))
    down = pd.Series(np.arange(50.0, 0.0, -1.0))
    assert indicators.rsi(up, 14).iloc[-1] == pytest.approx(100.0)
    assert indicators.rsi(down, 14).iloc[-1] == pytest.approx(0.0)
    vals = indicators.rsi(ohlcv["Close"], 14).dropna()
    assert ((vals >= 0) & (vals <= 100)).all()


def test_macd_definition(ohlcv: pd.DataFrame) -> None:
    close = ohlcv["Close"]
    out = indicators.macd(close, fast=12, slow=26, signal=9)
    expected = indicators.ema(close, 12) - indicators.ema(close, 26)
    pd.testing.assert_series_equal(out["macd"], expected, check_names=False)
    pd.testing.assert_series_equal(
        out["hist"], out["macd"] - out["signal"], check_names=False
    )


def test_bollinger_hand_calculated() -> None:
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = indicators.bollinger(s, window=3, num_std=2.0)
    # 最終窓 [3,4,5]: mean=4, std(標本)=1
    assert out["middle"].iloc[-1] == pytest.approx(4.0)
    assert out["upper"].iloc[-1] == pytest.approx(6.0)
    assert out["lower"].iloc[-1] == pytest.approx(2.0)


def test_ichimoku_structure(ohlcv: pd.DataFrame) -> None:
    out = indicators.ichimoku(ohlcv)
    assert list(out.columns) == ["tenkan", "kijun", "senkou_a", "senkou_b", "chikou"]
    assert len(out) == len(ohlcv)
    # 転換線: 直近9日の (高値max + 安値min) / 2
    hi9 = ohlcv["High"].iloc[-9:].max()
    lo9 = ohlcv["Low"].iloc[-9:].min()
    assert out["tenkan"].iloc[-1] == pytest.approx((hi9 + lo9) / 2)
    # 遅行スパン: 終値を26日過去へシフト → 位置 t の値は t+26 の終値
    assert out["chikou"].iloc[-27] == pytest.approx(ohlcv["Close"].iloc[-1])
    # 先行スパンは26日未来へシフト → 末尾26個より前で NaN でない
    assert out["senkou_a"].iloc[-1] == pytest.approx(
        ((out["tenkan"] + out["kijun"]) / 2).iloc[-27]
    )


def test_atr_constant_range() -> None:
    # 毎日 High-Low=2、ギャップなし → TR は常に 2 → ATR も 2 に一致
    n = 60
    close = pd.Series(np.full(n, 100.0))
    df = pd.DataFrame({"High": close + 1, "Low": close - 1, "Close": close})
    out = indicators.atr(df, window=14)
    assert out.iloc[-1] == pytest.approx(2.0)

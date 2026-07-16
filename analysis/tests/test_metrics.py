"""metrics モジュールの数値検証（手計算値との一致）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stocklib import metrics


def test_daily_returns_hand_calculated() -> None:
    prices = pd.Series([100.0, 110.0, 99.0])
    out = metrics.daily_returns(prices)
    assert len(out) == 2
    assert out.iloc[0] == pytest.approx(0.10)
    assert out.iloc[1] == pytest.approx(-0.10)


def test_ann_return_geometric() -> None:
    # 252日間 毎日 +0.1% → (1.001)^252 - 1
    r = pd.Series(np.full(252, 0.001))
    assert metrics.ann_return(r) == pytest.approx(1.001**252 - 1)
    # 126日で累積 (1.001)^126 → 年率換算しても同じ値
    r_half = pd.Series(np.full(126, 0.001))
    assert metrics.ann_return(r_half) == pytest.approx(1.001**252 - 1)


def test_ann_vol_and_sharpe() -> None:
    r = pd.Series([0.01, -0.01] * 100)
    expected_vol = float(r.std()) * np.sqrt(252)
    assert metrics.ann_vol(r) == pytest.approx(expected_vol)
    expected_sharpe = float(r.mean()) * 252 / expected_vol
    assert metrics.sharpe(r) == pytest.approx(expected_sharpe)
    # 変動ゼロ → NaN
    assert np.isnan(metrics.sharpe(pd.Series([0.0] * 10)))


def test_sortino_uses_downside_only() -> None:
    r = pd.Series([0.02, -0.01, 0.02, -0.01])
    downside = np.sqrt(np.mean([0.0, 0.01**2, 0.0, 0.01**2]))
    expected = float(r.mean()) * 252 / (downside * np.sqrt(252))
    assert metrics.sortino(r) == pytest.approx(expected)


def test_max_drawdown_hand_calculated() -> None:
    prices = pd.Series([100.0, 120.0, 60.0, 90.0])
    # ピーク120 → 60 で -50%
    assert metrics.max_drawdown(prices) == pytest.approx(-0.5)
    # 単調増加ならドローダウンなし
    assert metrics.max_drawdown(pd.Series([1.0, 2.0, 3.0])) == pytest.approx(0.0)


def test_beta_scaled_benchmark() -> None:
    idx = pd.date_range("2025-01-01", periods=100, freq="B")
    rng = np.random.default_rng(0)
    bench = pd.Series(rng.normal(0, 0.01, 100), index=idx)
    stock = 2.0 * bench  # 完全連動でベータ2
    assert metrics.beta(stock, bench) == pytest.approx(2.0)


def test_var_historical_quantile() -> None:
    r = pd.Series(np.arange(-0.10, 0.10, 0.002))  # -10%〜+10% の一様な系列
    out = metrics.var_historical(r, level=0.95)
    assert out == pytest.approx(float(r.quantile(0.05)))
    assert out < 0


def test_correlation_matrix_identity_diagonal() -> None:
    idx = pd.date_range("2025-01-01", periods=50, freq="B")
    rng = np.random.default_rng(1)
    df = pd.DataFrame(
        {"a": rng.normal(0, 0.01, 50), "b": rng.normal(0, 0.01, 50)}, index=idx
    )
    corr = metrics.correlation_matrix(df)
    assert corr.loc["a", "a"] == pytest.approx(1.0)
    assert corr.loc["a", "b"] == pytest.approx(corr.loc["b", "a"])
    assert -1.0 <= corr.loc["a", "b"] <= 1.0

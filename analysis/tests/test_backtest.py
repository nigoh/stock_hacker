"""backtest モジュールの数値検証（手計算値との一致）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stocklib.backtest import (
    BacktestResult,
    ma_cross_signal,
    parameter_sweep,
    rsi_reversal_signal,
    run_backtest,
    split_series,
)
from stocklib.data import synthetic_prices
from stocklib.indicators import rsi


def _series(values: list[float]) -> pd.Series:
    idx = pd.date_range("2025-01-01", periods=len(values), freq="B")
    return pd.Series(values, index=idx)


def test_buy_and_hold_no_cost() -> None:
    prices = _series([100.0, 110.0, 121.0, 133.1])
    positions = pd.Series(1.0, index=prices.index)
    result = run_backtest(prices, positions, cost_bps=0.0)
    # 初日終値でエントリー、以降の日次リターンを全て捕捉: 1.1^3 - 1
    assert result.total_return == pytest.approx(1.1**3 - 1.0)
    assert result.n_trades == 1
    assert result.win_rate == pytest.approx(1.0)


def test_cost_applied_on_position_change() -> None:
    # 2日目終値で買い、4日目終値で売り（シグナルは前日に判定）
    prices = _series([100.0, 100.0, 110.0, 110.0, 110.0])
    positions = pd.Series([1.0, 1.0, 1.0, 0.0, 0.0], index=prices.index)
    cost_bps = 100.0  # 1% 片道
    result = run_backtest(prices, positions, cost_bps=cost_bps)
    # 執行ポジション: [0,1,1,1,0] → 保有リターン: day2 +10%, day3 0%, day4 0%
    # コスト: エントリー時 1% + イグジット時 1%
    expected = (1.0 - 0.01) * 1.10 * 1.0 * (1.0 - 0.01) - 1.0
    assert result.total_return == pytest.approx(expected)
    assert result.n_trades == 1


def test_flat_positions_zero_return() -> None:
    prices = _series([100.0, 90.0, 80.0, 120.0])
    positions = pd.Series(0.0, index=prices.index)
    result = run_backtest(prices, positions, cost_bps=10.0)
    assert result.total_return == pytest.approx(0.0)
    assert result.n_trades == 0
    assert np.isnan(result.win_rate)


def test_max_drawdown_of_strategy_equity() -> None:
    prices = _series([100.0, 100.0, 120.0, 60.0, 60.0])
    positions = pd.Series(1.0, index=prices.index)
    result = run_backtest(prices, positions, cost_bps=0.0)
    # エクイティ: 1, 1, 1.2, 0.6, 0.6 → 最大DD -50%
    assert result.max_drawdown == pytest.approx(-0.5)


def test_win_rate_two_trades() -> None:
    # トレード1: 100→110 (+10%, 勝ち)、トレード2: 100→90 (-10%, 負け)
    prices = _series([100.0, 100.0, 110.0, 110.0, 100.0, 100.0, 90.0, 90.0])
    positions = pd.Series([1.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0], index=prices.index)
    result = run_backtest(prices, positions, cost_bps=0.0)
    assert result.n_trades == 2
    assert result.win_rate == pytest.approx(0.5)


def test_positions_must_be_binary() -> None:
    prices = _series([100.0, 101.0, 102.0])
    positions = pd.Series([0.5, 1.0, 0.0], index=prices.index)
    with pytest.raises(ValueError):
        run_backtest(prices, positions)


def test_ma_cross_signal_values() -> None:
    close = synthetic_prices("7203", days=300)["Close"]
    sig = ma_cross_signal(close, fast=5, slow=20)
    assert set(sig.dropna().unique()).issubset({0.0, 1.0})
    fast_ma = close.rolling(5).mean()
    slow_ma = close.rolling(20).mean()
    expected = (fast_ma > slow_ma).astype(float)
    pd.testing.assert_series_equal(sig, expected, check_names=False)
    with pytest.raises(ValueError):
        ma_cross_signal(close, fast=50, slow=20)


def test_rsi_reversal_signal_state_machine() -> None:
    """ベクトル化実装がループによる素朴な状態機械と一致することを確認する。"""
    close = synthetic_prices("7203", days=400)["Close"]
    sig = rsi_reversal_signal(close, window=14, lower=30.0, upper=50.0)
    assert set(sig.dropna().unique()).issubset({0.0, 1.0})

    r = rsi(close, 14)
    state = 0.0
    expected_values: list[float] = []
    for v in r.to_numpy():
        if not np.isnan(v):
            if v < 30.0:
                state = 1.0
            elif v > 50.0:
                state = 0.0
        expected_values.append(state)
    expected = pd.Series(expected_values, index=close.index)
    pd.testing.assert_series_equal(sig, expected, check_names=False)


def test_rsi_reversal_signal_invalid_params() -> None:
    close = synthetic_prices("7203", days=100)["Close"]
    with pytest.raises(ValueError):
        rsi_reversal_signal(close, lower=60.0, upper=40.0)  # lower >= upper
    with pytest.raises(ValueError):
        rsi_reversal_signal(close, lower=-5.0, upper=50.0)  # 範囲外
    with pytest.raises(ValueError):
        rsi_reversal_signal(close, window=1)  # window が短すぎ


def test_rsi_reversal_backtest_runs_on_synthetic() -> None:
    close = synthetic_prices("6758", days=500)["Close"]
    sig = rsi_reversal_signal(close, window=14, lower=30.0, upper=50.0)
    result = run_backtest(close, sig, cost_bps=10.0)
    assert result.n_days == len(close)
    assert np.isfinite(result.total_return)


def test_split_series_time_order() -> None:
    close = synthetic_prices("7203", days=500)["Close"]
    is_prices, oos_prices = split_series(close, ratio=0.7)
    assert len(is_prices) == 350
    assert len(oos_prices) == 150
    # 時間順序を保ち、重複しない
    assert is_prices.index[-1] < oos_prices.index[0]
    pd.testing.assert_series_equal(pd.concat([is_prices, oos_prices]), close)


def test_split_series_invalid_ratio() -> None:
    close = synthetic_prices("7203", days=100)["Close"]
    for bad in (0.0, 1.0, -0.5, 1.5):
        with pytest.raises(ValueError):
            split_series(close, ratio=bad)
    # 分割後の区間が短すぎる場合もエラー
    short = close.iloc[:3]
    with pytest.raises(ValueError):
        split_series(short, ratio=0.5)


def test_split_backtest_is_oos_stats() -> None:
    close = synthetic_prices("9984", days=500)["Close"]
    is_prices, oos_prices = split_series(close, ratio=0.7)
    is_result = run_backtest(is_prices, ma_cross_signal(is_prices, 5, 20), cost_bps=10.0)
    oos_result = run_backtest(oos_prices, ma_cross_signal(oos_prices, 5, 20), cost_bps=10.0)
    assert is_result.n_days == 350
    assert oos_result.n_days == 150
    assert np.isfinite(is_result.total_return)
    assert np.isfinite(oos_result.total_return)


def test_parameter_sweep_returns_grid_order() -> None:
    close = synthetic_prices("7203", days=400)["Close"]
    grid: list[dict[str, float | int]] = [
        {"fast": 5, "slow": 20},
        {"fast": 10, "slow": 20},
        {"fast": 10, "slow": 30},
    ]
    results = parameter_sweep(close, ma_cross_signal, grid, cost_bps=10.0)
    assert len(results) == len(grid)  # 試行回数 N = グリッドの件数
    for (params, result), expected_params in zip(results, grid):
        assert params == expected_params
        assert isinstance(result, BacktestResult)
    # 各結果は個別実行と一致する
    single = run_backtest(close, ma_cross_signal(close, fast=5, slow=20), cost_bps=10.0)
    assert results[0][1].total_return == pytest.approx(single.total_return)


def test_parameter_sweep_rsi_reversal() -> None:
    close = synthetic_prices("6758", days=400)["Close"]
    grid: list[dict[str, float | int]] = [
        {"window": 14, "lower": 25.0, "upper": 50.0},
        {"window": 14, "lower": 30.0, "upper": 50.0},
    ]
    results = parameter_sweep(close, rsi_reversal_signal, grid, cost_bps=10.0)
    assert len(results) == 2
    assert all(np.isfinite(r.total_return) for _, r in results)


def test_t_stat_positive_for_upward_drift() -> None:
    idx = pd.date_range("2024-01-01", periods=300, freq="B")
    prices = pd.Series(100.0 * 1.002 ** np.arange(300), index=idx)
    rng = np.random.default_rng(0)
    prices = prices * np.exp(rng.normal(0, 0.001, 300))
    positions = pd.Series(1.0, index=idx)
    result = run_backtest(prices, positions, cost_bps=0.0)
    assert result.t_stat > 2.0
    assert "有意" in result.t_stat_interpretation

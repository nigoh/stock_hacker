"""seasonality モジュールと seasonality_report CLI のテスト（決定論・ネットワーク不使用）。

既知の月別・曜日別・月内パターンを埋め込んだ人工価格系列で集計値を検証し、
CLI は --synthetic スモーク（RESULT 行・returncode 0）で確認する。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stocklib import seasonality

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# 人工系列ビルダー
# --------------------------------------------------------------------------

def _const_month_series(returns_by_month: dict[int, float], years: int) -> pd.Series:
    """各暦月の月次リターンが returns_by_month で決まる、月内一定の Close 系列を作る。

    1999-12 を基準（リターンなし）に、以降の各月の水準を
    ``level[m] = level[m-1] * (1 + returns_by_month.get(month, 0))`` で決め、
    その月の全営業日に同じ水準を割り当てる。月末終値どうしの月次リターンが
    returns_by_month を厳密に再現する。各月 1月〜12月は `years` 回ずつ現れる。
    """
    idx = pd.bdate_range(start="1999-12-01", end=f"{1999 + years}-12-31")
    period = idx.to_period("M")
    months = list(pd.PeriodIndex(period).unique())
    level: dict[object, float] = {}
    cur = 100.0
    for i, p in enumerate(months):
        if i > 0:
            cur = cur * (1.0 + returns_by_month.get(p.month, 0.0))
        level[p] = cur
    return pd.Series([level[p] for p in period], index=idx)


def _weekday_series(factor_by_weekday: dict[int, float], years: int) -> pd.Series:
    """各営業日の日次リターンが曜日で決まる Close 系列を作る（累積積）。"""
    idx = pd.bdate_range(start="2018-01-01", end=f"{2018 + years - 1}-12-31")
    factors = np.array([1.0 + factor_by_weekday.get(d.dayofweek, 0.0) for d in idx])
    close = 100.0 * np.cumprod(factors)
    return pd.Series(close, index=idx)


# --------------------------------------------------------------------------
# 月別効果
# --------------------------------------------------------------------------

def test_monthly_effect_known_pattern() -> None:
    # 1月 +10%、7月 -5%、他 0% を6年分。各月 n=6。
    series = _const_month_series({1: 0.10, 7: -0.05}, years=6)
    stats = seasonality.monthly_effect(series)
    assert len(stats) == 12
    by_month = {i + 1: s for i, s in enumerate(stats)}

    jan = by_month[1]
    assert jan.n == 6
    assert jan.mean_return == pytest.approx(0.10)
    assert jan.win_rate == pytest.approx(1.0)
    assert jan.std == pytest.approx(0.0, abs=1e-12)  # 全て同一値

    jul = by_month[7]
    assert jul.n == 6
    assert jul.mean_return == pytest.approx(-0.05)
    assert jul.win_rate == pytest.approx(0.0)

    mar = by_month[3]
    assert mar.n == 6
    assert mar.mean_return == pytest.approx(0.0, abs=1e-12)
    assert mar.win_rate == pytest.approx(0.0)  # リターン 0 は「勝ち」に数えない


def test_monthly_returns_index_is_month_end() -> None:
    series = _const_month_series({1: 0.10}, years=3)
    mret = seasonality.monthly_returns(series)
    # 先頭月（1999-12）は前月が無く除外されるので、最初の観測は 2000-01。
    assert mret.index[0].month == 1
    assert mret.index[0].year == 2000


# --------------------------------------------------------------------------
# 曜日効果
# --------------------------------------------------------------------------

def test_weekday_effect_known_pattern() -> None:
    # 月曜 +1%、金曜 -1%、他 0% を2年分。
    series = _weekday_series({0: 0.01, 4: -0.01}, years=2)
    stats = seasonality.weekday_effect(series)
    assert len(stats) == 5
    labels = [s.label for s in stats]
    assert labels == ["月", "火", "水", "木", "金"]

    mon, fri = stats[0], stats[4]
    assert mon.mean_return == pytest.approx(0.01)
    assert mon.win_rate == pytest.approx(1.0)
    assert fri.mean_return == pytest.approx(-0.01)
    assert fri.win_rate == pytest.approx(0.0)
    # 水曜は 0
    assert stats[2].mean_return == pytest.approx(0.0, abs=1e-12)
    # 各曜日の n はおよそ 104（2年 × 約52週）で正
    assert all(s.n > 90 for s in stats)


# --------------------------------------------------------------------------
# 月内（月初/月末）効果
# --------------------------------------------------------------------------

def test_turn_of_month_window_vs_middle() -> None:
    # 2020-12-31（捨て日: 日次リターンの先頭で除外される）＋ 2021-01 の12営業日。
    # 12日: 窓（first2/last2）= order 0,1,10,11 に +2%、月中 order 2..9 に +0.5%。
    prior = pd.Timestamp("2020-12-31")
    jan = pd.bdate_range(start="2021-01-01", periods=12)
    idx = pd.DatetimeIndex([prior, *jan])
    rets = np.zeros(12)
    for k in range(12):
        rets[k] = 0.02 if (k < 2 or k >= 10) else 0.005
    close = np.empty(13)
    close[0] = 100.0
    close[1:] = 100.0 * np.cumprod(1.0 + rets)
    series = pd.Series(close, index=idx)

    tom = seasonality.turn_of_month_effect(series, first_days=2, last_days=2)
    assert tom.tom.n == 4
    assert tom.rest.n == 8
    assert tom.tom.mean_return == pytest.approx(0.02)
    assert tom.rest.mean_return == pytest.approx(0.005)
    assert tom.edge == pytest.approx(0.015)


def test_turn_of_month_partition_is_exhaustive() -> None:
    # 一定日次リターンなら窓内・窓外の平均は等しく、標本数の合計は全日次数に一致。
    idx = pd.bdate_range(start="2020-01-01", end="2021-12-31")
    close = 100.0 * np.cumprod(np.full(len(idx), 1.001))
    series = pd.Series(close, index=idx)
    n_days = len(seasonality.daily_returns(series))
    tom = seasonality.turn_of_month_effect(series, first_days=3, last_days=3)
    assert tom.tom.n + tom.rest.n == n_days
    assert tom.tom.mean_return == pytest.approx(tom.rest.mean_return)
    assert tom.edge == pytest.approx(0.0, abs=1e-12)


def test_turn_of_month_negative_raises() -> None:
    series = _weekday_series({0: 0.01}, years=1)
    with pytest.raises(ValueError):
        seasonality.turn_of_month_effect(series, first_days=-1)


# --------------------------------------------------------------------------
# 半期効果（Sell in May）
# --------------------------------------------------------------------------

def test_sell_in_may_split() -> None:
    # 1月 +10%（winter 側）、7月 -5%（summer 側）、他 0%。各月 n=6。
    series = _const_month_series({1: 0.10, 7: -0.05}, years=6)
    winter, summer = seasonality.sell_in_may(series)
    # winter = {11,12,1,2,3,4}: Jan だけ +0.10、他 0 → 平均 = 0.10/6
    assert winter.n == 6 * 6
    assert winter.mean_return == pytest.approx(0.10 / 6)
    # summer = {5..10}: Jul だけ -0.05 → 平均 = -0.05/6
    assert summer.n == 6 * 6
    assert summer.mean_return == pytest.approx(-0.05 / 6)

    result = seasonality.compute_seasonality(series)
    assert result.sell_in_may_edge == pytest.approx(0.10 / 6 - (-0.05 / 6))


# --------------------------------------------------------------------------
# compute_seasonality 全体
# --------------------------------------------------------------------------

def test_compute_seasonality_bundle() -> None:
    series = _const_month_series({1: 0.10}, years=5)
    result = seasonality.compute_seasonality(series)
    assert len(result.monthly) == 12
    assert len(result.weekday) == 5
    assert result.turn_of_month is not None
    assert result.n_months > 0
    assert result.n_days > 0
    assert result.years >= 5
    assert result.start is not None and result.end is not None


def test_dataframe_input_uses_close_column() -> None:
    series = _const_month_series({1: 0.10}, years=2)
    df = pd.DataFrame({"Open": series * 0.99, "Close": series})
    result = seasonality.compute_seasonality(df)
    assert result.monthly[0].mean_return == pytest.approx(0.10)


def test_missing_close_column_raises() -> None:
    df = pd.DataFrame({"Open": [1.0, 2.0]}, index=pd.date_range("2020-01-01", periods=2))
    with pytest.raises(ValueError):
        seasonality.compute_seasonality(df)


# --------------------------------------------------------------------------
# CLI スモーク
# --------------------------------------------------------------------------

def test_cli_synthetic_smoke() -> None:
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "analysis" / "seasonality_report.py"),
         "7203", "--synthetic", "--period", "5y"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert res.returncode == 0, res.stderr
    last = res.stdout.strip().splitlines()[-1]
    assert last.startswith("RESULT months=") and "data=synthetic" in last
    assert "years=" in last

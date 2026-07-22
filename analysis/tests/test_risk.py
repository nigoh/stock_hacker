"""risk モジュールと risk_report CLI のテスト（決定論・ネットワーク不使用）。

純関数は手計算値との一致で検証し、CLI は --synthetic のスモーク（RESULT 行・exit 0）で検証する。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stocklib import metrics, risk

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# 下方偏差
# --------------------------------------------------------------------------

def test_downside_deviation_hand_calculated() -> None:
    r = pd.Series([0.02, -0.01, 0.02, -0.01])
    # 下方のみ: [0, -0.01, 0, -0.01] → RMS = sqrt((0.01^2 + 0.01^2)/4)
    daily = np.sqrt((0.01**2 + 0.01**2) / 4)
    expected = daily * np.sqrt(252)
    assert risk.downside_deviation(r) == pytest.approx(expected)


def test_downside_deviation_no_downside_is_zero() -> None:
    r = pd.Series([0.01, 0.02, 0.03])
    assert risk.downside_deviation(r) == pytest.approx(0.0)


def test_downside_deviation_matches_sortino_denominator() -> None:
    # 下方偏差はソルティノの分母（年率）に一致する。
    r = pd.Series([0.02, -0.01, 0.015, -0.02, 0.01, -0.005])
    dd = risk.downside_deviation(r)
    expected_sortino = float(r.mean()) * 252 / dd
    assert metrics.sortino(r) == pytest.approx(expected_sortino)


# --------------------------------------------------------------------------
# VaR / CVaR（ES）
# --------------------------------------------------------------------------

def test_var_and_cvar_hand_calculated() -> None:
    # -0.10 から 0.10 まで 0.01 刻みの 21 点。q*(n-1) が整数になるよう設計。
    r = pd.Series([-0.10 + 0.01 * i for i in range(21)])
    # 95%: quantile(0.05) は 0.05*20=1 → idx1 = -0.09。ES は {-0.10, -0.09} の平均。
    assert risk.var_historical(r, 0.95) == pytest.approx(-0.09)
    assert risk.cvar_historical(r, 0.95) == pytest.approx(-0.095)
    # 99%: quantile(0.01) は 0.01*20=0.2 → -0.10 と -0.09 の内挿 = -0.098。
    assert risk.var_historical(r, 0.99) == pytest.approx(-0.098)
    # ES は threshold 以下 = {-0.10} のみ → -0.10。
    assert risk.cvar_historical(r, 0.99) == pytest.approx(-0.10)


def test_cvar_never_above_var() -> None:
    rng = np.random.default_rng(0)
    r = pd.Series(rng.normal(0, 0.02, 500))
    for level in (0.95, 0.99):
        assert risk.cvar_historical(r, level) <= risk.var_historical(r, level)


def test_cvar_reexports_var_historical() -> None:
    # risk.var_historical は metrics のものを再エクスポートしている。
    assert risk.var_historical is metrics.var_historical
    assert risk.sortino is metrics.sortino


# --------------------------------------------------------------------------
# ドローダウンと継続日数
# --------------------------------------------------------------------------

def test_drawdown_stats_recovered() -> None:
    prices = pd.Series([100.0, 120.0, 60.0, 90.0, 130.0])
    s = risk.drawdown_stats(prices)
    assert s.max_drawdown == pytest.approx(-0.5)      # 120 → 60
    # ピーク(idx1=120)から回復(idx4=130)まで 3 営業日。
    assert s.max_duration == 3
    assert s.recovered is True


def test_drawdown_stats_ongoing_not_recovered() -> None:
    prices = pd.Series([100.0, 120.0, 60.0, 90.0])  # 120 を回復しないまま終了
    s = risk.drawdown_stats(prices)
    assert s.max_drawdown == pytest.approx(-0.5)
    assert s.max_duration == 2       # idx1 → 末尾(idx3) の 2 バー
    assert s.recovered is False


def test_drawdown_stats_monotonic_up_has_no_drawdown() -> None:
    s = risk.drawdown_stats(pd.Series([1.0, 2.0, 3.0, 4.0]))
    assert s.max_drawdown == pytest.approx(0.0)
    assert s.max_duration == 0
    assert s.recovered is True


def test_drawdown_series_matches_metrics_max() -> None:
    prices = pd.Series([100.0, 120.0, 60.0, 90.0, 130.0])
    assert risk.drawdown_series(prices).min() == pytest.approx(metrics.max_drawdown(prices))


# --------------------------------------------------------------------------
# ボラティリティ・レジーム
# --------------------------------------------------------------------------

def test_rolling_ann_vol_hand_calculated() -> None:
    r = pd.Series([0.01, -0.01, 0.01, -0.01])
    roll = risk.rolling_ann_vol(r, window=2)
    # 各 2 本窓の標本標準偏差（ddof=1）は sqrt(0.0002) で一定。
    expected = np.sqrt(((0.01 - 0.0) ** 2 + (-0.01 - 0.0) ** 2) / 1) * np.sqrt(252)
    assert roll.dropna().iloc[0] == pytest.approx(expected)
    assert roll.iloc[:1].isna().all()  # 先頭 window-1 本は NaN


def test_vol_regime_all_equal_is_top_percentile() -> None:
    r = pd.Series([0.01, -0.01, 0.01, -0.01])
    cur, pct = risk.vol_regime(r, window=2)
    assert pct == pytest.approx(100.0)  # 全て等しい → 最新値は分布の 100 パーセンタイル
    assert cur > 0


def test_vol_regime_low_latest_is_low_percentile() -> None:
    # 末尾の窓だけボラが小さい → 最新は最小値 → 低パーセンタイル。
    r = pd.Series([0.02, -0.02, 0.02, -0.02, 0.0])
    cur, pct = risk.vol_regime(r, window=2)
    # ローリング値は 4 本、最新が唯一の最小 → 1/4 = 25%。
    assert pct == pytest.approx(25.0)
    roll = risk.rolling_ann_vol(r, window=2).dropna()
    assert cur == pytest.approx(float(roll.min()))


def test_vol_regime_insufficient_data_is_nan() -> None:
    cur, pct = risk.vol_regime(pd.Series([0.01]), window=21)
    assert np.isnan(cur) and np.isnan(pct)


# --------------------------------------------------------------------------
# compute_risk（統合）
# --------------------------------------------------------------------------

def test_compute_risk_fields() -> None:
    idx = pd.date_range("2024-01-01", periods=300, freq="B")
    rng = np.random.default_rng(3)
    prices = pd.Series(100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 300))), index=idx)
    res = risk.compute_risk(prices, vol_window=21)
    assert res.n == 299
    assert res.max_drawdown <= 0.0
    assert res.var99 <= res.var95 <= 0.0
    assert res.cvar95 <= res.var95
    assert 0.0 <= res.vol_percentile <= 100.0
    assert res.max_dd_duration >= 0


# --------------------------------------------------------------------------
# CLI スモーク
# --------------------------------------------------------------------------

def test_cli_synthetic_smoke() -> None:
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "analysis" / "risk_report.py"), "7203", "--synthetic"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert res.returncode == 0, res.stderr
    last = res.stdout.strip().splitlines()[-1]
    assert last.startswith("RESULT var95=") and "maxdd=" in last and "data=synthetic" in last


def test_cli_bad_vol_window_exits_1() -> None:
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "analysis" / "risk_report.py"),
         "7203", "--synthetic", "--vol-window", "1"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert res.returncode == 1

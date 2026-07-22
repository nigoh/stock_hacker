"""pairs モジュールと pairs_screen CLI のテスト（numpy のみ・ネットワーク不使用）。"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stocklib import pairs

REPO_ROOT = Path(__file__).resolve().parents[2]


def _price_df(values: np.ndarray) -> pd.DataFrame:
    idx = pd.date_range(end=dt.date(2026, 6, 30), periods=len(values), freq="B")
    return pd.DataFrame({"Close": values}, index=idx)


def test_ols_recovers_line() -> None:
    x = np.linspace(0, 10, 50)
    y = 3.0 + 2.0 * x
    intercept, slope, resid = pairs._ols(y, x)
    assert intercept == pytest.approx(3.0, abs=1e-9)
    assert slope == pytest.approx(2.0, abs=1e-9)
    assert np.allclose(resid, 0.0, atol=1e-9)


def test_df_stat_mean_reverting_vs_random_walk() -> None:
    rng = np.random.default_rng(42)
    n = 500
    # OU（平均回帰）: s_t = 0.9 s_{t-1} + eps → b = -0.1
    ou = np.zeros(n)
    eps = rng.normal(0, 1.0, n)
    for t in range(1, n):
        ou[t] = 0.9 * ou[t - 1] + eps[t]
    df_ou, hl_ou = pairs._df_stat_and_half_life(ou)
    # ランダムウォーク: s_t = s_{t-1} + eps → 単位根
    rw = np.cumsum(rng.normal(0, 1.0, n))
    df_rw, hl_rw = pairs._df_stat_and_half_life(rw)

    assert df_ou < pairs.DF_CRIT_1PCT      # 強く平均回帰的
    assert hl_ou > 0 and np.isfinite(hl_ou)
    assert df_ou < df_rw                    # OU の方が定常的（より負）
    assert df_rw > pairs.DF_CRIT_5PCT       # RW は平均回帰と判定されない


def test_df_stat_half_life_short_series_nan() -> None:
    df_stat, hl = pairs._df_stat_and_half_life(np.arange(10.0))
    assert np.isnan(df_stat) and np.isnan(hl)


def test_analyze_pair_cointegrated() -> None:
    rng = np.random.default_rng(7)
    n = 400
    common = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))  # 共通の確率トレンド
    a = common * np.exp(rng.normal(0, 0.003, n))
    b = common * np.exp(rng.normal(0, 0.003, n))
    result = pairs.analyze_pair(
        pd.Series(a), pd.Series(b), code_a="A", code_b="B", sector_a="X", sector_b="X"
    )
    assert result is not None
    assert result.n == n
    assert result.corr > 0.5                   # 共通トレンドで高相関
    assert result.same_sector is True
    assert np.isfinite(result.df_stat)


def test_analyze_pair_too_short_returns_none() -> None:
    a = _price_df(np.full(10, 100.0))["Close"]
    b = _price_df(np.full(10, 100.0))["Close"]
    assert pairs.analyze_pair(a, b) is None


def test_find_pairs_orders_and_filters() -> None:
    rng = np.random.default_rng(1)
    n = 300
    common = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    prices = {
        "A": _price_df(common * np.exp(rng.normal(0, 0.003, n))),
        "B": _price_df(common * np.exp(rng.normal(0, 0.003, n))),
        "C": _price_df(100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))),  # 独立
    }
    sectors = {"A": "銀行", "B": "銀行", "C": "電機"}
    results = pairs.find_pairs(prices, sectors=sectors, min_overlap=250)
    assert len(results) == 3  # 3ペア（A-B, A-C, B-C）
    # DF 統計量の昇順
    stats = [r.df_stat for r in results]
    assert stats == sorted(stats)

    same = pairs.find_pairs(prices, sectors=sectors, min_overlap=250, same_sector_only=True)
    assert len(same) == 1 and {same[0].code_a, same[0].code_b} == {"A", "B"}


def test_find_pairs_min_overlap_excludes() -> None:
    prices = {"A": _price_df(np.full(100, 100.0)), "B": _price_df(np.full(100, 100.0))}
    # 100 営業日 < min_overlap 250 → 除外
    assert pairs.find_pairs(prices, min_overlap=250) == []


def test_is_mean_reverting_flag() -> None:
    r = pairs.PairResult("A", "B", "", "", "", "", 300, 0.9, 1.0, -3.5, 10.0, 2.0)
    assert r.is_mean_reverting() is True
    r2 = pairs.PairResult("A", "B", "", "", "", "", 300, 0.9, 1.0, -1.0, float("nan"), 0.5)
    assert r2.is_mean_reverting() is False


def test_cli_synthetic_smoke() -> None:
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "analysis" / "pairs_screen.py"),
         "--synthetic", "--top", "5",
         "--universe", str(REPO_ROOT / "analysis" / "universe" / "liquid30.csv")],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert res.returncode == 0, res.stderr
    last = res.stdout.strip().splitlines()[-1]
    assert last.startswith("RESULT pairs=") and "data=synthetic" in last

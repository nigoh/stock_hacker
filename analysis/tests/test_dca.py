"""積立（ドルコスト平均法）バックテストの数値検証（手計算値との一致）と CLI スモーク。"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stocklib.backtest import (
    DCAComparison,
    compare_dca_lump_sum,
    dca_backtest,
    dca_schedule,
    lump_sum_backtest,
)
from stocklib.data import synthetic_prices

REPO_ROOT = Path(__file__).resolve().parents[2]
TODAY = dt.date.today().isoformat()


def _bday_series(start: str, end: str, price_fn) -> pd.Series:
    """営業日（月〜金）のインデックスに price_fn(Timestamp) の価格を載せた系列を作る。"""
    idx = pd.date_range(start, end, freq="B")
    return pd.Series([price_fn(ts) for ts in idx], index=idx, dtype=float)


# ---------------------------------------------------------------------------
# dca_schedule: 買付日の割り当て
# ---------------------------------------------------------------------------


def test_schedule_rolls_to_next_business_day() -> None:
    idx = pd.date_range("2025-01-01", "2025-03-31", freq="B")
    dates = dca_schedule(idx, day_of_month=1)
    # 2025-01-01 は水曜（freq="B" では営業日）、2/1・3/1 は土曜 → 翌営業日（月曜）に繰越
    expected = pd.DatetimeIndex(["2025-01-01", "2025-02-03", "2025-03-03"])
    pd.testing.assert_index_equal(dates, expected)


def test_schedule_skips_holiday_in_index() -> None:
    """インデックスに無い日（休場日扱い）は翌営業日に繰り越される。"""
    idx = pd.date_range("2025-01-01", "2025-02-28", freq="B")
    idx = idx[idx != pd.Timestamp("2025-02-03")]  # 2/1(土)の繰越先の月曜を休場にする
    dates = dca_schedule(idx, day_of_month=1)
    expected = pd.DatetimeIndex(["2025-01-01", "2025-02-04"])
    pd.testing.assert_index_equal(dates, expected)


def test_schedule_clips_day_to_month_end() -> None:
    """月に存在しない日（例: 2月31日）は月末日に丸めてから繰越判定する。"""
    idx = pd.date_range("2025-01-01", "2025-04-30", freq="B")
    dates = dca_schedule(idx, day_of_month=31)
    # 1/31(金) → そのまま。2月は28日(金)。3/31(月) → そのまま。4月は30日(水)。
    expected = pd.DatetimeIndex(["2025-01-31", "2025-02-28", "2025-03-31", "2025-04-30"])
    pd.testing.assert_index_equal(dates, expected)


def test_schedule_target_before_series_start_rolls_to_first_day() -> None:
    """系列開始前の目標日は系列初日に繰り越される（重複はしない）。"""
    idx = pd.date_range("2025-01-15", "2025-02-28", freq="B")
    dates = dca_schedule(idx, day_of_month=1)
    expected = pd.DatetimeIndex(["2025-01-15", "2025-02-03"])
    pd.testing.assert_index_equal(dates, expected)


def test_schedule_invalid_day() -> None:
    idx = pd.date_range("2025-01-01", "2025-03-31", freq="B")
    for bad in (0, 32, -1):
        with pytest.raises(ValueError):
            dca_schedule(idx, day_of_month=bad)


# ---------------------------------------------------------------------------
# dca_backtest: 手計算一致
# ---------------------------------------------------------------------------


def _step_prices() -> pd.Series:
    """1月=100円、2月=200円、3月=100円の階段状の系列（営業日）。"""
    def price(ts: pd.Timestamp) -> float:
        return {1: 100.0, 2: 200.0, 3: 100.0}[ts.month]

    return _bday_series("2025-01-01", "2025-03-31", price)


def test_dca_hand_computed_no_cost() -> None:
    close = _step_prices()
    res = dca_backtest(close, monthly_amount=10_000.0, day_of_month=1, cost_bps=0.0)
    # 買付: 1/1@100 → 100株、2/3@200 → 50株、3/3@100 → 100株
    assert res.n_buys == 3
    assert res.total_invested == pytest.approx(30_000.0)
    assert res.total_shares == pytest.approx(250.0)
    assert res.avg_cost == pytest.approx(30_000.0 / 250.0)  # = 120円
    assert res.avg_buy_price == pytest.approx((100.0 + 200.0 + 100.0) / 3.0)
    # 調和平均効果: 平均取得単価 <= 買付価格の単純平均
    assert res.avg_cost <= res.avg_buy_price
    # 最終評価額: 250株 × 100円
    assert res.final_value == pytest.approx(25_000.0)
    assert res.total_return == pytest.approx(25_000.0 / 30_000.0 - 1.0)


def test_dca_curves_hand_computed() -> None:
    close = _step_prices()
    res = dca_backtest(close, monthly_amount=10_000.0, day_of_month=1, cost_bps=0.0)
    # 1月末（1/31）: 投資1万・100株・価格100円 → 評価1万・損益0%・取得単価100円
    jan_end = pd.Timestamp("2025-01-31")
    assert res.invested_curve.loc[jan_end] == pytest.approx(10_000.0)
    assert res.value_curve.loc[jan_end] == pytest.approx(10_000.0)
    assert res.pnl_curve.loc[jan_end] == pytest.approx(0.0)
    assert res.avg_cost_curve.loc[jan_end] == pytest.approx(100.0)
    # 2月末（2/28）: 投資2万・150株・価格200円 → 評価3万・損益+50%
    feb_end = pd.Timestamp("2025-02-28")
    assert res.invested_curve.loc[feb_end] == pytest.approx(20_000.0)
    assert res.value_curve.loc[feb_end] == pytest.approx(30_000.0)
    assert res.pnl_curve.loc[feb_end] == pytest.approx(0.5)
    assert res.avg_cost_curve.loc[feb_end] == pytest.approx(20_000.0 / 150.0)
    # 損益率のピーク +50% → 3月に -16.67% まで下落 → 下落幅 -66.67%pt
    assert res.min_pnl == pytest.approx(25_000.0 / 30_000.0 - 1.0)
    assert res.max_drawdown_pp == pytest.approx((25_000.0 / 30_000.0 - 1.0) - 0.5)


def test_dca_cost_reduces_shares_not_invested() -> None:
    close = _step_prices()
    res = dca_backtest(close, monthly_amount=10_000.0, day_of_month=1, cost_bps=100.0)
    # 片道1%: 各買付の株数は (10000 × 0.99) / P。累計投資額はコスト込みの拠出額
    assert res.total_invested == pytest.approx(30_000.0)
    assert res.total_shares == pytest.approx(0.99 * 250.0)
    assert res.avg_cost == pytest.approx(30_000.0 / (0.99 * 250.0))
    # コスト分だけ取得単価は割高になる
    no_cost = dca_backtest(close, monthly_amount=10_000.0, day_of_month=1, cost_bps=0.0)
    assert res.avg_cost > no_cost.avg_cost


def test_dca_pnl_nan_before_first_buy() -> None:
    """買付日より前は損益率・平均取得単価が NaN（投資額ゼロ）になる。"""
    close = _bday_series("2025-01-06", "2025-02-27", lambda ts: 100.0)
    res = dca_backtest(close, monthly_amount=10_000.0, day_of_month=15, cost_bps=0.0)
    assert np.isnan(res.pnl_curve.loc[pd.Timestamp("2025-01-06")])
    assert np.isnan(res.avg_cost_curve.loc[pd.Timestamp("2025-01-06")])
    assert res.invested_curve.loc[pd.Timestamp("2025-01-06")] == pytest.approx(0.0)
    first_buy = res.buy_prices.index[0]
    assert first_buy == pd.Timestamp("2025-01-15")
    assert res.pnl_curve.loc[first_buy] == pytest.approx(0.0)


def test_dca_accepts_dataframe_close_column() -> None:
    df = synthetic_prices("7203", days=500)
    res_df = dca_backtest(df, monthly_amount=30_000.0)
    res_sr = dca_backtest(df["Close"], monthly_amount=30_000.0)
    assert res_df.final_value == pytest.approx(res_sr.final_value)
    assert res_df.n_buys == res_sr.n_buys


def test_dca_invalid_params() -> None:
    close = _step_prices()
    with pytest.raises(ValueError):
        dca_backtest(close, monthly_amount=0.0)
    with pytest.raises(ValueError):
        dca_backtest(close, monthly_amount=-100.0)
    with pytest.raises(ValueError):
        dca_backtest(close, monthly_amount=10_000.0, day_of_month=0)
    with pytest.raises(ValueError):
        dca_backtest(close, monthly_amount=10_000.0, cost_bps=-1.0)
    with pytest.raises(ValueError):
        dca_backtest(pd.Series(dtype=float), monthly_amount=10_000.0)


# ---------------------------------------------------------------------------
# lump_sum_backtest / compare_dca_lump_sum
# ---------------------------------------------------------------------------


def test_lump_sum_hand_computed() -> None:
    close = _bday_series("2025-01-01", "2025-03-31", lambda ts: 100.0 if ts.month < 3 else 120.0)
    res = lump_sum_backtest(close, amount=30_000.0, cost_bps=0.0)
    assert res.total_shares == pytest.approx(300.0)
    assert res.avg_cost == pytest.approx(100.0)
    assert res.final_value == pytest.approx(36_000.0)
    assert res.total_return == pytest.approx(0.2)
    assert res.min_pnl == pytest.approx(0.0)
    assert (res.invested_curve == 30_000.0).all()


def test_lump_sum_cost() -> None:
    close = _bday_series("2025-01-01", "2025-01-31", lambda ts: 100.0)
    res = lump_sum_backtest(close, amount=10_000.0, cost_bps=100.0)
    assert res.total_shares == pytest.approx(99.0)
    assert res.total_return == pytest.approx(-0.01)  # コスト分の負け


def test_compare_uses_same_total_invested() -> None:
    df = synthetic_prices("7203", days=750)
    cmp = compare_dca_lump_sum(df, monthly_amount=30_000.0, day_of_month=1, cost_bps=10.0)
    assert isinstance(cmp, DCAComparison)
    assert cmp.lump_sum.amount == pytest.approx(cmp.dca.total_invested)
    assert cmp.lump_sum.cost_bps == cmp.dca.cost_bps
    assert np.isfinite(cmp.dca.total_return)
    assert np.isfinite(cmp.lump_sum.total_return)
    # 時系列は同じインデックスを共有する
    pd.testing.assert_index_equal(cmp.dca.value_curve.index, cmp.lump_sum.value_curve.index)


def test_compare_lump_sum_wins_in_monotonic_uptrend() -> None:
    """単調上昇の系列では期初一括が積立を上回る（資金を早く晒すため）。"""
    idx = pd.date_range("2023-01-02", periods=500, freq="B")
    close = pd.Series(100.0 * 1.001 ** np.arange(500), index=idx)
    cmp = compare_dca_lump_sum(close, monthly_amount=10_000.0, cost_bps=0.0)
    assert cmp.lump_sum.final_value > cmp.dca.final_value


def test_compare_dca_wins_when_early_crash_then_recovery() -> None:
    """期初に急落しその後回復する系列では積立が有利になる（安値で多く買うため）。"""
    def price(ts: pd.Timestamp) -> float:
        return {1: 100.0, 2: 50.0, 3: 50.0, 4: 100.0}[ts.month]

    close = _bday_series("2025-01-01", "2025-04-30", price)
    cmp = compare_dca_lump_sum(close, monthly_amount=10_000.0, cost_bps=0.0)
    assert cmp.dca.final_value > cmp.lump_sum.final_value
    # 一括は行って来い（損益0）、積立は安値買付分がプラス
    assert cmp.lump_sum.total_return == pytest.approx(0.0)
    assert cmp.dca.total_return > 0.0


# ---------------------------------------------------------------------------
# CLI スモーク（subprocess + --synthetic、ネットワーク不使用）
# ---------------------------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_run_backtest_cli_dca() -> None:
    proc = _run(
        "analysis/run_backtest.py",
        "--strategy", "dca",
        "--code", "7203",
        "--monthly", "30000",
        "--period", "2y",
        "--synthetic",
    )
    assert proc.returncode == 0, proc.stderr
    assert "積立シミュレーション結果" in proc.stdout
    assert "期初一括" in proc.stdout
    assert "合成データ" in proc.stdout
    report_path = REPO_ROOT / "reports" / f"backtest-dca-7203-{TODAY}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "免責事項" in content
    assert "long-term-wealth-building.md" in content
    assert "行動の継続性" in content


def test_run_backtest_cli_dca_rejects_sweep_and_split() -> None:
    for extra in (["--sweep"], ["--split", "0.7"], ["--in-currency", "USD"]):
        proc = _run(
            "analysis/run_backtest.py",
            "--strategy", "dca",
            "--code", "7203",
            "--synthetic",
            *extra,
        )
        assert proc.returncode == 1, proc.stdout
        assert "エラー" in proc.stderr


def test_run_backtest_cli_ma_cross_still_works() -> None:
    """dca 追加後も既存の signal 系戦略の CLI 契約が壊れていないこと。"""
    proc = _run(
        "analysis/run_backtest.py",
        "--strategy", "ma_cross",
        "--code", "6758",
        "--fast", "5",
        "--slow", "20",
        "--period", "1y",
        "--synthetic",
        "--no-charts",
    )
    assert proc.returncode == 0, proc.stderr
    assert "t統計量" in proc.stdout
    assert (REPO_ROOT / "reports" / f"backtest-ma_cross-6758-{TODAY}.md").exists()

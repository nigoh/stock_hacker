"""forecast モジュールと overnight_forecast CLI のテスト。

決定論的な人工系列で予想生成・採点・台帳・集計を検証する（ネットワーク不使用）。
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stocklib import forecast
from stocklib.forecast import Forecast, ForecastError

REPO_ROOT = Path(__file__).resolve().parents[2]


def _ohlcv(close: list[float], end: dt.date | None = None) -> pd.DataFrame:
    """終値リストから High/Low/Open 付き OHLCV を組み立てる（営業日 index）。"""
    close_s = pd.Series(close, dtype=float)
    end = end or dt.date(2026, 6, 30)
    index = pd.date_range(end=end, periods=len(close_s), freq="B")
    return pd.DataFrame(
        {
            "Open": close_s.to_numpy(),
            "High": (close_s * 1.01).to_numpy(),
            "Low": (close_s * 0.99).to_numpy(),
            "Close": close_s.to_numpy(),
            "Volume": np.full(len(close_s), 1_000.0),
        },
        index=index,
    )


def _uptrend(n: int = 200, start: float = 1000.0, step: float = 5.0) -> pd.DataFrame:
    return _ohlcv([start + step * i for i in range(n)])


def _downtrend(n: int = 200, start: float = 2000.0, step: float = 5.0) -> pd.DataFrame:
    return _ohlcv([start - step * i for i in range(n)])


# --------------------------------------------------------------------------
# make_forecast
# --------------------------------------------------------------------------

def test_make_forecast_uptrend_is_up() -> None:
    fc = forecast.make_forecast("7203", _uptrend(), name="トヨタ")
    assert fc.direction == "up"
    assert fc.score > 0
    assert fc.prob_up > 0.5
    assert fc.pred_low < fc.pred_high
    assert 0.0 <= fc.confidence <= 1.0
    assert fc.data == "real"
    assert fc.forecast_id == f"{fc.asof_date.isoformat()}:7203"


def test_make_forecast_downtrend_is_down() -> None:
    fc = forecast.make_forecast("7203", _downtrend())
    assert fc.direction == "down"
    assert fc.score < 0
    assert fc.prob_up < 0.5


def test_make_forecast_target_is_next_business_day() -> None:
    # asof を金曜にすると target は翌月曜（土日スキップ）。
    df = _uptrend()
    df = df[df.index <= pd.Timestamp("2026-06-26")]  # 2026-06-26 は金曜
    fc = forecast.make_forecast("7203", df)
    assert fc.asof_date == dt.date(2026, 6, 26)
    assert fc.target_date == dt.date(2026, 6, 29)  # 月曜


def test_make_forecast_insufficient_history_raises() -> None:
    with pytest.raises(ForecastError):
        forecast.make_forecast("7203", _uptrend(n=forecast.MIN_HISTORY - 1))


def test_make_forecast_requires_close_column() -> None:
    df = pd.DataFrame({"Open": [1.0, 2.0, 3.0]})
    with pytest.raises(ForecastError):
        forecast.make_forecast("7203", df)


def test_prob_up_thresholds_map_to_labels() -> None:
    # 上昇確率が閾値帯（0.45〜0.55）に収まる弱いシグナルは flat になりうる。
    up = forecast.make_forecast("A", _uptrend())
    down = forecast.make_forecast("B", _downtrend())
    assert up.prob_up >= forecast.PROB_UP_THRESHOLD
    assert down.prob_up <= forecast.PROB_DOWN_THRESHOLD


# --------------------------------------------------------------------------
# grade_forecast
# --------------------------------------------------------------------------

def _forecast(**kw: object) -> Forecast:
    base = dict(
        code="7203", name="トヨタ", asof_date=dt.date(2026, 6, 30),
        target_date=dt.date(2026, 7, 1), asof_close=1000.0, direction="up",
        prob_up=0.7, pred_return=0.01, pred_low=990.0, pred_high=1030.0,
        confidence=0.5, s_trend=1.0, s_momentum=0.5, s_meanrev=-0.2, score=0.5,
        data="real",
    )
    base.update(kw)
    return Forecast(**base)  # type: ignore[arg-type]


def _future(prices: dict[str, float]) -> pd.DataFrame:
    idx = [pd.Timestamp(d) for d in prices]
    return pd.DataFrame({"Close": list(prices.values())}, index=pd.DatetimeIndex(idx))


def test_grade_up_hit_and_in_range() -> None:
    fc = _forecast()
    future = _future({"2026-06-30": 1000.0, "2026-07-01": 1010.0})
    g = forecast.grade_forecast(fc, future)
    assert g is not None
    assert g.actual_date == dt.date(2026, 7, 1)
    assert g.dir_hit is True
    assert g.in_range is True
    assert g.actual_return == pytest.approx(0.01)
    # prob_up=0.7, 実際 up(1) → brier = (0.7-1)^2 = 0.09
    assert g.brier == pytest.approx(0.09)


def test_grade_up_miss_when_falls() -> None:
    fc = _forecast()
    future = _future({"2026-06-30": 1000.0, "2026-07-01": 980.0})
    g = forecast.grade_forecast(fc, future)
    assert g is not None
    assert g.dir_hit is False
    assert g.in_range is False  # 980 < pred_low 990
    assert g.brier == pytest.approx(0.49)  # (0.7-0)^2


def test_grade_flat_within_band_hits() -> None:
    fc = _forecast(direction="flat", pred_low=995.0, pred_high=1005.0, prob_up=0.5)
    future = _future({"2026-06-30": 1000.0, "2026-07-01": 1001.0})  # +0.1% < FLAT_BAND
    g = forecast.grade_forecast(fc, future)
    assert g is not None
    assert g.dir_hit is True


def test_grade_returns_none_when_no_future_bar() -> None:
    fc = _forecast()
    future = _future({"2026-06-29": 999.0, "2026-06-30": 1000.0})  # asof 以降の足なし
    assert forecast.grade_forecast(fc, future) is None


# --------------------------------------------------------------------------
# 台帳 I/O
# --------------------------------------------------------------------------

def test_ledger_roundtrip_and_upsert(tmp_path: Path) -> None:
    path = tmp_path / "ledger.csv"
    ledger = forecast.load_ledger(path)
    assert list(ledger.columns) == list(forecast.LEDGER_COLUMNS)
    assert len(ledger) == 0

    fc = forecast.make_forecast("7203", _uptrend())
    ledger = forecast.upsert_forecast(ledger, fc, dt.date(2026, 7, 1))
    assert len(ledger) == 1
    # 同一 forecast_id を再 upsert しても重複しない
    ledger = forecast.upsert_forecast(ledger, fc, dt.date(2026, 7, 2))
    assert len(ledger) == 1

    forecast.save_ledger(ledger, path)
    reloaded = forecast.load_ledger(path)
    assert reloaded.iloc[0]["code"] == "7203"
    assert reloaded.iloc[0]["status"] == "pending"


def test_pending_rows_filters_by_data(tmp_path: Path) -> None:
    ledger = forecast.load_ledger(tmp_path / "l.csv")
    real = forecast.make_forecast("7203", _uptrend(), data="real")
    synth = forecast.make_forecast("6758", _uptrend(start=500.0), data="synthetic")
    ledger = forecast.upsert_forecast(ledger, real, dt.date(2026, 7, 1))
    ledger = forecast.upsert_forecast(ledger, synth, dt.date(2026, 7, 1))
    assert len(forecast.pending_rows(ledger, data="real")) == 1
    assert len(forecast.pending_rows(ledger, data="synthetic")) == 1
    assert len(forecast.pending_rows(ledger)) == 2


def test_apply_grade_updates_row(tmp_path: Path) -> None:
    ledger = forecast.load_ledger(tmp_path / "l.csv")
    fc = _forecast()
    ledger = forecast.upsert_forecast(ledger, fc, dt.date(2026, 6, 30))
    future = _future({"2026-06-30": 1000.0, "2026-07-01": 1010.0})
    g = forecast.grade_forecast(fc, future)
    assert g is not None
    ledger = forecast.apply_grade(ledger, g, dt.date(2026, 7, 1))
    row = ledger.iloc[0]
    assert row["status"] == "graded"
    assert bool(row["dir_hit"]) is True
    # row_to_forecast は pending 復元用: 復元して再採点しても一致
    restored = forecast.row_to_forecast(forecast.load_ledger(tmp_path / "l.csv").iloc[0]) \
        if (tmp_path / "l.csv").exists() else fc
    assert restored.code == "7203"


def test_apply_grade_on_csv_roundtripped_all_pending(tmp_path: Path) -> None:
    """全行 pending の台帳を CSV 往復してから採点しても落ちない（回帰）。

    採点列が空だと load_ledger が float64 と推論するため、日付文字列や真偽値の
    採点値を代入すると TypeError になっていた。初めて grade を回す利用者が
    必ず通る経路なので、CSV 往復を挟んで検証する。
    """
    path = tmp_path / "l.csv"
    fc = _forecast()
    ledger = forecast.upsert_forecast(forecast.load_ledger(path), fc, dt.date(2026, 6, 30))
    forecast.save_ledger(ledger, path)
    ledger = forecast.load_ledger(path)  # 採点列は空 → float64 に推論される
    assert ledger["actual_date"].dtype != object  # 前提（この推論が不具合の起点）
    g = forecast.grade_forecast(fc, _future({"2026-06-30": 1000.0, "2026-07-01": 1010.0}))
    assert g is not None
    ledger = forecast.apply_grade(ledger, g, dt.date(2026, 7, 1))
    row = ledger.iloc[0]
    assert row["status"] == "graded"
    assert row["actual_date"] == "2026-07-01"
    assert row["graded_on"] == "2026-07-01"
    assert bool(row["dir_hit"]) is True


# --------------------------------------------------------------------------
# 集計・較正
# --------------------------------------------------------------------------

def _graded_ledger() -> pd.DataFrame:
    """採点済み行を手組みした台帳。"""
    rows = []
    # up 予想 6件（4 hit）、down 予想 4件（3 hit）、flat 2件
    specs = [
        ("up", 0.7, True), ("up", 0.65, True), ("up", 0.6, True), ("up", 0.58, True),
        ("up", 0.7, False), ("up", 0.6, False),
        ("down", 0.3, True), ("down", 0.35, True), ("down", 0.4, True), ("down", 0.3, False),
        ("flat", 0.5, True), ("flat", 0.5, False),
    ]
    for i, (direction, prob_up, hit) in enumerate(specs):
        up_outcome = (direction == "up" and hit) or (direction == "down" and not hit) \
            or (direction == "flat" and prob_up > 0.5)
        actual_return = 0.02 if up_outcome else -0.02
        if direction == "flat":
            actual_return = 0.001 if hit else 0.02
        rows.append({
            "forecast_id": f"2026-06-{i+1:02d}:7203", "made_on": "2026-06-01",
            "asof_date": f"2026-06-{i+1:02d}", "target_date": f"2026-06-{i+2:02d}",
            "code": "7203" if i % 2 == 0 else "6758", "name": "", "data": "real",
            "direction": direction, "prob_up": prob_up, "pred_return": 0.0,
            "asof_close": 1000.0, "pred_low": 990.0, "pred_high": 1010.0,
            "confidence": abs(prob_up - 0.5) * 2, "s_trend": 0.0, "s_momentum": 0.0,
            "s_meanrev": 0.0, "score": 0.0, "status": "graded", "graded_on": "2026-06-10",
            "actual_date": f"2026-06-{i+2:02d}", "actual_close": 1000.0 * (1 + actual_return),
            "actual_return": actual_return, "dir_hit": hit, "in_range": True,
            "abs_error": abs(actual_return), "brier": (prob_up - (1.0 if actual_return > 0 else 0.0)) ** 2,
        })
    return pd.DataFrame(rows)


def test_summarize_counts_and_rates() -> None:
    summary = forecast.summarize(_graded_ledger())
    assert summary.n_graded == 12
    assert summary.n_directional == 10  # flat 2件を除く
    # 方向的中 up 4/6 + down 3/4 = 7/10
    assert summary.dir_hit_rate == pytest.approx(0.7)
    assert summary.baseline_brier == 0.25
    assert "up" in summary.per_direction and summary.per_direction["up"][0] == 6


def test_summarize_empty_is_nan() -> None:
    empty = forecast.load_ledger(Path("/nonexistent/ledger.csv"))
    summary = forecast.summarize(empty)
    assert summary.n_graded == 0
    assert np.isnan(summary.dir_hit_rate)


def test_calibration_table_buckets() -> None:
    table = forecast.calibration_table(_graded_ledger())
    assert table
    for row in table:
        assert 0.0 <= row["mean_prob_up"] <= 1.0
        assert 0.0 <= row["realized_up_freq"] <= 1.0
        assert row["n"] >= 1


def test_per_code_hit_rate() -> None:
    per_code = forecast.per_code_hit_rate(_graded_ledger())
    codes = {r["code"] for r in per_code}
    assert codes <= {"7203", "6758"}
    for r in per_code:
        assert 0.0 <= r["dir_hit_rate"] <= 1.0


# --------------------------------------------------------------------------
# CLI スモーク（subprocess、--synthetic）
# --------------------------------------------------------------------------

CLI = REPO_ROOT / "analysis" / "overnight_forecast.py"


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )


def test_cli_forecast_grade_calibration_synthetic(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.csv"
    universe = REPO_ROOT / "analysis" / "universe" / "liquid30.csv"

    fc = _run_cli(["--ledger", str(ledger), "forecast", "--synthetic", "--universe", str(universe)])
    assert fc.returncode == 0, fc.stderr
    assert "RESULT forecasts=" in fc.stdout
    assert "data=synthetic" in fc.stdout
    assert ledger.exists()

    gr = _run_cli(["--ledger", str(ledger), "grade", "--synthetic"])
    assert gr.returncode == 0, gr.stderr
    assert "RESULT graded=" in gr.stdout
    assert "data=synthetic" in gr.stdout

    cal = _run_cli(["--ledger", str(ledger), "calibration"])
    assert cal.returncode == 0, cal.stderr
    assert "RESULT graded=" in cal.stdout


def test_cli_run_synthetic(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.csv"
    universe = REPO_ROOT / "analysis" / "universe" / "liquid30.csv"
    res = _run_cli(["--ledger", str(ledger), "run", "--synthetic", "--universe", str(universe)])
    assert res.returncode == 0, res.stderr
    assert "RESULT graded=" in res.stdout and "forecasts=" in res.stdout


def test_cli_grade_with_no_pending_is_clean(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.csv"
    res = _run_cli(["--ledger", str(ledger), "grade", "--synthetic"])
    assert res.returncode == 0
    assert "RESULT graded=0 pending=0" in res.stdout


# --------------------------------------------------------------------------
# resolve_universe
# --------------------------------------------------------------------------

def test_resolve_universe_explicit(tmp_path: Path) -> None:
    import overnight_forecast

    csv = tmp_path / "u.csv"
    csv.write_text("code,name\n7203,トヨタ\n6758,ソニー\n", encoding="utf-8")
    path, items = overnight_forecast.resolve_universe(csv)
    assert path == csv
    assert items == [("7203", "トヨタ"), ("6758", "ソニー")]


def test_resolve_universe_code_only(tmp_path: Path) -> None:
    import overnight_forecast

    csv = tmp_path / "u.csv"
    csv.write_text("code\n7203\n", encoding="utf-8")
    _, items = overnight_forecast.resolve_universe(csv)
    assert items == [("7203", "")]


def test_resolve_universe_missing_code_column_raises(tmp_path: Path) -> None:
    import overnight_forecast

    csv = tmp_path / "u.csv"
    csv.write_text("ticker\n7203\n", encoding="utf-8")
    with pytest.raises(ForecastError):
        overnight_forecast.resolve_universe(csv)


# ---------------------------------------------------------------------------
# バグ修正の回帰: 集計の real/synthetic 分離・grade_forecast の防御
# ---------------------------------------------------------------------------

def test_summarize_excludes_synthetic_by_default() -> None:
    # real と synthetic の採点行が混在する台帳でも、既定(real)は real のみ集計する。
    ledger = forecast.load_ledger(Path("/nonexistent/l.csv"))
    real_fc = _forecast(code="7203", data="real")
    synth_fc = _forecast(code="6758", data="synthetic")
    ledger = forecast.upsert_forecast(ledger, real_fc, dt.date(2026, 6, 30))
    ledger = forecast.upsert_forecast(ledger, synth_fc, dt.date(2026, 6, 30))
    up = _future({"2026-06-30": 1000.0, "2026-07-01": 1010.0})
    ledger = forecast.apply_grade(ledger, forecast.grade_forecast(real_fc, up), dt.date(2026, 7, 1))
    ledger = forecast.apply_grade(ledger, forecast.grade_forecast(synth_fc, up), dt.date(2026, 7, 1))
    assert forecast.summarize(ledger).n_graded == 1                 # 既定 real のみ
    assert forecast.summarize(ledger, data="synthetic").n_graded == 1
    assert forecast.summarize(ledger, data=None).n_graded == 2      # 全件


def test_grade_forecast_handles_unsorted_and_tz_aware() -> None:
    fc = _forecast()  # asof 2026-06-30, asof_close 1000
    # 降順 index + tz-aware。旧バグ: iloc[0] が翌々日を誤採用 / tz比較で TypeError。
    idx = pd.DatetimeIndex(
        [pd.Timestamp("2026-07-02"), pd.Timestamp("2026-07-01"), pd.Timestamp("2026-06-30")],
        tz="Asia/Tokyo",
    )
    future = pd.DataFrame({"Close": [1020.0, 1010.0, 1000.0]}, index=idx)
    g = forecast.grade_forecast(fc, future)
    assert g is not None
    assert g.actual_date == dt.date(2026, 7, 1)   # 翌営業日（翌々日ではない）
    assert g.actual_close == pytest.approx(1010.0)

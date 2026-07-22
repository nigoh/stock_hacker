"""events モジュールと catalyst_radar CLI のテスト（ネットワーク不使用）。"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import pytest

from stocklib import events

REPO_ROOT = Path(__file__).resolve().parents[2]


def _payload(earnings_dates=None, ex_div=None):
    cal: dict = {}
    if earnings_dates is not None:
        cal["earnings"] = {"earningsDate": [{"fmt": d} for d in earnings_dates]}
    if ex_div is not None:
        cal["exDividendDate"] = {"fmt": ex_div}
    return {"quoteSummary": {"result": [{"calendarEvents": cal}]}}


def test_to_date_variants() -> None:
    assert events._to_date({"fmt": "2026-08-04"}) == dt.date(2026, 8, 4)
    assert events._to_date({"raw": 1785974400}) is not None  # epoch
    assert events._to_date("2026-08-04") == dt.date(2026, 8, 4)
    assert events._to_date({"fmt": "not-a-date"}) is None
    assert events._to_date(None) is None


def test_parse_calendar_events_takes_earliest_earnings() -> None:
    ev = events.parse_calendar_events(
        _payload(earnings_dates=["2026-08-06", "2026-08-04"], ex_div="2026-09-29"), "7203", "トヨタ"
    )
    assert ev.earnings_date == dt.date(2026, 8, 4)  # レンジの早い方
    assert ev.ex_dividend_date == dt.date(2026, 9, 29)
    assert ev.code == "7203" and ev.name == "トヨタ"


def test_parse_calendar_events_missing() -> None:
    ev = events.parse_calendar_events({"quoteSummary": {"result": []}}, "7203")
    assert ev.earnings_date is None and ev.ex_dividend_date is None


def test_upcoming_events_filters_and_sorts() -> None:
    asof = dt.date(2026, 7, 22)
    ev = events.CalendarEvents(
        code="7203", name="", earnings_date=dt.date(2026, 8, 4),
        ex_dividend_date=dt.date(2026, 7, 25),
    )
    up = ev.upcoming_events(asof, within_days=30)
    assert [label for label, _d, _n in up] == ["配当落ち", "決算発表"]  # 日付昇順
    assert up[0][2] == 3 and up[1][2] == 13  # 残日数


def test_upcoming_events_excludes_past_and_far() -> None:
    asof = dt.date(2026, 7, 22)
    ev = events.CalendarEvents(
        code="7203", name="", earnings_date=dt.date(2026, 7, 1),   # 過去
        ex_dividend_date=dt.date(2026, 12, 1),                      # 遠い
    )
    assert ev.upcoming_events(asof, within_days=30) == []


def test_synthetic_events_deterministic() -> None:
    asof = dt.date(2026, 7, 22)
    a = events.fetch_calendar_events("7203", synthetic=True, asof=asof)
    b = events.fetch_calendar_events("7203", synthetic=True, asof=asof)
    assert a.earnings_date == b.earnings_date
    assert a.data == "synthetic"
    assert a.earnings_date is not None and a.earnings_date > asof
    # 別コードは（ほぼ）別日程
    c = events.fetch_calendar_events("6758", synthetic=True, asof=asof)
    assert (a.earnings_date, a.ex_dividend_date) != (c.earnings_date, c.ex_dividend_date)


def test_cli_synthetic_smoke() -> None:
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "analysis" / "catalyst_radar.py"),
         "--synthetic", "--within", "60",
         "--universe", str(REPO_ROOT / "analysis" / "universe" / "liquid30.csv")],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert res.returncode == 0, res.stderr
    last = res.stdout.strip().splitlines()[-1]
    assert last.startswith("RESULT events=") and "data=synthetic" in last


def test_to_date_fmt_invalid_falls_back_to_raw() -> None:
    # fmt が不正でも raw エポックへフォールバックする（旧バグ: None を返していた）。
    d = events._to_date({"fmt": "N/A", "raw": 1785974400})
    assert d is not None and isinstance(d, dt.date)

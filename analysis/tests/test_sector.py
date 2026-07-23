"""sector モジュールと sector_rotation CLI のテスト（決定論・ネットワーク不使用）。"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from stocklib import sector

REPO_ROOT = Path(__file__).resolve().parents[2]


def _df(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range(end=dt.date(2026, 6, 30), periods=len(closes), freq="B")
    return pd.DataFrame({"Close": closes}, index=idx)


def _rising(n: int, start: float = 100.0, step: float = 1.0) -> pd.DataFrame:
    return _df([start + step * i for i in range(n)])


def _falling(n: int, start: float = 300.0, step: float = 1.0) -> pd.DataFrame:
    return _df([start - step * i for i in range(n)])


def test_return_over() -> None:
    s = _rising(300)["Close"]
    # 63本前比: (100+299)/(100+236) - 1
    assert sector._return_over(s, 63) == pytest.approx(399.0 / 336.0 - 1.0)
    assert sector._return_over(_df([1.0, 2.0])["Close"], 63) is None  # データ不足


def test_above_sma() -> None:
    assert sector._above_sma(_rising(60)["Close"], 50) is True    # 上昇は終値>SMA
    assert sector._above_sma(_falling(60)["Close"], 50) is False  # 下降は終値<SMA
    assert sector._above_sma(_rising(30)["Close"], 50) is None    # 母数不足


def test_sector_aggregation_uses_median() -> None:
    # 同一セクターに強・中・弱の3銘柄 → セクター・モメンタムは中央値（=中）。
    prices = {
        "hi": _rising(260, step=2.0),
        "mid": _rising(260, step=1.0),
        "lo": _rising(260, step=0.5),
    }
    sectors = {"hi": "電機", "mid": "電機", "lo": "電機"}
    rows = sector.compute_sector_rotation(prices, sectors)
    assert len(rows) == 1
    row = rows[0]
    assert row.sector == "電機"
    assert row.n == 3
    # 各銘柄の 63日リターンの中央値 = mid 銘柄のリターン。
    mid_ret = sector._return_over(prices["mid"]["Close"], sector.RANK_WINDOW)
    assert row.momentum[sector.RANK_WINDOW] == pytest.approx(mid_ret)


def test_sector_ranking_leader_first() -> None:
    # 強いセクターが上位（rank=1）、弱いセクターが下位。
    prices = {
        "a1": _rising(260, step=3.0), "a2": _rising(260, step=2.5),  # 強
        "b1": _falling(260, step=2.0), "b2": _falling(260, step=1.5),  # 弱
    }
    sectors = {"a1": "強", "a2": "強", "b1": "弱", "b2": "弱"}
    rows = sector.compute_sector_rotation(prices, sectors)
    assert [r.sector for r in rows] == ["強", "弱"]
    assert rows[0].rank == 1 and rows[1].rank == 2
    assert rows[0].rank_momentum > rows[1].rank_momentum


def test_sector_internal_breadth() -> None:
    # 上昇2・下降1 → 終値>SMA50 の割合 = 2/3。
    prices = {
        "u1": _rising(60), "u2": _rising(60, start=50.0), "d1": _falling(60),
    }
    sectors = {"u1": "S", "u2": "S", "d1": "S"}
    rows = sector.compute_sector_rotation(prices, sectors)
    assert rows[0].breadth_base == 3
    assert rows[0].breadth_above_sma == pytest.approx(2.0 / 3.0)


def test_unknown_sector_bucketed() -> None:
    prices = {"x": _rising(260), "y": _rising(260, start=50.0)}
    sectors = {"x": "", "y": "電機"}  # x はセクター未指定
    rows = sector.compute_sector_rotation(prices, sectors)
    by_sector = {r.sector: r for r in rows}
    assert sector.UNKNOWN_SECTOR in by_sector
    assert by_sector[sector.UNKNOWN_SECTOR].n == 1


def test_short_series_still_counted_but_no_momentum_ranks_last() -> None:
    # 63本に満たないセクターは代表窓モメンタムが算出できず末尾に回る。
    prices = {
        "long": _rising(260), "short": _rising(30),
    }
    sectors = {"long": "十分", "short": "短い"}
    rows = sector.compute_sector_rotation(prices, sectors)
    assert rows[0].sector == "十分"          # 代表窓算出可 → 先頭
    assert rows[-1].sector == "短い"          # 算出不可 → 末尾
    assert rows[-1].rank_momentum is None


def test_empty_universe() -> None:
    assert sector.compute_sector_rotation({}, {}) == []


def test_ignores_series_without_close_or_too_short() -> None:
    prices = {
        "ok": _rising(260),
        "onebar": _df([10.0]),                          # 1本 → 除外
        "nocol": pd.DataFrame({"Open": [1.0, 2.0]}),    # Close なし → 除外
    }
    sectors = {"ok": "S", "onebar": "S", "nocol": "S"}
    rows = sector.compute_sector_rotation(prices, sectors)
    assert len(rows) == 1 and rows[0].n == 1


# --------------------------------------------------------------------------
# CLI スモーク
# --------------------------------------------------------------------------

def test_cli_synthetic_smoke() -> None:
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "analysis" / "sector_rotation.py"),
         "--synthetic", "--universe", str(REPO_ROOT / "analysis" / "universe" / "large70.csv")],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert res.returncode == 0, res.stderr
    last = res.stdout.strip().splitlines()[-1]
    assert last.startswith("RESULT sectors=") and "data=synthetic" in last
    assert "covered=" in last


def test_cli_bad_universe_exits_1(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("ticker\n7203\n", encoding="utf-8")
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "analysis" / "sector_rotation.py"),
         "--synthetic", "--universe", str(bad)],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert res.returncode == 1

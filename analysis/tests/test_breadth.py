"""breadth モジュールと market_breadth CLI のテスト（決定論・ネットワーク不使用）。"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from stocklib import breadth

REPO_ROOT = Path(__file__).resolve().parents[2]


def _df(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range(end=dt.date(2026, 6, 30), periods=len(closes), freq="B")
    return pd.DataFrame({"Close": closes}, index=idx)


def _rising(n: int, start: float = 100.0, step: float = 1.0) -> pd.DataFrame:
    return _df([start + step * i for i in range(n)])


def _falling(n: int, start: float = 300.0, step: float = 1.0) -> pd.DataFrame:
    return _df([start - step * i for i in range(n)])


def test_advancers_decliners_unchanged() -> None:
    prices = {
        "A": _df([100.0, 101.0]),   # up
        "B": _df([100.0, 99.0]),    # down
        "C": _df([100.0, 100.0]),   # unchanged
        "D": _df([50.0, 55.0]),     # up
    }
    r = breadth.compute_breadth(prices)
    assert r.n == 4
    assert r.advancers == 2
    assert r.decliners == 1
    assert r.unchanged == 1
    assert r.advance_pct == pytest.approx(0.5)


def test_pct_above_sma() -> None:
    # 上昇トレンドは終値が各SMAを上回る、下降トレンドは下回る。
    prices = {"up": _rising(260), "down": _falling(260)}
    r = breadth.compute_breadth(prices)
    for w in breadth.SMA_WINDOWS:
        assert r.sma_base[w] == 2
        assert r.pct_above_sma[w] == pytest.approx(0.5)  # up は上・down は下


def test_new_highs_and_lows() -> None:
    prices = {"up": _rising(260), "down": _falling(260)}
    r = breadth.compute_breadth(prices)
    assert r.new_highs == 1   # 上昇トレンドは直近が252日高値
    assert r.new_lows == 1    # 下降トレンドは直近が252日安値


def test_short_series_skipped_for_sma_but_counted() -> None:
    # SMA200 に満たない系列は SMA200 の母数から外れるが騰落には数える。
    prices = {"short": _rising(30), "long": _rising(260)}
    r = breadth.compute_breadth(prices)
    assert r.n == 2
    assert r.sma_base[25] == 2      # 25本以上は両方
    assert r.sma_base[200] == 1     # 200本以上は long のみ


def test_ad_ratio_all_up_is_none_when_no_decliners() -> None:
    # 値下がりが1件も無ければ分母0で None。
    prices = {"A": _rising(40), "B": _rising(40, start=50.0)}
    r = breadth.compute_breadth(prices)
    assert r.ad_ratio_25 is None


def test_ad_ratio_value() -> None:
    # 25日窓: 常に上がる銘柄2・常に下がる銘柄1 → adv=2/日, dec=1/日 → 比 200。
    prices = {"u1": _rising(40), "u2": _rising(40, start=200.0), "d1": _falling(40)}
    r = breadth.compute_breadth(prices)
    assert r.ad_ratio_25 == pytest.approx(200.0)
    assert r.ad_ratio_label().startswith("過熱")


def test_ad_ratio_label_oversold() -> None:
    prices = {"u1": _rising(40), "d1": _falling(40), "d2": _falling(40, start=250.0)}
    r = breadth.compute_breadth(prices)
    # adv=1/日, dec=2/日 → 50 → 売られすぎ
    assert r.ad_ratio_25 == pytest.approx(50.0)
    assert "売られすぎ" in r.ad_ratio_label()


def test_empty_universe() -> None:
    r = breadth.compute_breadth({})
    assert r.n == 0
    assert r.ad_ratio_25 is None


def test_ignores_series_without_close_or_too_short() -> None:
    prices = {
        "ok": _df([10.0, 11.0]),
        "onebar": _df([10.0]),           # 1本 → 数えない
        "nocol": pd.DataFrame({"Open": [1.0, 2.0]}),  # Close なし → 数えない
    }
    r = breadth.compute_breadth(prices)
    assert r.n == 1


# --------------------------------------------------------------------------
# CLI スモーク
# --------------------------------------------------------------------------

def test_cli_synthetic_smoke() -> None:
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "analysis" / "market_breadth.py"),
         "--synthetic", "--universe", str(REPO_ROOT / "analysis" / "universe" / "liquid30.csv")],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert res.returncode == 0, res.stderr
    last = res.stdout.strip().splitlines()[-1]
    assert last.startswith("RESULT breadth=") and "data=synthetic" in last


def test_cli_bad_universe_exits_1(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("ticker\n7203\n", encoding="utf-8")
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "analysis" / "market_breadth.py"),
         "--synthetic", "--universe", str(bad)],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert res.returncode == 1

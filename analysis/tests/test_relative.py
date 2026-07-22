"""relative モジュールと relative_strength CLI のテスト（決定論・ネットワーク不使用）。"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from stocklib import relative

REPO_ROOT = Path(__file__).resolve().parents[2]


def _close(closes: list[float]) -> pd.Series:
    idx = pd.date_range(end=dt.date(2026, 6, 30), periods=len(closes), freq="B")
    return pd.Series(closes, index=idx)


def _df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"Close": _close(closes).to_numpy()}, index=_close(closes).index)


def test_return_over() -> None:
    s = _close([100.0] * 300)
    s.iloc[-1] = 110.0
    assert relative._return_over(s, 63) == pytest.approx(0.10)
    assert relative._return_over(_close([1.0, 2.0]), 63) is None  # データ不足


def test_blended_momentum_uses_available_windows() -> None:
    # 260本あれば全窓（63/126/189/252）が使える。単調増加は正のモメンタム。
    s = _close([100.0 + i for i in range(260)])
    result = relative.blended_momentum(s)
    assert result is not None
    blended, components = result
    assert set(components) == {63, 126, 189, 252}
    assert blended > 0


def test_blended_momentum_partial_windows() -> None:
    # 130本なら 63/126 のみ使え、重みは利用可能分で正規化される。
    s = _close([100.0 + i for i in range(130)])
    result = relative.blended_momentum(s)
    assert result is not None
    _blended, components = result
    assert set(components) == {63, 126}


def test_blended_momentum_none_when_too_short() -> None:
    assert relative.blended_momentum(_close([1.0, 2.0, 3.0])) is None


def test_percentile_ranks_monotonic() -> None:
    ranks = relative._percentile_ranks([10.0, 20.0, 30.0])
    assert ranks[0] < ranks[1] < ranks[2]
    assert ranks[-1] == pytest.approx(99.0)
    assert ranks[0] == pytest.approx(1.0)


def test_percentile_ranks_ties_get_same() -> None:
    ranks = relative._percentile_ranks([5.0, 5.0, 9.0])
    assert ranks[0] == ranks[1]        # 同点は同順位
    assert ranks[2] > ranks[0]


def test_percentile_ranks_single() -> None:
    assert relative._percentile_ranks([42.0]) == [50.0]


def test_compute_relative_strength_orders_by_rank() -> None:
    prices = {
        "strong": _df([100.0 + 2 * i for i in range(260)]),   # 急上昇
        "flat": _df([100.0 for _ in range(260)]),             # 横ばい
        "weak": _df([300.0 - i for i in range(260)]),         # 下落
    }
    rows = relative.compute_relative_strength(prices, {"strong": "S"})
    assert [r.code for r in rows] == ["strong", "flat", "weak"]  # RS 降順
    assert rows[0].rs_rank > rows[-1].rs_rank
    assert rows[0].name == "S"


def test_compute_relative_strength_skips_uncomputable() -> None:
    prices = {"ok": _df([100.0 + i for i in range(260)]), "short": _df([1.0, 2.0])}
    rows = relative.compute_relative_strength(prices)
    assert [r.code for r in rows] == ["ok"]


def test_sector_relative_valuation_median_and_premium() -> None:
    infos = {
        "A": {"PER（実績）": 10.0, "PBR": 1.0},
        "B": {"PER（実績）": 20.0, "PBR": 2.0},
        "C": {"PER（実績）": 30.0, "PBR": 3.0},  # 別セクター
    }
    sectors = {"A": "銀行", "B": "銀行", "C": "電機"}
    rows = relative.sector_relative_valuation(infos, sectors)
    by_code = {r.code: r for r in rows}
    # 銀行の PER 中央値 = median(10,20) = 15
    assert by_code["A"].sector_per_median == pytest.approx(15.0)
    assert by_code["A"].per_premium == pytest.approx(10.0 / 15.0 - 1.0)
    assert by_code["B"].per_premium == pytest.approx(20.0 / 15.0 - 1.0)
    # 電機は C 単独 → 中央値は自身
    assert by_code["C"].sector_per_median == pytest.approx(30.0)
    assert by_code["C"].per_premium == pytest.approx(0.0)


def test_sector_relative_valuation_handles_missing_per() -> None:
    infos = {"A": {"PBR": 1.0}, "B": {"PER（実績）": 20.0, "PBR": 2.0}}
    sectors = {"A": "銀行", "B": "銀行"}
    rows = relative.sector_relative_valuation(infos, sectors)
    a = next(r for r in rows if r.code == "A")
    assert a.per is None and a.per_premium is None  # PER 欠損は乖離も None


# --------------------------------------------------------------------------
# CLI スモーク
# --------------------------------------------------------------------------

def test_cli_synthetic_smoke() -> None:
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "analysis" / "relative_strength.py"),
         "--synthetic", "--top", "3",
         "--universe", str(REPO_ROOT / "analysis" / "universe" / "liquid30.csv")],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert res.returncode == 0, res.stderr
    last = res.stdout.strip().splitlines()[-1]
    assert last.startswith("RESULT rs=") and "data=synthetic" in last


def test_cli_no_valuation_flag() -> None:
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "analysis" / "relative_strength.py"),
         "--synthetic", "--no-valuation",
         "--universe", str(REPO_ROOT / "analysis" / "universe" / "liquid30.csv")],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert res.returncode == 0, res.stderr
    assert "valuation=0" in res.stdout  # 取得を省いたので0
    assert "セクター相対バリュエーション" not in res.stdout


def test_cli_synthetic_empty_universe_not_unavailable(tmp_path: Path) -> None:
    u = tmp_path / "empty.csv"
    u.write_text("code,name,sector\n", encoding="utf-8")
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "analysis" / "relative_strength.py"),
         "--synthetic", "--universe", str(u)],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert res.returncode == 0, res.stderr
    assert "data=synthetic" in res.stdout and "data=unavailable" not in res.stdout

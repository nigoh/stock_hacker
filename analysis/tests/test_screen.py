"""screen.py の各フィルタを合成データで検証するテスト。

合成データ（``--synthetic`` 相当）は銘柄コードごとにシード固定で決定論的なので、
stocklib.indicators で期待値を独立に計算し、screen() の絞り込み結果と照合する。
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

import screen
from stocklib import indicators
from stocklib.data import DataFetchError, fetch_info, fetch_prices

CODES: list[str] = ["7203", "6758", "9984", "8306", "6501"]
PERIOD: str = "1y"
SMA_WINDOW: int = 50


@pytest.fixture(scope="module")
def universe() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": CODES,
            "name": [f"銘柄{c}" for c in CODES],
            "sector": ["テスト"] * len(CODES),
        }
    )


@pytest.fixture(scope="module")
def expected() -> dict[str, dict[str, float]]:
    """各銘柄の指標を screen.py と独立に計算した期待値。"""
    out: dict[str, dict[str, float]] = {}
    for code in CODES:
        df = fetch_prices(code, period=PERIOD, synthetic=True)[code]
        close = df["Close"]
        volume = df["Volume"]
        last = float(close.iloc[-1])
        avg20 = float(volume.iloc[-21:-1].mean())
        info = fetch_info(code, synthetic=True)
        out[code] = {
            "close": last,
            "rsi": float(indicators.rsi(close, 14).iloc[-1]),
            "sma": float(indicators.sma(close, SMA_WINDOW).iloc[-1]),
            "vol_surge": float(volume.iloc[-1]) / avg20,
            "ret_pct": (last / float(close.iloc[0]) - 1.0) * 100.0,
            "per": float(info["PER（実績）"]),  # type: ignore[arg-type]
            "pbr": float(info["PBR"]),  # type: ignore[arg-type]
            "div_yield_pct": float(info["配当利回り"]) * 100.0,  # type: ignore[arg-type]
        }
    return out


def _run(universe: pd.DataFrame, **kwargs: object) -> set[str]:
    criteria = screen.ScreenCriteria(**kwargs)  # type: ignore[arg-type]
    result, errors = screen.screen(universe, PERIOD, criteria, synthetic=True)
    assert errors == []
    return set() if result.empty else set(result["code"])


def _split_threshold(values: list[float]) -> float:
    """値集合を非自明に二分する閾値（中央付近の2値の中点）を返す。"""
    s = sorted(values)
    mid = len(s) // 2
    thr = (s[mid - 1] + s[mid]) / 2.0
    assert not math.isnan(thr)
    return thr


def test_no_criteria_returns_all(universe: pd.DataFrame) -> None:
    assert _run(universe) == set(CODES)


def test_result_has_condition_columns(universe: pd.DataFrame) -> None:
    criteria = screen.ScreenCriteria(price_below_sma=SMA_WINDOW)
    result, _ = screen.screen(universe, PERIOD, criteria, synthetic=True)
    if not result.empty:
        for col in ("rsi14", f"sma{SMA_WINDOW}", "ret_period", "vol_surge", "ann_vol"):
            assert col in result.columns


def test_rsi_below(universe: pd.DataFrame, expected: dict[str, dict[str, float]]) -> None:
    thr = _split_threshold([m["rsi"] for m in expected.values()])
    want = {c for c, m in expected.items() if m["rsi"] < thr}
    assert 0 < len(want) < len(CODES)
    assert _run(universe, rsi_below=thr) == want


def test_rsi_above(universe: pd.DataFrame, expected: dict[str, dict[str, float]]) -> None:
    thr = _split_threshold([m["rsi"] for m in expected.values()])
    want = {c for c, m in expected.items() if m["rsi"] > thr}
    assert 0 < len(want) < len(CODES)
    assert _run(universe, rsi_above=thr) == want


def test_price_above_sma(universe: pd.DataFrame, expected: dict[str, dict[str, float]]) -> None:
    want = {c for c, m in expected.items() if m["close"] > m["sma"]}
    assert _run(universe, price_above_sma=SMA_WINDOW) == want


def test_price_below_sma(universe: pd.DataFrame, expected: dict[str, dict[str, float]]) -> None:
    want = {c for c, m in expected.items() if m["close"] < m["sma"]}
    assert _run(universe, price_below_sma=SMA_WINDOW) == want


def test_price_above_and_below_sma_are_disjoint(
    universe: pd.DataFrame,
) -> None:
    above = _run(universe, price_above_sma=SMA_WINDOW)
    below = _run(universe, price_below_sma=SMA_WINDOW)
    assert above & below == set()
    assert above | below == set(CODES)  # 合成データで close == sma の一致はまず起きない


def test_volume_surge(universe: pd.DataFrame, expected: dict[str, dict[str, float]]) -> None:
    thr = _split_threshold([m["vol_surge"] for m in expected.values()])
    want = {c for c, m in expected.items() if m["vol_surge"] >= thr}
    assert 0 < len(want) < len(CODES)
    assert _run(universe, volume_surge=thr) == want


def test_return_below(universe: pd.DataFrame, expected: dict[str, dict[str, float]]) -> None:
    thr = _split_threshold([m["ret_pct"] for m in expected.values()])
    want = {c for c, m in expected.items() if m["ret_pct"] < thr}
    assert 0 < len(want) < len(CODES)
    assert _run(universe, return_below=thr) == want


def test_return_above(universe: pd.DataFrame, expected: dict[str, dict[str, float]]) -> None:
    thr = _split_threshold([m["ret_pct"] for m in expected.values()])
    want = {c for c, m in expected.items() if m["ret_pct"] > thr}
    assert 0 < len(want) < len(CODES)
    assert _run(universe, return_above=thr) == want


def test_combined_filters_intersect(
    universe: pd.DataFrame, expected: dict[str, dict[str, float]]
) -> None:
    rsi_thr = _split_threshold([m["rsi"] for m in expected.values()])
    want = {c for c, m in expected.items() if m["rsi"] < rsi_thr and m["close"] < m["sma"]}
    assert _run(universe, rsi_below=rsi_thr, price_below_sma=SMA_WINDOW) == want


def test_per_below(universe: pd.DataFrame, expected: dict[str, dict[str, float]]) -> None:
    thr = _split_threshold([m["per"] for m in expected.values()])
    want = {c for c, m in expected.items() if m["per"] < thr}
    assert 0 < len(want) < len(CODES)
    assert _run(universe, per_below=thr) == want


def test_pbr_below(universe: pd.DataFrame, expected: dict[str, dict[str, float]]) -> None:
    thr = _split_threshold([m["pbr"] for m in expected.values()])
    want = {c for c, m in expected.items() if m["pbr"] < thr}
    assert 0 < len(want) < len(CODES)
    assert _run(universe, pbr_below=thr) == want


def test_dividend_yield_above(
    universe: pd.DataFrame, expected: dict[str, dict[str, float]]
) -> None:
    thr = _split_threshold([m["div_yield_pct"] for m in expected.values()])
    want = {c for c, m in expected.items() if m["div_yield_pct"] > thr}
    assert 0 < len(want) < len(CODES)
    assert _run(universe, dividend_yield_above=thr) == want


def test_valuation_and_technical_are_anded(
    universe: pd.DataFrame, expected: dict[str, dict[str, float]]
) -> None:
    per_thr = _split_threshold([m["per"] for m in expected.values()])
    rsi_thr = _split_threshold([m["rsi"] for m in expected.values()])
    want = {c for c, m in expected.items() if m["per"] < per_thr and m["rsi"] < rsi_thr}
    assert _run(universe, per_below=per_thr, rsi_below=rsi_thr) == want


def test_valuation_columns_only_when_criteria_given(universe: pd.DataFrame) -> None:
    result, _ = screen.screen(universe, PERIOD, screen.ScreenCriteria(), synthetic=True)
    for col in ("per", "pbr", "div_yield"):
        assert col not in result.columns

    result, _ = screen.screen(
        universe, PERIOD, screen.ScreenCriteria(per_below=1e9), synthetic=True
    )
    assert not result.empty
    for col in ("per", "pbr", "div_yield"):
        assert col in result.columns


def test_missing_info_fails_valuation_criteria(
    universe: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fetch_info が失敗・欠損の銘柄は「値なし＝条件不成立」で除外される。"""
    missing_code = CODES[0]

    def fake_fetch_info(code: str, *, synthetic: bool = False) -> dict[str, object]:
        if code == missing_code:
            raise DataFetchError("テスト用の取得失敗")
        return fetch_info(code, synthetic=synthetic)

    monkeypatch.setattr(screen, "fetch_info", fake_fetch_info)
    got = _run(universe, per_below=1e9)
    assert got == set(CODES) - {missing_code}


def test_partial_missing_info_renders_dash(
    universe: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PER はあるが PBR・配当利回りが欠損の銘柄は、合格時に該当列が - になる。"""

    def fake_fetch_info(code: str, *, synthetic: bool = False) -> dict[str, object]:
        return {"PER（実績）": 10.0}  # PBR・配当利回りは取得できなかった想定

    monkeypatch.setattr(screen, "fetch_info", fake_fetch_info)
    result, errors = screen.screen(
        universe, PERIOD, screen.ScreenCriteria(per_below=1e9), synthetic=True
    )
    assert errors == []
    assert set(result["code"]) == set(CODES)
    assert result["pbr"].isna().all()
    assert result["div_yield"].isna().all()
    table = screen.result_table(result)
    row = next(line for line in table.splitlines() if f"| {CODES[0]} |" in line)
    cells = [c.strip() for c in row.strip("|").split("|")]
    headers = [h.strip() for h in table.splitlines()[0].strip("|").split("|")]
    assert cells[headers.index("pbr")] == "-"
    assert cells[headers.index("div_yield")] == "-"
    assert cells[headers.index("per")] == "10.00"


def test_missing_pbr_fails_pbr_criterion(
    universe: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_fetch_info(code: str, *, synthetic: bool = False) -> dict[str, object]:
        return {"PER（実績）": 10.0}  # PBR なし

    monkeypatch.setattr(screen, "fetch_info", fake_fetch_info)
    assert _run(universe, pbr_below=1e9) == set()


def test_cli_main_valuation_report_notes_dummy_values(
    tmp_path, universe: pd.DataFrame
) -> None:
    csv_path = tmp_path / "universe.csv"
    universe.to_csv(csv_path, index=False)
    rc = screen.main(
        [
            "--universe", str(csv_path),
            "--period", "6mo",
            "--per-below", "1000000",
            "--pbr-below", "1000000",
            "--dividend-yield-above", "-1",
            "--synthetic",
        ]
    )
    assert rc == 0
    import datetime as dt

    report_path = screen.report.REPORTS_DIR / f"screen-{dt.date.today().isoformat()}.md"
    content = report_path.read_text(encoding="utf-8")
    assert "PER < 1e+06" in content
    assert "PBR < 1e+06" in content
    assert "配当利回り > -1%" in content
    assert "合成ダミー値" in content
    assert "| per | pbr | div_yield |" in content


def test_volume_surge_ratio_insufficient_data() -> None:
    short = pd.Series([100.0] * 10)
    assert math.isnan(screen.volume_surge_ratio(short))
    assert math.isnan(screen.volume_surge_ratio(None))


def test_cli_main_writes_conditions_to_report(tmp_path, universe: pd.DataFrame) -> None:
    csv_path = tmp_path / "universe.csv"
    universe.to_csv(csv_path, index=False)
    rc = screen.main(
        [
            "--universe", str(csv_path),
            "--period", "6mo",
            "--rsi-above", "0",
            "--volume-surge", "0.1",
            "--return-above", "-1000",
            "--synthetic",
        ]
    )
    assert rc == 0
    import datetime as dt

    report_path = screen.report.REPORTS_DIR / f"screen-{dt.date.today().isoformat()}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "RSI(14) > 0" in content
    assert "直近出来高" in content
    assert "期間リターン > -1000%" in content
    assert "免責事項" in content

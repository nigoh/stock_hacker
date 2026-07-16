"""fundamentals モジュールと fundamentals_report.py CLI の検証（ネットワーク不使用）。"""

from __future__ import annotations

import datetime as dt
import math
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from stocklib.fundamentals import (
    HISTORY_COLUMNS,
    _cagr,
    _growth_streak,
    analyze_growth,
    fetch_financial_history,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TODAY = dt.date.today().isoformat()


# ---------------------------------------------------------------------------
# 合成業績（synthetic=True）
# ---------------------------------------------------------------------------


def test_synthetic_history_shape_and_columns() -> None:
    history = fetch_financial_history("7203", years=5, synthetic=True)
    assert list(history.columns) == list(HISTORY_COLUMNS)
    assert len(history) == 5
    assert isinstance(history.index, pd.DatetimeIndex)
    assert history.index.is_monotonic_increasing
    assert (history["売上高"] > 0).all()


def test_synthetic_history_is_deterministic() -> None:
    a = fetch_financial_history("7203", years=5, synthetic=True)
    b = fetch_financial_history("7203", years=5, synthetic=True)
    pd.testing.assert_frame_equal(a, b)


def test_synthetic_history_differs_by_code() -> None:
    a = fetch_financial_history("7203", years=5, synthetic=True)
    b = fetch_financial_history("6758", years=5, synthetic=True)
    assert not a["売上高"].equals(b["売上高"])


def test_years_must_be_positive() -> None:
    with pytest.raises(ValueError):
        fetch_financial_history("7203", years=0, synthetic=True)


# ---------------------------------------------------------------------------
# analyze_growth — CAGR の手計算一致・マージン・連続増益
# ---------------------------------------------------------------------------


def test_synthetic_cagr_matches_hand_calculation() -> None:
    history = fetch_financial_history("7203", years=5, synthetic=True)
    g = analyze_growth(history)
    rev = history["売上高"]
    n = len(rev)
    expected = (float(rev.iloc[-1]) / float(rev.iloc[0])) ** (1.0 / (n - 1)) - 1.0
    assert g["revenue_cagr"] == pytest.approx(expected)
    net = history["純利益"]
    expected_net = (float(net.iloc[-1]) / float(net.iloc[0])) ** (1.0 / (n - 1)) - 1.0
    assert g["net_income_cagr"] == pytest.approx(expected_net)


def _manual_history() -> pd.DataFrame:
    index = pd.to_datetime(["2022-03-31", "2023-03-31", "2024-03-31", "2025-03-31"])
    return pd.DataFrame(
        {
            "売上高": [1000.0, 1100.0, 1210.0, 1331.0],  # 毎年 +10%
            "営業利益": [100.0, 121.0, 110.0, 133.1],
            "純利益": [50.0, 60.0, 55.0, 70.0],
            "自己資本": [500.0, 550.0, 600.0, 700.0],
            "営業CF": [80.0, 90.0, 85.0, 100.0],
        },
        index=index,
    )


def test_analyze_growth_manual_values() -> None:
    g = analyze_growth(_manual_history())
    assert g["years"] == 4
    assert g["revenue_cagr"] == pytest.approx(0.10)  # (1331/1000)^(1/3) - 1
    op_margin: pd.Series = g["op_margin"]  # type: ignore[assignment]
    assert op_margin.iloc[0] == pytest.approx(0.10)
    assert op_margin.iloc[1] == pytest.approx(0.11)
    roe: pd.Series = g["roe"]  # type: ignore[assignment]
    assert roe.iloc[-1] == pytest.approx(70.0 / 700.0)
    # 増収は4期連続（差分3回すべて正）、営業増益・純増益は直近1期のみ
    assert g["revenue_streak"] == 3
    assert g["op_income_streak"] == 1
    assert g["net_income_streak"] == 1


def test_cagr_negative_endpoint_is_nan() -> None:
    s = pd.Series([-10.0, 50.0, 100.0])
    assert math.isnan(_cagr(s))
    assert math.isnan(_cagr(pd.Series([100.0])))  # 2期未満も NaN


def test_growth_streak_edge_cases() -> None:
    assert _growth_streak(pd.Series([1.0, 2.0, 3.0])) == 2
    assert _growth_streak(pd.Series([3.0, 2.0, 1.0])) == 0
    assert _growth_streak(pd.Series([1.0])) == 0


def test_analyze_growth_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        analyze_growth(pd.DataFrame())
    with pytest.raises(ValueError):
        analyze_growth(pd.DataFrame({"売上高": [1.0]}))  # 列不足


# ---------------------------------------------------------------------------
# CLI スモーク（--synthetic）
# ---------------------------------------------------------------------------


def test_fundamentals_report_cli_synthetic() -> None:
    proc = subprocess.run(
        [sys.executable, "analysis/fundamentals_report.py", "7203", "--years", "5", "--synthetic"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    assert report_path.name == f"fundamentals-7203-{TODAY}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "免責事項" in content
    assert "合成データ" in content  # 合成データである旨の明記
    assert "業績推移" in content
    assert "売上高CAGR" in content
    assert "EDINET" in content  # 合成モードでは問い合わせない旨のセクション


def test_fundamentals_report_cli_rejects_bad_years() -> None:
    proc = subprocess.run(
        [sys.executable, "analysis/fundamentals_report.py", "7203", "--years", "0", "--synthetic"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1
    assert "エラー" in proc.stderr

"""stocklib.adr（ADRパリティ計算）のテスト。

固定値でのパリティ式（理論ADR価格・乖離の符号・円換算の往復）、ratio 換算、
対応表 CSV の読み込み・バリデーション、CLI ``--synthetic`` のスモークを検証する。
ネットワーク不使用。
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import pytest

from stocklib import adr

REPO_ROOT = Path(__file__).resolve().parents[2]
TODAY = dt.date.today().isoformat()


# --- compute_parity: パリティ式 ---

def test_theoretical_price_formula() -> None:
    """P_ADR = P_東証 × n / USDJPY。3000円 × 10株 ÷ 150円 = 200ドル。"""
    r = adr.compute_parity(tse_close=3000.0, adr_close=200.0, usdjpy_close=150.0, ratio=10.0)
    assert r.theoretical_adr_usd == pytest.approx(200.0)
    assert r.premium_pct == pytest.approx(0.0)


def test_premium_sign_positive_when_adr_rich() -> None:
    """ADR終値が理論値より高ければ乖離は正（+5%）。"""
    r = adr.compute_parity(tse_close=3000.0, adr_close=210.0, usdjpy_close=150.0, ratio=10.0)
    assert r.premium_pct == pytest.approx(0.05)


def test_premium_sign_negative_when_adr_cheap() -> None:
    """ADR終値が理論値より低ければ乖離は負（−10%）。"""
    r = adr.compute_parity(tse_close=3000.0, adr_close=180.0, usdjpy_close=150.0, ratio=10.0)
    assert r.premium_pct == pytest.approx(-0.10)


def test_implied_jpy_conversion() -> None:
    """円換算ADR価格 = ADR × ドル円 ÷ ratio。210ドル × 150 ÷ 10 = 3150円。"""
    r = adr.compute_parity(tse_close=3000.0, adr_close=210.0, usdjpy_close=150.0, ratio=10.0)
    assert r.adr_implied_jpy == pytest.approx(3150.0)


def test_fractional_ratio_conversion() -> None:
    """ratio < 1（例: 武田 TAK は 0.5株 = 1ADR）でも式が正しい。"""
    r = adr.compute_parity(tse_close=4000.0, adr_close=12.5, usdjpy_close=160.0, ratio=0.5)
    assert r.theoretical_adr_usd == pytest.approx(4000.0 * 0.5 / 160.0)  # 12.5ドル
    assert r.premium_pct == pytest.approx(0.0)
    assert r.adr_implied_jpy == pytest.approx(12.5 * 160.0 / 0.5)  # 4000円


def test_round_trip_theoretical_implies_tse_close() -> None:
    """ADR終値 = 理論値のとき、円換算ADR価格は東証終値に厳密に一致する（往復整合）。"""
    tse, fx, n = 2871.5, 153.42, 10.0
    theoretical = tse * n / fx
    r = adr.compute_parity(tse_close=tse, adr_close=theoretical, usdjpy_close=fx, ratio=n)
    assert r.adr_implied_jpy == pytest.approx(tse)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tse_close": 0.0, "adr_close": 10.0, "usdjpy_close": 150.0, "ratio": 1.0},
        {"tse_close": 3000.0, "adr_close": -1.0, "usdjpy_close": 150.0, "ratio": 1.0},
        {"tse_close": 3000.0, "adr_close": 10.0, "usdjpy_close": 0.0, "ratio": 1.0},
        {"tse_close": 3000.0, "adr_close": 10.0, "usdjpy_close": 150.0, "ratio": 0.0},
    ],
)
def test_compute_parity_rejects_nonpositive_inputs(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        adr.compute_parity(**kwargs)


# --- load_adr_map: 対応表の読み込み ---

def test_load_adr_map_default() -> None:
    mappings = adr.load_adr_map()
    by_code = {m.code: m for m in mappings}
    assert "7203" in by_code
    assert by_code["7203"].adr_ticker == "TM"
    assert by_code["7203"].ratio == pytest.approx(10.0)
    assert by_code["7203"].listing == "NYSE"
    assert by_code["7974"].listing == "OTC"  # 任天堂 NTDOY はアンスポンサード（OTC）
    assert all(m.ratio > 0 for m in mappings)


def test_load_adr_map_rejects_missing_columns(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("code,adr_ticker\n7203,TM\n", encoding="utf-8")
    with pytest.raises(ValueError, match="列が必要"):
        adr.load_adr_map(bad)


def test_load_adr_map_rejects_nonpositive_ratio(tmp_path: Path) -> None:
    bad = tmp_path / "bad_ratio.csv"
    bad.write_text(
        "code,adr_ticker,ratio,listing\n7203,TM,0,NYSE\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="正の数"):
        adr.load_adr_map(bad)


# --- evaluate_mapping: 合成データでの一気通貫 ---

def test_evaluate_mapping_synthetic() -> None:
    mapping = adr.AdrMapping(code="7203", adr_ticker="TM", ratio=10.0, listing="NYSE")
    result, tse_date, adr_date, fx_date = adr.evaluate_mapping(
        mapping, period="3mo", synthetic=True
    )
    # 導出値が入力値と式で整合していること
    assert result.theoretical_adr_usd == pytest.approx(
        result.tse_close * 10.0 / result.usdjpy_close
    )
    assert result.premium_pct == pytest.approx(
        result.adr_close / result.theoretical_adr_usd - 1.0
    )
    assert result.adr_implied_jpy == pytest.approx(
        result.adr_close * result.usdjpy_close / 10.0
    )
    assert isinstance(tse_date, dt.date)
    assert isinstance(adr_date, dt.date)
    assert isinstance(fx_date, dt.date)


# --- CLI スモーク（subprocess + --synthetic、ネットワーク不使用） ---

def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_adr_parity_cli_all_synthetic() -> None:
    proc = _run("analysis/adr_parity.py", "--all", "--synthetic")
    assert proc.returncode == 0, proc.stderr
    report_path = REPO_ROOT / "reports" / f"adr-{TODAY}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "合成データ" in content
    assert "免責事項" in content
    assert "同一暦日ではない" in content
    assert "TM" in content and "NTDOY" in content


def test_adr_parity_cli_single_code_synthetic() -> None:
    proc = _run("analysis/adr_parity.py", "7203", "--synthetic")
    assert proc.returncode == 0, proc.stderr
    assert "TM" in proc.stdout


def test_adr_parity_cli_unknown_code_fails() -> None:
    proc = _run("analysis/adr_parity.py", "9999", "--synthetic")
    assert proc.returncode == 1
    assert "未登録" in proc.stderr


def test_adr_parity_cli_requires_codes_or_all() -> None:
    proc = _run("analysis/adr_parity.py", "--synthetic")
    assert proc.returncode == 2  # argparse.error

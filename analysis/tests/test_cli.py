"""CLI 4本のスモークテスト（subprocess + --synthetic、ネットワーク不使用）。"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TODAY = dt.date.today().isoformat()


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_analyze_stock_cli() -> None:
    proc = _run("analysis/analyze_stock.py", "7203", "--period", "1y", "--synthetic")
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    assert report_path.name == f"analyze-7203-{TODAY}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "免責事項" in content
    assert "リスク・リターン指標" in content


def test_screen_cli() -> None:
    proc = _run(
        "analysis/screen.py",
        "--period", "6mo",
        "--rsi-below", "70",
        "--synthetic",
    )
    assert proc.returncode == 0, proc.stderr
    assert "code" in proc.stdout or "合致する銘柄はありません" in proc.stdout
    report_path = REPO_ROOT / "reports" / f"screen-{TODAY}.md"
    assert report_path.exists()
    assert "免責事項" in report_path.read_text(encoding="utf-8")


def test_compare_cli() -> None:
    proc = _run("analysis/compare.py", "7203", "6758", "9984", "--period", "1y", "--synthetic")
    assert proc.returncode == 0, proc.stderr
    assert "相関行列" in proc.stdout
    report_path = REPO_ROOT / "reports" / f"compare-7203-6758-9984-{TODAY}.md"
    assert report_path.exists()


def test_run_backtest_cli() -> None:
    proc = _run(
        "analysis/run_backtest.py",
        "--strategy", "ma_cross",
        "--code", "7203",
        "--fast", "25",
        "--slow", "75",
        "--cost-bps", "10",
        "--period", "2y",
        "--synthetic",
    )
    assert proc.returncode == 0, proc.stderr
    assert "t統計量" in proc.stdout
    # スキル本文の規約どおり backtest-<strategy>-<code>-<日付>.md
    report_path = REPO_ROOT / "reports" / f"backtest-ma_cross-7203-{TODAY}.md"
    assert report_path.exists()
    assert "免責事項" in report_path.read_text(encoding="utf-8")


def test_run_backtest_cli_split_and_sweep() -> None:
    proc = _run(
        "analysis/run_backtest.py",
        "--strategy", "rsi_reversal",
        "--code", "7203",
        "--rsi-window", "14",
        "--rsi-lower", "30",
        "--rsi-upper", "50",
        "--split", "0.7",
        "--sweep",
        "--period", "2y",
        "--synthetic",
    )
    assert proc.returncode == 0, proc.stderr
    assert "IS/OOS 分割検証" in proc.stdout
    assert "パラメータ頑健性" in proc.stdout
    assert "試行回数 N=" in proc.stdout
    assert "過剰適合" in proc.stdout
    report_path = REPO_ROOT / "reports" / f"backtest-rsi_reversal-7203-{TODAY}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "多重検定に関する注意" in content
    assert "免責事項" in content


def test_run_backtest_cli_rejects_bad_split() -> None:
    proc = _run(
        "analysis/run_backtest.py",
        "--strategy", "ma_cross",
        "--code", "7203",
        "--split", "1.5",
        "--synthetic",
    )
    assert proc.returncode == 1
    assert "エラー" in proc.stderr


def test_compare_cli_rejects_single_code() -> None:
    proc = _run("analysis/compare.py", "7203", "--synthetic")
    assert proc.returncode == 1
    assert "2つ以上" in proc.stderr


def test_run_backtest_cli_rejects_unknown_strategy() -> None:
    proc = _run("analysis/run_backtest.py", "--strategy", "unknown", "--code", "7203", "--synthetic")
    assert proc.returncode != 0

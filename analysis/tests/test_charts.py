"""stocklib.charts と CLI のチャート生成テスト（合成データ、ネットワーク不使用）。"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from stocklib import charts
from stocklib.data import synthetic_prices

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


def _assert_png(path: Path) -> None:
    assert path.exists(), f"PNG が生成されていません: {path}"
    assert path.stat().st_size > 0, f"PNG が空ファイルです: {path}"


# --- plot 関数単体（tmp_path に出力） ---


def test_charts_available() -> None:
    assert charts.charts_available() is True


def test_plot_price_chart(tmp_path: Path) -> None:
    df = synthetic_prices("7203", days=300)
    out = charts.plot_price_chart(df, "7203", tmp_path / "price.png")
    _assert_png(out)


def test_plot_price_chart_options(tmp_path: Path) -> None:
    df = synthetic_prices("6758", days=120)
    out = charts.plot_price_chart(
        df, "6758", tmp_path / "price-opt.png", sma_windows=(5, 20, 60), with_bollinger=False
    )
    _assert_png(out)


def test_plot_relative_performance(tmp_path: Path) -> None:
    dfs = {code: synthetic_prices(code, days=250) for code in ("7203", "6758", "9984")}
    out = charts.plot_relative_performance(dfs, tmp_path / "relative.png")
    _assert_png(out)


def test_plot_relative_performance_rejects_no_common_dates(tmp_path: Path) -> None:
    a = synthetic_prices("7203", days=50)
    b = synthetic_prices("6758", days=50)
    b.index = b.index + pd.DateOffset(years=10)  # 共通取引日をなくす
    with pytest.raises(ValueError):
        charts.plot_relative_performance({"7203": a, "6758": b}, tmp_path / "never.png")


def test_plot_drawdown_from_dataframe(tmp_path: Path) -> None:
    df = synthetic_prices("7203", days=250)
    out = charts.plot_drawdown(df, tmp_path / "dd-df.png")
    _assert_png(out)


def test_plot_drawdown_from_series(tmp_path: Path) -> None:
    equity = synthetic_prices("9984", days=250)["Close"]
    out = charts.plot_drawdown(equity, tmp_path / "dd-series.png", title="9984 Equity & Drawdown")
    _assert_png(out)


# --- CLI 組み込み（既定でチャート埋め込み、--no-charts で無効化） ---


def test_analyze_stock_cli_embeds_chart() -> None:
    proc = _run("analysis/analyze_stock.py", "7203", "--period", "1y", "--synthetic")
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    content = report_path.read_text(encoding="utf-8")
    img_name = f"analyze-7203-{TODAY}-price.png"
    assert f"![chart](img/{img_name})" in content
    _assert_png(REPO_ROOT / "reports" / "img" / img_name)


def test_analyze_stock_cli_no_charts() -> None:
    proc = _run(
        "analysis/analyze_stock.py", "9984", "--period", "1y", "--synthetic", "--no-charts"
    )
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    content = report_path.read_text(encoding="utf-8")
    assert "![chart]" not in content
    assert not (REPO_ROOT / "reports" / "img" / f"analyze-9984-{TODAY}-price.png").exists()


def test_compare_cli_embeds_chart() -> None:
    proc = _run("analysis/compare.py", "7203", "6758", "--period", "1y", "--synthetic")
    assert proc.returncode == 0, proc.stderr
    img_name = f"compare-7203-6758-{TODAY}-relative.png"
    report_path = REPO_ROOT / "reports" / f"compare-7203-6758-{TODAY}.md"
    assert f"![chart](img/{img_name})" in report_path.read_text(encoding="utf-8")
    _assert_png(REPO_ROOT / "reports" / "img" / img_name)


def test_compare_cli_no_charts() -> None:
    proc = _run(
        "analysis/compare.py", "6861", "8035", "--period", "1y", "--synthetic", "--no-charts"
    )
    assert proc.returncode == 0, proc.stderr
    report_path = REPO_ROOT / "reports" / f"compare-6861-8035-{TODAY}.md"
    assert "![chart]" not in report_path.read_text(encoding="utf-8")


def test_run_backtest_cli_embeds_chart() -> None:
    proc = _run(
        "analysis/run_backtest.py",
        "--strategy", "ma_cross",
        "--code", "7203",
        "--period", "2y",
        "--synthetic",
    )
    assert proc.returncode == 0, proc.stderr
    img_name = f"backtest-ma_cross-7203-{TODAY}-equity.png"
    report_path = REPO_ROOT / "reports" / f"backtest-ma_cross-7203-{TODAY}.md"
    assert f"![chart](img/{img_name})" in report_path.read_text(encoding="utf-8")
    _assert_png(REPO_ROOT / "reports" / "img" / img_name)


def test_run_backtest_cli_no_charts() -> None:
    proc = _run(
        "analysis/run_backtest.py",
        "--strategy", "rsi_reversal",
        "--code", "6758",
        "--period", "2y",
        "--synthetic",
        "--no-charts",
    )
    assert proc.returncode == 0, proc.stderr
    report_path = REPO_ROOT / "reports" / f"backtest-rsi_reversal-6758-{TODAY}.md"
    assert "![chart]" not in report_path.read_text(encoding="utf-8")
    assert not (
        REPO_ROOT / "reports" / "img" / f"backtest-rsi_reversal-6758-{TODAY}-equity.png"
    ).exists()

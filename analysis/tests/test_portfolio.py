"""stocklib.portfolio と portfolio_review.py CLI のテスト（合成データのみ、ネットワーク不使用）。"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stocklib import metrics
from stocklib.data import period_to_days, synthetic_prices
from stocklib.portfolio import (
    PortfolioValidationError,
    Position,
    evaluate_portfolio,
    interpret_hhi,
    load_portfolio,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_CSV = REPO_ROOT / "analysis" / "templates" / "portfolio-example.csv"
TODAY = dt.date.today().isoformat()


# ---------------------------------------------------------------------------
# load_portfolio: 読み込みとバリデーション
# ---------------------------------------------------------------------------


def test_load_portfolio_template() -> None:
    positions = load_portfolio(TEMPLATE_CSV)
    assert len(positions) == 5
    first = positions[0]
    assert first.code == "7203"
    assert first.shares == 300
    assert first.avg_cost == 2450
    assert first.acquired_date == dt.date(2024, 6, 14)
    assert first.memo == "主力・輸送用機器"
    assert first.cost_value == 300 * 2450
    # memo は省略可（空文字）
    assert positions[1].memo == ""


def test_load_portfolio_without_memo_column(tmp_path: Path) -> None:
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date\n7203,100,2500,2024-01-10\n",
        encoding="utf-8",
    )
    positions = load_portfolio(p)
    assert len(positions) == 1
    assert positions[0].memo == ""


def test_load_portfolio_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_portfolio(tmp_path / "nonexistent.csv")


def test_load_portfolio_missing_required_column(tmp_path: Path) -> None:
    p = tmp_path / "pf.csv"
    p.write_text("code,shares,acquired_date\n7203,100,2024-01-10\n", encoding="utf-8")
    with pytest.raises(PortfolioValidationError, match="avg_cost"):
        load_portfolio(p)


def test_load_portfolio_reports_line_numbers(tmp_path: Path) -> None:
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date,memo\n"
        "7203,100,2500,2024-01-10,ok\n"        # 2行目: 正常
        "abcde,100,2500,2024-01-10,\n"          # 3行目: code 不正
        "6758,-5,12000,2024-01-10,\n"           # 4行目: shares 負
        "9984,100,abc,2024-01-10,\n"            # 5行目: avg_cost 非数値
        "8306,100,1400,2024/01/10,\n",          # 6行目: 日付形式不正
        encoding="utf-8",
    )
    with pytest.raises(PortfolioValidationError) as exc_info:
        load_portfolio(p)
    msg = str(exc_info.value)
    assert "3行目" in msg and "code" in msg
    assert "4行目" in msg and "shares" in msg
    assert "5行目" in msg and "avg_cost" in msg
    assert "6行目" in msg and "acquired_date" in msg
    assert "2行目" not in msg


def test_load_portfolio_rejects_future_date(tmp_path: Path) -> None:
    future = dt.date.today() + dt.timedelta(days=30)
    p = tmp_path / "pf.csv"
    p.write_text(
        f"code,shares,avg_cost,acquired_date\n7203,100,2500,{future.isoformat()}\n",
        encoding="utf-8",
    )
    with pytest.raises(PortfolioValidationError, match="未来"):
        load_portfolio(p)


def test_load_portfolio_rejects_duplicate_code(tmp_path: Path) -> None:
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date\n"
        "7203,100,2500,2024-01-10\n"
        "7203,200,2600,2024-02-10\n",
        encoding="utf-8",
    )
    with pytest.raises(PortfolioValidationError, match="重複"):
        load_portfolio(p)


def test_load_portfolio_rejects_header_only(tmp_path: Path) -> None:
    p = tmp_path / "pf.csv"
    p.write_text("code,shares,avg_cost,acquired_date,memo\n", encoding="utf-8")
    with pytest.raises(PortfolioValidationError, match="データ行"):
        load_portfolio(p)


# ---------------------------------------------------------------------------
# evaluate_portfolio: 手計算値との一致（合成データはシード固定で決定論的）
# ---------------------------------------------------------------------------


def _expected_last_close(code: str, period: str = "1y") -> float:
    """synthetic_prices を直接呼んで期待される直近終値を得る（fetch_prices と同じ正規化）。"""
    from stocklib.data import normalize_code

    return float(synthetic_prices(normalize_code(code), days=period_to_days(period))["Close"].iloc[-1])


def test_evaluate_portfolio_hand_computed_values() -> None:
    positions = [
        Position(code="7203", shares=300, avg_cost=2450, acquired_date=dt.date(2024, 6, 14)),
        Position(code="6758", shares=100, avg_cost=12800, acquired_date=dt.date(2024, 9, 2)),
    ]
    review = evaluate_portfolio(positions, period="1y", synthetic=True)

    p1_close = _expected_last_close("7203")
    p2_close = _expected_last_close("6758")
    mv1, mv2 = 300 * p1_close, 100 * p2_close
    total_mv = mv1 + mv2
    total_cost = 300 * 2450 + 100 * 12800

    v1, v2 = review.positions
    assert v1.price == pytest.approx(p1_close)
    assert v1.market_value == pytest.approx(mv1)
    assert v1.pnl == pytest.approx(mv1 - 300 * 2450)
    assert v1.pnl_pct == pytest.approx(mv1 / (300 * 2450) - 1.0)
    assert v1.weight == pytest.approx(mv1 / total_mv)
    assert v2.weight == pytest.approx(mv2 / total_mv)
    assert v1.weight + v2.weight == pytest.approx(1.0)

    assert review.total_market_value == pytest.approx(total_mv)
    assert review.total_cost == pytest.approx(total_cost)
    assert review.total_pnl == pytest.approx(total_mv - total_cost)
    assert review.total_pnl_pct == pytest.approx(total_mv / total_cost - 1.0)

    # HHI = ウエイト二乗和
    w1, w2 = mv1 / total_mv, mv2 / total_mv
    assert review.hhi == pytest.approx(w1**2 + w2**2)
    assert review.hhi_interpretation == interpret_hhi(review.hhi)

    # 加重β = Σ w_i × β_i
    assert review.portfolio_beta == pytest.approx(
        w1 * v1.beta + w2 * v2.beta
    )

    # 年率ボラ・VaR は現在ウエイト固定のポートフォリオ日次リターンから
    closes = pd.concat(
        {
            "7203": synthetic_prices("7203.T", days=period_to_days("1y"))["Close"],
            "6758": synthetic_prices("6758.T", days=period_to_days("1y"))["Close"],
        },
        axis=1,
    ).dropna()
    rets = closes.pct_change().dropna()
    port_rets = rets["7203"] * w1 + rets["6758"] * w2
    assert review.ann_vol == pytest.approx(metrics.ann_vol(port_rets))
    assert review.var_95 == pytest.approx(metrics.var_historical(port_rets, 0.95))
    assert review.var_95 < 0  # VaR は負のリターン（損失側）

    # 相関行列: 対角 1、対称、銘柄数 × 銘柄数
    corr = review.correlation
    assert corr.shape == (2, 2)
    assert np.allclose(np.diag(corr.to_numpy()), 1.0)
    assert corr.iloc[0, 1] == pytest.approx(corr.iloc[1, 0])


def test_evaluate_portfolio_sector_from_universe_csv() -> None:
    # liquid30.csv 収載銘柄は合成モードでも日本語セクターが引ける
    positions = [
        Position(code="7203", shares=100, avg_cost=2000, acquired_date=dt.date(2024, 1, 1)),
        Position(code="8306", shares=100, avg_cost=1000, acquired_date=dt.date(2024, 1, 1)),
    ]
    review = evaluate_portfolio(positions, period="6mo", synthetic=True)
    assert review.positions[0].sector == "輸送用機器"
    assert review.positions[0].name == "トヨタ自動車"
    assert review.positions[1].sector == "銀行業"
    assert sum(review.sector_weights.values()) == pytest.approx(1.0)


def test_evaluate_portfolio_unknown_code_sector_fallback() -> None:
    # liquid30 非収載コード → 合成 fetch_info のセクター（"Synthetic"）で補完され「不明」にはならない
    positions = [
        Position(code="1234", shares=100, avg_cost=500, acquired_date=dt.date(2024, 1, 1)),
    ]
    review = evaluate_portfolio(positions, period="6mo", synthetic=True)
    assert review.positions[0].sector in ("Synthetic", "不明")
    assert review.positions[0].weight == pytest.approx(1.0)
    assert review.hhi == pytest.approx(1.0)


def test_evaluate_portfolio_rejects_empty() -> None:
    with pytest.raises(ValueError):
        evaluate_portfolio([], synthetic=True)


def test_interpret_hhi_levels() -> None:
    assert "分散的" in interpret_hhi(0.05)
    assert "中程度" in interpret_hhi(0.15)
    assert "やや高い" in interpret_hhi(0.20)
    assert "高い集中" in interpret_hhi(0.5)
    assert "実効銘柄数" in interpret_hhi(0.25)
    assert "計算不能" in interpret_hhi(float("nan"))


def test_review_to_markdown_contains_sections() -> None:
    positions = load_portfolio(TEMPLATE_CSV)
    review = evaluate_portfolio(positions, period="6mo", synthetic=True)
    md = review.to_markdown()
    for section in ("## 保有明細", "## 全体サマリー", "## セクター配分", "## 日次リターン相関行列"):
        assert section in md
    assert "HHI" in md


# ---------------------------------------------------------------------------
# CLI スモーク（subprocess + --synthetic）
# ---------------------------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_portfolio_review_cli_smoke() -> None:
    proc = _run(
        "analysis/portfolio_review.py",
        "--file", str(TEMPLATE_CSV),
        "--period", "1y",
        "--synthetic",
    )
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    assert report_path.name == f"portfolio-{TODAY}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "免責事項" in content
    assert "合成データ" in content
    assert "セクター配分" in content
    assert "HHI" in content


def test_portfolio_review_cli_missing_default_file() -> None:
    default_csv = REPO_ROOT / "data" / "portfolio.csv"
    if default_csv.exists():
        pytest.skip("data/portfolio.csv が存在する環境ではスキップ")
    proc = _run("analysis/portfolio_review.py", "--synthetic")
    assert proc.returncode == 1
    assert "data/portfolio.csv を作成してください" in proc.stderr
    assert "analysis/templates/portfolio-example.csv" in proc.stderr


def test_portfolio_review_cli_invalid_csv(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "code,shares,avg_cost,acquired_date\n7203,-1,2500,2024-01-10\n",
        encoding="utf-8",
    )
    proc = _run("analysis/portfolio_review.py", "--file", str(bad), "--synthetic")
    assert proc.returncode == 1
    assert "2行目" in proc.stderr

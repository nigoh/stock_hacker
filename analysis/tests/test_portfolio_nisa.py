"""ポートフォリオの NISA 対応（account 列・NISA口座状況節）のテスト。

合成データのみ・ネットワーク不使用。account 列の後方互換（列なし CSV は従来どおり）、
枠使用率の手計算一致、非課税メリット計算を検証する。
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import pytest

from stocklib.data import normalize_code, period_to_days, synthetic_prices
from stocklib.portfolio import (
    ACCOUNT_NISA_GROWTH,
    ACCOUNT_NISA_TSUMITATE,
    ACCOUNT_TAXABLE,
    CAPITAL_GAINS_TAX_RATE,
    NISA_ANNUAL_LIMITS,
    NISA_LIFETIME_GROWTH_LIMIT,
    NISA_LIFETIME_LIMIT,
    PortfolioValidationError,
    Position,
    build_nisa_summary,
    evaluate_portfolio,
    load_portfolio,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_CSV = REPO_ROOT / "analysis" / "templates" / "portfolio-example.csv"


def _last_close(code: str, period: str = "1y") -> float:
    """合成データの期待される直近終値（fetch_prices と同じ正規化）。"""
    return float(
        synthetic_prices(normalize_code(code), days=period_to_days(period))["Close"].iloc[-1]
    )


# ---------------------------------------------------------------------------
# load_portfolio: account 列の読み込みと後方互換
# ---------------------------------------------------------------------------


def test_load_portfolio_without_account_column(tmp_path: Path) -> None:
    """account 列の無い既存 CSV は従来どおり読める（account は None = taxable 扱い）。"""
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date\n7203,100,2500,2024-01-10\n",
        encoding="utf-8",
    )
    positions = load_portfolio(p)
    assert positions[0].account is None
    assert positions[0].account_type == ACCOUNT_TAXABLE


def test_load_portfolio_with_account_column(tmp_path: Path) -> None:
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date,account\n"
        "7203,100,2500,2024-01-10,nisa_growth\n"
        "6758,50,13000,2024-06-03,taxable\n"
        "9984,10,8200,2024-06-03,\n"          # 空欄 → None（taxable 扱い）
        "8306,100,1450,2024-06-03,NISA_TSUMITATE\n",  # 大文字も許容
        encoding="utf-8",
    )
    positions = load_portfolio(p)
    assert positions[0].account == ACCOUNT_NISA_GROWTH
    assert positions[1].account == ACCOUNT_TAXABLE
    assert positions[2].account is None
    assert positions[2].account_type == ACCOUNT_TAXABLE
    assert positions[3].account == ACCOUNT_NISA_TSUMITATE


def test_load_portfolio_rejects_invalid_account(tmp_path: Path) -> None:
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date,account\n"
        "7203,100,2500,2024-01-10,nisa\n",  # 2行目: 不正な区分
        encoding="utf-8",
    )
    with pytest.raises(PortfolioValidationError) as exc_info:
        load_portfolio(p)
    msg = str(exc_info.value)
    assert "2行目" in msg and "account" in msg
    assert "nisa_tsumitate" in msg and "nisa_growth" in msg and "taxable" in msg


def test_load_portfolio_template_has_account_examples() -> None:
    """テンプレート CSV に account 列の記入例（nisa_growth・taxable・空欄）がある。"""
    positions = load_portfolio(TEMPLATE_CSV)
    accounts = {p.code: p.account for p in positions}
    assert accounts["7203"] == ACCOUNT_TAXABLE
    assert accounts["6758"] == ACCOUNT_NISA_GROWTH
    assert accounts["9984"] is None  # 空欄 → taxable 扱い


# ---------------------------------------------------------------------------
# build_nisa_summary: 手計算一致
# ---------------------------------------------------------------------------


def _positions_mixed() -> list[Position]:
    return [
        # 2024年 成長投資枠: 100×2500 = 250,000
        Position(code="7203", shares=100, avg_cost=2500, acquired_date=dt.date(2024, 6, 14),
                 account=ACCOUNT_NISA_GROWTH),
        # 2024年 成長投資枠: 100×12800 = 1,280,000（同一年で合算 → 1,530,000）
        Position(code="6758", shares=100, avg_cost=12800, acquired_date=dt.date(2024, 9, 2),
                 account=ACCOUNT_NISA_GROWTH),
        # 2025年 つみたて投資枠: 100×3000 = 300,000
        Position(code="4568", shares=100, avg_cost=3000, acquired_date=dt.date(2025, 3, 5),
                 account=ACCOUNT_NISA_TSUMITATE),
        # 課税口座（明示）
        Position(code="8306", shares=500, avg_cost=1450, acquired_date=dt.date(2024, 4, 10),
                 account=ACCOUNT_TAXABLE),
        # 未指定（None）→ taxable 扱い
        Position(code="9984", shares=100, avg_cost=8200, acquired_date=dt.date(2025, 1, 20)),
    ]


def test_build_nisa_summary_none_when_no_account_column() -> None:
    """全銘柄 account=None（列なし CSV 相当）なら NISA サマリーは作らない（後方互換）。"""
    positions = [
        Position(code="7203", shares=100, avg_cost=2500, acquired_date=dt.date(2024, 1, 10)),
        Position(code="6758", shares=50, avg_cost=13000, acquired_date=dt.date(2024, 6, 3)),
    ]
    mv = {"7203": 300_000.0, "6758": 700_000.0}
    assert build_nisa_summary(positions, mv) is None


def test_build_nisa_summary_hand_computed() -> None:
    positions = _positions_mixed()
    # 評価額は任意の固定値（手計算しやすい値）で検証する
    mv = {
        "7203": 300_000.0,   # nisa_growth: 簿価 250,000 → 含み益 +50,000
        "6758": 1_200_000.0, # nisa_growth: 簿価 1,280,000 → 含み損 -80,000
        "4568": 400_000.0,   # nisa_tsumitate: 簿価 300,000 → 含み益 +100,000
        "8306": 800_000.0,   # taxable
        "9984": 820_000.0,   # taxable（未指定）
    }
    summary = build_nisa_summary(positions, mv)
    assert summary is not None

    # 口座区分別の内訳（tsumitate → growth → taxable の順）
    by_account = {b.account: b for b in summary.breakdown}
    assert [b.account for b in summary.breakdown] == [
        ACCOUNT_NISA_TSUMITATE, ACCOUNT_NISA_GROWTH, ACCOUNT_TAXABLE,
    ]
    growth = by_account[ACCOUNT_NISA_GROWTH]
    assert growth.n_positions == 2
    assert growth.cost_value == pytest.approx(250_000 + 1_280_000)
    assert growth.market_value == pytest.approx(300_000 + 1_200_000)
    assert growth.pnl == pytest.approx(-30_000)
    assert growth.pnl_pct == pytest.approx(1_500_000 / 1_530_000 - 1.0)
    tsumitate = by_account[ACCOUNT_NISA_TSUMITATE]
    assert tsumitate.cost_value == pytest.approx(300_000)
    assert tsumitate.pnl == pytest.approx(100_000)
    taxable = by_account[ACCOUNT_TAXABLE]
    assert taxable.n_positions == 2  # 明示 taxable + 未指定
    assert taxable.cost_value == pytest.approx(500 * 1450 + 100 * 8200)

    # 年間投資枠: 同一年の取得分が合算される（taxable は含まない）
    assert set(summary.annual_usage.keys()) == {2024, 2025}
    assert summary.annual_usage[2024] == {
        ACCOUNT_NISA_GROWTH: pytest.approx(1_530_000),
    }
    assert summary.annual_usage[2025] == {
        ACCOUNT_NISA_TSUMITATE: pytest.approx(300_000),
    }
    # 使用率（レポート表示値の元）: 2024年成長枠 1,530,000 / 2,400,000
    assert summary.annual_usage[2024][ACCOUNT_NISA_GROWTH] / NISA_ANNUAL_LIMITS[
        ACCOUNT_NISA_GROWTH
    ] == pytest.approx(1_530_000 / 2_400_000)

    # 生涯投資枠（簿価残高方式）: NISA 簿価合計と成長枠内訳
    assert summary.lifetime_used == pytest.approx(1_530_000 + 300_000)
    assert summary.lifetime_growth_used == pytest.approx(1_530_000)
    assert summary.lifetime_used / NISA_LIFETIME_LIMIT == pytest.approx(
        1_830_000 / 18_000_000
    )
    assert summary.lifetime_growth_used / NISA_LIFETIME_GROWTH_LIMIT == pytest.approx(
        1_530_000 / 12_000_000
    )

    # 非課税メリット: NISA 含み損益合計（+50,000 -80,000 +100,000 = +70,000）× 20.315%
    assert summary.nisa_pnl == pytest.approx(70_000)
    assert summary.tax_benefit_estimate == pytest.approx(70_000 * CAPITAL_GAINS_TAX_RATE)
    assert CAPITAL_GAINS_TAX_RATE == pytest.approx(0.20315)


def test_build_nisa_summary_no_tax_benefit_when_nisa_in_loss() -> None:
    """NISA 口座が含み損なら非課税メリット推計は 0（負値にしない）。"""
    positions = [
        Position(code="7203", shares=100, avg_cost=5000, acquired_date=dt.date(2024, 1, 10),
                 account=ACCOUNT_NISA_GROWTH),
    ]
    mv = {"7203": 300_000.0}  # 簿価 500,000 → 含み損 -200,000
    summary = build_nisa_summary(positions, mv)
    assert summary is not None
    assert summary.nisa_pnl == pytest.approx(-200_000)
    assert summary.tax_benefit_estimate == 0.0


# ---------------------------------------------------------------------------
# evaluate_portfolio / to_markdown: 節の有無と数値の整合
# ---------------------------------------------------------------------------


def test_evaluate_portfolio_without_account_has_no_nisa_section() -> None:
    positions = [
        Position(code="7203", shares=100, avg_cost=2500, acquired_date=dt.date(2024, 1, 10)),
    ]
    review = evaluate_portfolio(positions, period="6mo", synthetic=True)
    assert review.nisa is None
    assert "NISA口座状況" not in review.to_markdown()


def test_evaluate_portfolio_with_account_builds_nisa_summary() -> None:
    positions = [
        Position(code="7203", shares=300, avg_cost=2450, acquired_date=dt.date(2024, 6, 14),
                 account=ACCOUNT_NISA_GROWTH),
        Position(code="6758", shares=100, avg_cost=12800, acquired_date=dt.date(2024, 9, 2),
                 account=ACCOUNT_TAXABLE),
    ]
    review = evaluate_portfolio(positions, period="1y", synthetic=True)
    assert review.nisa is not None

    # 評価額は合成データの直近終値ベースで build_nisa_summary と一致する
    mv_7203 = 300 * _last_close("7203")
    growth = next(
        b for b in review.nisa.breakdown if b.account == ACCOUNT_NISA_GROWTH
    )
    assert growth.market_value == pytest.approx(mv_7203)
    assert growth.cost_value == pytest.approx(300 * 2450)
    assert growth.pnl == pytest.approx(mv_7203 - 300 * 2450)
    assert review.nisa.lifetime_used == pytest.approx(300 * 2450)
    assert review.nisa.lifetime_growth_used == pytest.approx(300 * 2450)
    assert review.nisa.annual_usage == {
        2024: {ACCOUNT_NISA_GROWTH: pytest.approx(300 * 2450)}
    }
    expected_benefit = max(mv_7203 - 300 * 2450, 0.0) * CAPITAL_GAINS_TAX_RATE
    assert review.nisa.tax_benefit_estimate == pytest.approx(expected_benefit)

    md = review.to_markdown()
    assert "## NISA口座状況" in md
    assert "口座区分別の内訳" in md
    assert "NISA枠の使用状況" in md
    assert "非課税メリットの推計" in md
    assert "損益通算" in md  # 制度上の注意書き
    assert "20.315%" in md and "2025年時点" in md
    assert "2024年" in md or "2024" in md  # 制度年の付記


# ---------------------------------------------------------------------------
# CLI スモーク（テンプレート CSV + --synthetic で新節が出る）
# ---------------------------------------------------------------------------


def test_portfolio_review_cli_nisa_section_with_template() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "analysis/portfolio_review.py",
            "--file", str(TEMPLATE_CSV),
            "--period", "1y",
            "--synthetic",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "## NISA口座状況" in content
    assert "生涯投資枠" in content
    assert "免責事項" in content
    assert "合成データ" in content


def test_portfolio_review_cli_no_nisa_section_without_account(tmp_path: Path) -> None:
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date\n7203,100,2500,2024-01-10\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            "analysis/portfolio_review.py",
            "--file", str(p),
            "--period", "6mo",
            "--synthetic",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    content = report_path.read_text(encoding="utf-8")
    assert "NISA口座状況" not in content

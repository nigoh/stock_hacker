"""stocklib.income と income_report CLI のテスト（ネットワーク不要、モック/合成のみ）。"""

from __future__ import annotations

import datetime as dt
import math
import subprocess
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

from stocklib.income import (
    DIVIDEND_TAX_RATE,
    IncomeReport,
    PositionIncome,
    build_income_report,
    fetch_dividends,
    synthetic_dividends,
    ttm_dividend,
)
from stocklib.portfolio import CAPITAL_GAINS_TAX_RATE, Position

REPO_ROOT = Path(__file__).resolve().parents[2]
AS_OF = dt.date(2026, 7, 17)


def _pos(
    code: str,
    shares: float = 100,
    avg_cost: float = 2000.0,
    account: str | None = None,
    manual_price: float | None = None,
) -> Position:
    return Position(
        code=code,
        shares=shares,
        avg_cost=avg_cost,
        acquired_date=dt.date(2024, 6, 14),
        account=account,
        manual_price=manual_price,
    )


# --- synthetic_dividends -------------------------------------------------------


def test_synthetic_dividends_deterministic() -> None:
    a = synthetic_dividends("7203", as_of=AS_OF)
    b = synthetic_dividends("7203", as_of=AS_OF)
    pd.testing.assert_series_equal(a, b)
    assert (a > 0).all()
    assert a.index.is_monotonic_increasing


def test_synthetic_dividends_varies_by_code() -> None:
    a = synthetic_dividends("7203", as_of=AS_OF)
    b = synthetic_dividends("6758", as_of=AS_OF)
    assert not a.equals(b)


def test_synthetic_dividends_ttm_covers_two_payments() -> None:
    div = synthetic_dividends("7203", as_of=AS_OF, years=3)
    assert len(div) == 6  # 年2回 × 3年
    ttm = ttm_dividend(div, as_of=AS_OF)
    # 直近12ヶ月に半期配当2回（= 想定年間配当）が入る
    assert ttm == pytest.approx(float(div.iloc[-2:].sum()))
    assert ttm > 0


def test_synthetic_dividends_rejects_zero_years() -> None:
    with pytest.raises(ValueError):
        synthetic_dividends("7203", years=0)


# --- ttm_dividend --------------------------------------------------------------


def test_ttm_dividend_window() -> None:
    idx = pd.DatetimeIndex([
        pd.Timestamp(AS_OF) - pd.Timedelta(days=400),  # 窓の外（13ヶ月超前）
        pd.Timestamp(AS_OF) - pd.Timedelta(days=180),  # 窓の中
        pd.Timestamp(AS_OF) - pd.Timedelta(days=30),   # 窓の中
    ])
    div = pd.Series([50.0, 30.0, 40.0], index=idx)
    assert ttm_dividend(div, as_of=AS_OF) == pytest.approx(70.0)


def test_ttm_dividend_includes_as_of_day_and_excludes_future() -> None:
    idx = pd.DatetimeIndex([
        pd.Timestamp(AS_OF),                          # 基準日当日 → 含む
        pd.Timestamp(AS_OF) + pd.Timedelta(days=10),  # 未来 → 含まない
    ])
    div = pd.Series([25.0, 99.0], index=idx)
    assert ttm_dividend(div, as_of=AS_OF) == pytest.approx(25.0)


def test_ttm_dividend_empty_is_zero() -> None:
    assert ttm_dividend(pd.Series(dtype=float), as_of=AS_OF) == 0.0


# --- fetch_dividends（yfinance をモック） ---------------------------------------


def test_fetch_dividends_strips_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    idx = pd.DatetimeIndex(
        [pd.Timestamp("2026-03-30", tz="Asia/Tokyo"), pd.Timestamp("2025-09-29", tz="Asia/Tokyo")]
    )
    fake_series = pd.Series([40.0, 35.0], index=idx, name="Dividends")

    class _FakeTicker:
        def __init__(self, ticker: str) -> None:
            assert ticker == "7203.T"  # 4桁コードが .T に正規化されて渡る

        @property
        def dividends(self) -> pd.Series:
            return fake_series

    fake_yf = types.ModuleType("yfinance")
    fake_yf.Ticker = _FakeTicker  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    div = fetch_dividends("7203")
    assert div.index.tz is None
    assert div.index.is_monotonic_increasing
    assert float(div.sum()) == pytest.approx(75.0)


def test_fetch_dividends_empty_returns_empty_series(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeTicker:
        def __init__(self, ticker: str) -> None:
            pass

        @property
        def dividends(self) -> pd.Series:
            return pd.Series(dtype=float)

    fake_yf = types.ModuleType("yfinance")
    fake_yf.Ticker = _FakeTicker  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    div = fetch_dividends("9999")
    assert div.empty


def test_fetch_dividends_synthetic_needs_no_network() -> None:
    div = fetch_dividends("7203", synthetic=True)
    assert not div.empty
    assert (div > 0).all()


# --- PositionIncome（税計算・利回り） -------------------------------------------


def test_position_income_taxable() -> None:
    p = PositionIncome(
        code="7203", name="X", shares=100, avg_cost=2000.0, price=2500.0,
        dps_ttm=100.0, account="taxable",
    )
    assert p.annual_gross == pytest.approx(10_000.0)
    assert p.tax_rate == pytest.approx(CAPITAL_GAINS_TAX_RATE)
    assert p.tax_withheld == pytest.approx(10_000.0 * 0.20315)
    assert p.annual_net == pytest.approx(10_000.0 * (1 - 0.20315))
    assert p.yoc == pytest.approx(0.05)
    assert p.market_yield == pytest.approx(0.04)


def test_position_income_nisa_is_tax_free() -> None:
    for account in ("nisa_growth", "nisa_tsumitate"):
        p = PositionIncome(
            code="6758", name="X", shares=100, avg_cost=2000.0, price=2500.0,
            dps_ttm=100.0, account=account,
        )
        assert p.is_nisa
        assert p.tax_rate == 0.0
        assert p.tax_withheld == 0.0
        assert p.annual_net == pytest.approx(p.annual_gross)


def test_position_income_none_account_is_taxable() -> None:
    p = PositionIncome(
        code="9984", name="X", shares=100, avg_cost=2000.0, price=2500.0,
        dps_ttm=100.0, account=None,
    )
    assert not p.is_nisa
    assert p.tax_rate == pytest.approx(DIVIDEND_TAX_RATE)


def test_position_income_nan_guard() -> None:
    p = PositionIncome(
        code="9984", name="X", shares=100, avg_cost=0.0, price=float("nan"),
        dps_ttm=100.0,
    )
    assert math.isnan(p.yoc)
    assert math.isnan(p.market_yield)


# --- build_income_report（合成データ） ------------------------------------------


def test_build_income_report_totals_consistent() -> None:
    positions = [
        _pos("7203", shares=300, account="taxable"),
        _pos("6758", shares=100, account="nisa_growth"),
        _pos("9984", shares=100, account=None),
    ]
    rep = build_income_report(positions, synthetic=True, as_of=AS_OF)
    assert rep.synthetic
    assert rep.has_account
    assert len(rep.positions) == 3
    assert rep.total_gross == pytest.approx(sum(p.annual_gross for p in rep.positions))
    assert rep.total_net == pytest.approx(rep.total_gross - rep.total_tax)
    assert rep.monthly_net == pytest.approx(rep.total_net / 12.0)
    # NISA分の非課税メリット = NISA分税引前 × 20.315%
    nisa = [p for p in rep.positions if p.is_nisa]
    assert len(nisa) == 1
    assert rep.nisa_gross == pytest.approx(nisa[0].annual_gross)
    assert rep.nisa_tax_benefit == pytest.approx(rep.nisa_gross * 0.20315)
    # account 未指定（9984）は課税口座扱い
    p9984 = next(p for p in rep.positions if p.code == "9984")
    assert p9984.tax_rate == pytest.approx(0.20315)
    # 合成配当は全銘柄 TTM > 0 なので欠損なし
    assert rep.no_dividend == []


def test_build_income_report_without_account_column() -> None:
    positions = [_pos("7203"), _pos("6758")]
    rep = build_income_report(positions, synthetic=True, as_of=AS_OF)
    assert not rep.has_account
    # 全銘柄課税口座扱いの試算
    assert rep.total_tax == pytest.approx(rep.total_gross * 0.20315)
    md = rep.to_markdown()
    assert "全銘柄を課税口座" in md
    assert "NISA分の非課税メリット" not in md


def test_build_income_report_rejects_empty() -> None:
    with pytest.raises(ValueError):
        build_income_report([], synthetic=True)


def test_income_markdown_contains_required_notes() -> None:
    positions = [
        _pos("7203", shares=300, account="taxable"),
        _pos("6758", shares=100, account="nisa_growth"),
    ]
    rep = build_income_report(positions, synthetic=True, as_of=AS_OF)
    md = rep.to_markdown()
    # 減配リスク（実績配当は将来を保証しない）の明記
    assert "実績配当は将来の配当を保証しない" in md
    assert "減配" in md
    # 株式数比例配分方式の実務注意（NISA節）
    assert "株式数比例配分方式" in md
    assert "taxation-and-nisa.md" in md
    # yfinance データ品質の注記
    assert "data-apis-and-tools.md" in md
    # 税率と年時点表記
    assert "20.315%" in md
    assert "2025年時点" in md
    # 推奨しない（分析支援）
    assert "推奨するものではない" in md


def test_income_report_no_dividend_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    import stocklib.income as income_mod

    def _no_div(code: str, *, synthetic: bool = False) -> pd.Series:
        return pd.Series(dtype=float)

    monkeypatch.setattr(income_mod, "fetch_dividends", _no_div)
    rep = build_income_report([_pos("7203")], synthetic=True, as_of=AS_OF)
    assert rep.no_dividend == ["7203"]
    assert rep.total_gross == 0.0
    assert "配当実績が取得できなかった銘柄" in rep.to_markdown()


# --- manual_price 行（投信・現金の手入力評価。配当集計の対象外） -----------------


def test_manual_rows_get_no_synthetic_dividends() -> None:
    """回帰: manual_price 行（現金・投信）に合成配当が混入しない。"""
    positions = [
        _pos("7203", shares=300, account="taxable"),
        _pos("cash", shares=1_500_000, avg_cost=1.0, manual_price=1.0),
        _pos("emaxis-slim-allcountry", shares=400_000, avg_cost=2.5,
             account="nisa_tsumitate", manual_price=3.0),
    ]
    rep = build_income_report(positions, synthetic=True, as_of=AS_OF)
    assert len(rep.positions) == 3
    manual = {p.code: p for p in rep.positions if p.manual}
    assert set(manual) == {"cash", "emaxis-slim-allcountry"}
    for p in manual.values():
        assert p.dps_ttm == 0.0
        assert p.annual_gross == 0.0
        assert p.annual_net == 0.0
    assert manual["cash"].price == pytest.approx(1.0)
    assert manual["emaxis-slim-allcountry"].price == pytest.approx(3.0)
    # 合計は上場銘柄（7203）分のみ
    p7203 = next(p for p in rep.positions if p.code == "7203")
    assert not p7203.manual
    assert p7203.annual_gross > 0
    assert rep.total_gross == pytest.approx(p7203.annual_gross)
    # manual 行は no_dividend（データ欠損疑い）ではなく manual_codes に載る
    assert rep.no_dividend == []
    assert rep.manual_codes == ["cash", "emaxis-slim-allcountry"]


def test_manual_rows_skip_price_and_dividend_fetch_in_real_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回帰: 実データモードで manual 行の価格・配当取得が呼ばれない（cash で落ちない）。"""
    import stocklib.income as income_mod

    price_calls: list[list[str]] = []
    dividend_calls: list[str] = []

    def _fake_fetch_prices(
        codes: list[str], period: str = "1y", **kwargs: object
    ) -> dict[str, pd.DataFrame]:
        price_calls.append(list(codes))
        idx = pd.DatetimeIndex([pd.Timestamp(AS_OF)])
        return {c: pd.DataFrame({"Close": [2500.0]}, index=idx) for c in codes}

    def _fake_fetch_dividends(code: str, *, synthetic: bool = False) -> pd.Series:
        dividend_calls.append(code)
        return pd.Series([100.0], index=pd.DatetimeIndex([pd.Timestamp(AS_OF)]))

    monkeypatch.setattr(income_mod, "fetch_prices", _fake_fetch_prices)
    monkeypatch.setattr(income_mod, "fetch_dividends", _fake_fetch_dividends)

    positions = [
        _pos("7203", shares=300),
        _pos("cash", shares=1_500_000, avg_cost=1.0, manual_price=1.0),
    ]
    rep = build_income_report(positions, synthetic=False, as_of=AS_OF)
    assert price_calls == [["7203"]]  # manual 行は価格取得の対象外
    assert dividend_calls == ["7203"]  # manual 行は配当取得の対象外
    cash = next(p for p in rep.positions if p.code == "cash")
    assert cash.manual and cash.dps_ttm == 0.0 and cash.price == pytest.approx(1.0)


def test_all_manual_portfolio_needs_no_fetch_in_real_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全行 manual なら実データモードでも fetch_prices を一切呼ばない。"""
    import stocklib.income as income_mod

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("manual 行のみのポートフォリオで fetch が呼ばれた")

    monkeypatch.setattr(income_mod, "fetch_prices", _boom)
    monkeypatch.setattr(income_mod, "fetch_dividends", _boom)

    rep = build_income_report(
        [_pos("cash", shares=1_000_000, avg_cost=1.0, manual_price=1.0)],
        synthetic=False,
        as_of=AS_OF,
    )
    assert rep.total_gross == 0.0
    assert rep.manual_codes == ["cash"]


def test_manual_rows_markdown_footnote_separate_from_no_dividend() -> None:
    """manual 行の脚注は no_dividend（データ欠損疑い）の注記と分離される。"""
    positions = [
        _pos("7203", shares=300, account="taxable"),
        _pos("cash", shares=1_500_000, avg_cost=1.0, manual_price=1.0),
    ]
    rep = build_income_report(positions, synthetic=True, as_of=AS_OF)
    md = rep.to_markdown()
    # テーブルでは ※ 付きで対象外表示
    assert "cash※" in md
    # 脚注: manual_price 行は配当集計の対象外（投信の分配金・現金の利息は対象外）
    assert "manual_price" in md
    assert "配当集計の対象外" in md
    assert "分配金" in md and "利息" in md
    # no_dividend の注記（データ欠損疑い）には manual 行が混ざらない
    assert "配当実績が取得できなかった銘柄" not in md


# --- CLI（subprocess + --synthetic、test_cli.py の慣行に合わせる） ---------------


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _write_portfolio_csv(path: Path) -> None:
    path.write_text(
        "code,shares,avg_cost,acquired_date,memo,fx_at_cost,account\n"
        "7203,300,2450,2024-06-14,,,taxable\n"
        "6758,100,12800,2024-09-02,,,nisa_growth\n"
        "9984,100,8200,2025-01-20,,,\n",
        encoding="utf-8",
    )


def test_income_report_cli(tmp_path: Path) -> None:
    csv_path = tmp_path / "portfolio.csv"
    _write_portfolio_csv(csv_path)
    proc = _run("analysis/income_report.py", "--file", str(csv_path), "--synthetic")
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    assert report_path.name == f"income-{dt.date.today().isoformat()}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "免責事項" in content
    assert "合成データ" in content
    assert "株式数比例配分方式" in content
    assert "NISA分の非課税メリット" in content
    assert "月割り額" in content


def test_income_report_cli_template_with_manual_rows() -> None:
    """回帰: manual_price 行を含むテンプレート CSV で架空配当が混入しない。"""
    proc = _run(
        "analysis/income_report.py",
        "--file", "analysis/templates/portfolio-example.csv",
        "--synthetic",
    )
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    content = report_path.read_text(encoding="utf-8")
    # manual 行は ※ 付き・配当集計の対象外の脚注が出る
    assert "cash※" in content
    assert "emaxis-slim-allcountry※" in content
    assert "配当集計の対象外" in content
    # 修正前の症状（cash 1円×150万口への合成配当 → YOC 12099% 等）が出ない
    assert "12099" not in content
    assert "181,489,104" not in content


def test_income_report_cli_missing_file(tmp_path: Path) -> None:
    proc = _run("analysis/income_report.py", "--file", str(tmp_path / "nai.csv"), "--synthetic")
    assert proc.returncode == 1
    assert "見つかりません" in proc.stderr


def test_income_report_cli_invalid_csv(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("code,shares\n7203,100\n", encoding="utf-8")
    proc = _run("analysis/income_report.py", "--file", str(bad), "--synthetic")
    assert proc.returncode == 1
    assert "エラー" in proc.stderr

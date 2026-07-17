"""tax_report CLI（課税口座の含み損益 税価値ビュー）のテスト。

合成データ・一時 CSV のみでネットワーク不使用。税価値（含み損 × 20.315%）の
手計算一致、NISA 含み損の「損益通算・繰越控除の対象外」対比、必須の実務注意の
自動挿入、条件付き表現（売却の判断・断定をしない）を検証する。
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
    Position,
)

from tax_report import (
    TAX_COST_COLUMN,
    TAX_VALUE_COLUMN,
    build_tax_summary,
    resolve_last_prices,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TODAY = dt.date.today().isoformat()

#: レポートで使ってはいけない断定・推奨の表現（投資助言化の検出）。
#: 「を推奨する」は免責文（〜を推奨する投資助言ではありません）に正当に含まれる
#: ため、免責文が付く前の本文（to_markdown）のみで検査する。
FORBIDDEN_PHRASES: tuple[str, ...] = (
    "すべき",
    "売るべき",
    "買うべき",
    "推奨します",
    "損出ししましょう",
    "儲かる",
)
FORBIDDEN_PHRASES_BODY_ONLY: tuple[str, ...] = (*FORBIDDEN_PHRASES, "を推奨する")


def _last_close(code: str, period: str = "1y") -> float:
    """合成データの期待される直近終値（fetch_prices と同じ正規化）。"""
    return float(
        synthetic_prices(normalize_code(code), days=period_to_days(period))["Close"].iloc[-1]
    )


def _positions_mixed() -> list[Position]:
    return [
        # 課税口座・含み損: 100株 × (2,200 - 2,500) = -30,000
        Position(code="7203", shares=100, avg_cost=2500, acquired_date=dt.date(2024, 6, 14),
                 account=ACCOUNT_TAXABLE),
        # account 未指定（None）→ taxable 扱い・含み益: 10株 × (14,000 - 13,000) = +10,000
        Position(code="6758", shares=10, avg_cost=13000, acquired_date=dt.date(2024, 9, 2)),
        # NISA成長・含み損: 10株 × (7,200 - 8,200) = -10,000（損益通算・繰越の対象外）
        Position(code="9984", shares=10, avg_cost=8200, acquired_date=dt.date(2025, 1, 20),
                 account=ACCOUNT_NISA_GROWTH),
        # NISAつみたて・含み益: 100株 × (3,500 - 3,000) = +50,000
        Position(code="4568", shares=100, avg_cost=3000, acquired_date=dt.date(2025, 3, 5),
                 account=ACCOUNT_NISA_TSUMITATE),
    ]


_PRICES_MIXED: dict[str, float] = {
    "7203": 2200.0,
    "6758": 14000.0,
    "9984": 7200.0,
    "4568": 3500.0,
}


# ---------------------------------------------------------------------------
# build_tax_summary: 税価値の手計算一致と口座区分の振り分け
# ---------------------------------------------------------------------------


def test_build_tax_summary_hand_computed() -> None:
    summary = build_tax_summary(
        _positions_mixed(), _PRICES_MIXED, period="1y", synthetic=True,
    )

    # 口座区分の振り分け（未指定 = taxable 扱い）
    assert [v.code for v in summary.taxable] == ["7203", "6758"]
    assert [v.code for v in summary.nisa] == ["9984", "4568"]

    by_code = {v.code: v for v in [*summary.taxable, *summary.nisa]}
    loss = by_code["7203"]
    assert loss.pnl == pytest.approx(-30_000)
    assert loss.is_loss
    # 税価値 = 含み損 × 20.315%（2025年時点、stocklib.portfolio の定数を再利用）
    assert loss.tax_value_if_realized == pytest.approx(30_000 * CAPITAL_GAINS_TAX_RATE)
    assert loss.tax_value_if_realized == pytest.approx(6_094.5)
    assert loss.tax_cost_if_realized == 0.0
    assert CAPITAL_GAINS_TAX_RATE == pytest.approx(0.20315)

    gain = by_code["6758"]
    assert gain.account == ACCOUNT_TAXABLE  # None → taxable 扱い
    assert gain.pnl == pytest.approx(10_000)
    assert gain.tax_value_if_realized == 0.0
    assert gain.tax_cost_if_realized == pytest.approx(10_000 * CAPITAL_GAINS_TAX_RATE)

    # 集計（課税口座のみが税価値・課税見込みの対象）
    assert summary.taxable_loss_total == pytest.approx(-30_000)
    assert summary.taxable_gain_total == pytest.approx(10_000)
    assert summary.tax_value_total == pytest.approx(30_000 * CAPITAL_GAINS_TAX_RATE)
    assert summary.tax_cost_total == pytest.approx(10_000 * CAPITAL_GAINS_TAX_RATE)


def test_nisa_loss_has_zero_tax_value() -> None:
    """NISA 口座の含み損は損益通算・繰越控除の対象外 → 税価値は常に 0。"""
    summary = build_tax_summary(_positions_mixed(), _PRICES_MIXED, synthetic=True)
    nisa_loss = next(v for v in summary.nisa if v.code == "9984")
    assert nisa_loss.is_loss and nisa_loss.is_nisa
    assert nisa_loss.tax_value_if_realized == 0.0
    nisa_gain = next(v for v in summary.nisa if v.code == "4568")
    assert nisa_gain.tax_cost_if_realized == 0.0  # NISA の譲渡益は非課税
    assert summary.nisa_loss_total == pytest.approx(-10_000)
    # NISA の含み損は tax_value_total（課税口座の集計）に混入しない
    assert summary.tax_value_total == pytest.approx(30_000 * CAPITAL_GAINS_TAX_RATE)


def test_build_tax_summary_rejects_empty_positions() -> None:
    with pytest.raises(ValueError):
        build_tax_summary([], {})


# ---------------------------------------------------------------------------
# resolve_last_prices: 合成データ・手入力評価（ネットワーク不要）
# ---------------------------------------------------------------------------


def test_resolve_last_prices_synthetic_and_manual() -> None:
    positions = [
        Position(code="7203", shares=100, avg_cost=2500, acquired_date=dt.date(2024, 1, 10)),
        Position(code="cash", shares=100_000, avg_cost=1, acquired_date=dt.date(2024, 1, 1),
                 manual_price=1.0),
    ]
    last = resolve_last_prices(positions, period="6mo", synthetic=True)
    assert last["7203"] == pytest.approx(_last_close("7203", "6mo"))
    assert last["cash"] == pytest.approx(1.0)


def test_resolve_last_prices_all_manual_needs_no_fetch() -> None:
    """全行が manual_price なら synthetic=False でも価格取得なしで動く。"""
    positions = [
        Position(code="fund-x", shares=1000, avg_cost=2.0, acquired_date=dt.date(2024, 1, 1),
                 manual_price=2.5),
    ]
    last = resolve_last_prices(positions, synthetic=False)
    assert last == {"fund-x": 2.5}


# ---------------------------------------------------------------------------
# to_markdown: 条件付き表現・必須の実務注意・出典参照
# ---------------------------------------------------------------------------


def test_to_markdown_conditional_wording_and_required_notes() -> None:
    summary = build_tax_summary(
        _positions_mixed(), _PRICES_MIXED, period="1y", synthetic=True,
    )
    md = summary.to_markdown()

    # 列名・数値は条件付き表現（「実現した場合の税価値（試算）」）
    assert TAX_VALUE_COLUMN in md
    assert TAX_COST_COLUMN in md
    assert TAX_VALUE_COLUMN == "実現した場合の税価値（試算）"

    # 税価値の手計算値（6,094.5 円 → 桁区切り・0桁表示）と税率・年時点の付記
    assert "6,094" in md
    assert "20.315%" in md
    assert "2025年時点" in md

    # NISA 対比: 対象外の明示・理由・taxation-and-nisa.md の該当節への参照
    assert "対象外（0円）" in md
    assert "損益通算" in md and "繰越控除" in md
    assert "knowledge/regulation-tax/taxation-and-nisa.md" in md
    assert "4.2" in md  # NISAの税制上の性格と注意点の節参照

    # 必須の実務注意（自動挿入）: 同日買い戻しの単価平均化・確定申告・行動バイアス
    assert "総平均法に準ずる方法" in md
    assert "確定申告" in md
    assert "tax tail wagging the dog" in md
    assert "knowledge/strategies/behavioral-finance-japan.md" in md

    # 投資助言化の禁止: 断定・推奨の表現を含まない（本文は「を推奨する」も禁止）
    for phrase in FORBIDDEN_PHRASES_BODY_ONLY:
        assert phrase not in md, f"禁止表現 {phrase!r} がレポート本文に含まれている"


def test_to_markdown_without_nisa_positions() -> None:
    """account 列なし CSV 相当（全 taxable）でも NISA 節は対比の説明を出す。"""
    positions = [
        Position(code="7203", shares=100, avg_cost=2500, acquired_date=dt.date(2024, 1, 10)),
    ]
    summary = build_tax_summary(positions, {"7203": 2000.0}, synthetic=True)
    md = summary.to_markdown()
    assert "NISA口座（nisa_tsumitate / nisa_growth）のポジションはありません" in md
    assert "対象外の理由" in md  # 対比説明は保有が無くても挿入する


def test_to_markdown_without_taxable_positions() -> None:
    positions = [
        Position(code="9984", shares=10, avg_cost=8200, acquired_date=dt.date(2025, 1, 20),
                 account=ACCOUNT_NISA_GROWTH),
    ]
    summary = build_tax_summary(positions, {"9984": 7200.0}, synthetic=True)
    md = summary.to_markdown()
    assert "課税口座のポジションはありません" in md
    assert "対象外（0円）" in md


def test_manual_rows_footnote_and_view() -> None:
    positions = [
        Position(code="7203", shares=100, avg_cost=2500, acquired_date=dt.date(2024, 1, 10)),
        Position(code="fund-x", shares=1000, avg_cost=3.0, acquired_date=dt.date(2024, 1, 1),
                 manual_price=2.5),
    ]
    summary = build_tax_summary(positions, {"7203": 2000.0, "fund-x": 2.5}, synthetic=True)
    fund = next(v for v in summary.taxable if v.code == "fund-x")
    assert fund.manual and fund.name == "fund-x"
    assert fund.pnl == pytest.approx(1000 * (2.5 - 3.0))
    md = summary.to_markdown()
    assert "手入力評価" in md and "fund-x" in md


# ---------------------------------------------------------------------------
# CLI（subprocess + --synthetic、ネットワーク不使用）
# ---------------------------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "analysis/tax_report.py", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _write_csv(path: Path, rows: str) -> Path:
    path.write_text(rows, encoding="utf-8")
    return path


def test_cli_smoke_with_accounts(tmp_path: Path) -> None:
    # 合成データの直近終値から損益の符号を固定する（avg_cost を上下に振る）
    close_7203 = _last_close("7203")
    close_9984 = _last_close("9984")
    csv_path = _write_csv(
        tmp_path / "pf.csv",
        "code,shares,avg_cost,acquired_date,account\n"
        f"7203,100,{close_7203 * 2:.0f},2024-06-14,taxable\n"   # 課税・含み損
        f"9984,10,{close_9984 * 2:.0f},2025-01-20,nisa_growth\n",  # NISA・含み損
    )
    proc = _run("--file", str(csv_path), "--period", "1y", "--synthetic")
    assert proc.returncode == 0, proc.stderr

    report_path = Path(proc.stdout.strip().splitlines()[-1])
    assert report_path.name == f"tax-{TODAY}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")

    assert "課税口座の含み損益 税価値ビュー" in content
    assert TAX_VALUE_COLUMN in content
    assert "## NISA口座の含み損益（損益通算・繰越控除の対象外）" in content
    assert "対象外（0円）" in content
    assert "総平均法に準ずる方法" in content
    assert "確定申告" in content
    assert "tax tail wagging the dog" in content
    assert "免責事項" in content            # save_report が自動付与（hook 準拠）
    assert "合成データ" in content          # --synthetic の明記
    for phrase in FORBIDDEN_PHRASES:
        assert phrase not in content, f"禁止表現 {phrase!r} がレポートに含まれている"


def test_cli_without_account_column(tmp_path: Path) -> None:
    """account 列の無い既存 CSV でも動き、全銘柄が課税口座として扱われる。"""
    csv_path = _write_csv(
        tmp_path / "pf.csv",
        "code,shares,avg_cost,acquired_date\n7203,100,2500,2024-01-10\n",
    )
    proc = _run("--file", str(csv_path), "--period", "6mo", "--synthetic")
    assert proc.returncode == 0, proc.stderr
    content = Path(proc.stdout.strip().splitlines()[-1]).read_text(encoding="utf-8")
    assert "課税口座 1 + NISA 0" in content
    assert "NISA口座（nisa_tsumitate / nisa_growth）のポジションはありません" in content


def test_cli_missing_file_returns_1(tmp_path: Path) -> None:
    proc = _run("--file", str(tmp_path / "missing.csv"), "--synthetic")
    assert proc.returncode == 1
    assert "見つかりません" in proc.stderr
    assert proc.stdout.strip() == ""  # レポートパスは出力しない


def test_cli_invalid_csv_returns_1(tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path / "pf.csv",
        "code,shares,avg_cost,acquired_date\n7203,-100,2500,2024-01-10\n",
    )
    proc = _run("--file", str(csv_path), "--synthetic")
    assert proc.returncode == 1
    assert "バリデーション" in proc.stderr

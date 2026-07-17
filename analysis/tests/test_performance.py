"""stocklib.performance と performance_report.py CLI のテスト（合成データのみ、ネットワーク不使用）。"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import pytest

from stocklib.data import synthetic_prices
from stocklib.performance import (
    Transaction,
    TransactionValidationError,
    evaluate_performance,
    load_transactions,
    replicate_on_benchmark,
    xirr,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_CSV = REPO_ROOT / "analysis" / "templates" / "transactions-example.csv"
TODAY = dt.date.today().isoformat()


def _npv(flows: list[tuple[dt.date, float]], rate: float) -> float:
    d0 = min(d for d, _ in flows)
    return sum(a * (1.0 + rate) ** (-((d - d0).days / 365.25)) for d, a in flows)


# ---------------------------------------------------------------------------
# xirr: 数値解法
# ---------------------------------------------------------------------------


def test_xirr_simple_one_year() -> None:
    flows = [(dt.date(2024, 1, 1), -100.0), (dt.date(2025, 1, 1), 110.0)]
    r = xirr(flows)
    # 366日（うるう年）で +10% → 年率およそ 9.98%
    assert r == pytest.approx(0.0998, abs=1e-3)
    assert _npv(flows, r) == pytest.approx(0.0, abs=1e-6)


def test_xirr_negative_return() -> None:
    flows = [(dt.date(2023, 1, 1), -100.0), (dt.date(2024, 1, 1), 50.0)]
    r = xirr(flows)
    assert r < 0
    assert _npv(flows, r) == pytest.approx(0.0, abs=1e-6)


def test_xirr_multi_flow_npv_zero() -> None:
    flows = [
        (dt.date(2022, 1, 15), -1_000_000.0),
        (dt.date(2022, 9, 1), -500_000.0),
        (dt.date(2023, 6, 10), 200_000.0),
        (dt.date(2024, 3, 31), -300_000.0),
        (dt.date(2025, 1, 15), 1_900_000.0),
    ]
    r = xirr(flows)
    assert _npv(flows, r) == pytest.approx(0.0, abs=1e-4)


def test_xirr_extreme_gain_converges() -> None:
    # ニュートン法が発散しやすい極端なケースでも二分法フォールバックで解ける
    flows = [(dt.date(2024, 1, 1), -1.0), (dt.date(2025, 1, 1), 1000.0)]
    r = xirr(flows)
    assert r > 100.0
    assert _npv(flows, r) == pytest.approx(0.0, abs=1e-6)


def test_xirr_requires_both_signs() -> None:
    with pytest.raises(ValueError, match="正（回収）と負（投下）"):
        xirr([(dt.date(2024, 1, 1), -100.0), (dt.date(2025, 1, 1), -50.0)])


def test_xirr_requires_two_flows() -> None:
    with pytest.raises(ValueError, match="2件以上"):
        xirr([(dt.date(2024, 1, 1), -100.0)])


# ---------------------------------------------------------------------------
# load_transactions: 読み込みとバリデーション
# ---------------------------------------------------------------------------


def test_load_transactions_template() -> None:
    txns = load_transactions(TEMPLATE_CSV)
    assert len(txns) == 9
    # 日付順に整列されている
    assert all(a.date <= b.date for a, b in zip(txns, txns[1:]))
    first = txns[0]
    assert first.side == "deposit"
    assert first.code == ""
    assert first.shares == 1.0  # deposit の shares 省略ではなく明示 1
    assert first.gross_amount == pytest.approx(2_000_000)
    buy = txns[1]
    assert (buy.side, buy.code, buy.shares, buy.price, buy.fee) == ("buy", "7203", 300, 2450, 275)
    assert buy.account == "taxable"
    div = txns[2]
    assert div.side == "dividend"
    assert div.gross_amount == pytest.approx(300 * 25)
    assert div.cash_delta == pytest.approx(300 * 25 - 1523)


def test_load_transactions_shares_optional_for_cash_sides(tmp_path: Path) -> None:
    p = tmp_path / "tx.csv"
    p.write_text(
        "date,code,side,shares,price\n"
        "2024-01-10,,deposit,,500000\n"
        "2024-02-01,7203,dividend,,7500\n",
        encoding="utf-8",
    )
    txns = load_transactions(p)
    assert txns[0].shares == 1.0
    assert txns[0].gross_amount == pytest.approx(500_000)
    assert txns[1].shares == 1.0  # 配当は総額入力（price に総額）も可


def test_load_transactions_aggregates_errors(tmp_path: Path) -> None:
    p = tmp_path / "tx.csv"
    p.write_text(
        "date,code,side,shares,price,fee\n"
        "2024-01-10,7203,hold,100,2500,0\n"      # 2行目: side 不正
        "bad-date,7203,buy,100,2500,0\n"          # 3行目: date 不正
        "2024-01-12,,buy,100,2500,0\n"            # 4行目: buy に code 無し
        "2024-01-13,7203,buy,,2500,0\n"           # 5行目: buy に shares 無し
        "2024-01-14,7203,buy,100,-5,0\n"          # 6行目: price 負
        "2024-01-15,7203,deposit,1,10000,0\n"     # 7行目: deposit に code あり
        "2024-01-16,7203,buy,100,2500,-1\n"       # 8行目: fee 負
        "2099-01-01,7203,buy,100,2500,0\n",       # 9行目: 未来日
        encoding="utf-8",
    )
    with pytest.raises(TransactionValidationError) as ei:
        load_transactions(p)
    msg = str(ei.value)
    for fragment in ("2行目", "3行目", "4行目", "5行目", "6行目", "7行目", "8行目", "9行目"):
        assert fragment in msg


def test_load_transactions_missing_column(tmp_path: Path) -> None:
    p = tmp_path / "tx.csv"
    p.write_text("date,code,side,shares\n2024-01-10,7203,buy,100\n", encoding="utf-8")
    with pytest.raises(TransactionValidationError, match="必須列"):
        load_transactions(p)


def test_load_transactions_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_transactions(tmp_path / "nonexistent.csv")


def test_load_transactions_header_only(tmp_path: Path) -> None:
    p = tmp_path / "tx.csv"
    p.write_text("date,code,side,shares,price\n", encoding="utf-8")
    with pytest.raises(TransactionValidationError, match="データ行がありません"):
        load_transactions(p)


# ---------------------------------------------------------------------------
# evaluate_performance: 台帳・損益・XIRR・ベンチマーク比較（合成データ）
# ---------------------------------------------------------------------------


def test_evaluate_template_account_mode() -> None:
    txns = load_transactions(TEMPLATE_CSV)
    result = evaluate_performance(txns, synthetic=True)
    assert result.mode == "account"
    assert result.synthetic is True
    # テンプレートの手計算値（キャッシュ台帳・平均取得単価法）
    assert result.invested_total == pytest.approx(3_500_000)
    assert result.returned_total == pytest.approx(300_000)
    assert result.dividends_received == pytest.approx((300 * 25 - 1523) + (300 * 30 - 1828))
    assert result.fees_paid == pytest.approx(275 + 1523 + 325 + 275 + 250 + 1828)
    # 9984 の平均取得単価は (100×8200 + 275) / 100 = 8202.75
    assert result.realized_pnl == pytest.approx((9500 - 8202.75) * 50 - 250)
    assert result.cash_balance == pytest.approx(852_024)
    # 保有: 7203×300、6758×100、9984×50
    held = {h.code: h.shares for h in result.holdings}
    assert held == {"7203": 300, "6758": 100, "9984": 50}
    # 終端評価額 = 現金 + 保有時価
    market_value = sum(h.market_value for h in result.holdings)
    assert result.terminal_value == pytest.approx(result.cash_balance + market_value)
    # 恒等式: 総損益 = 実現 + 未実現 + 受取配当
    assert result.total_pnl == pytest.approx(
        result.realized_pnl + result.unrealized_pnl + result.dividends_received
    )
    # XIRR とベンチマーク XIRR が計算できている
    assert result.xirr_value is not None
    assert result.benchmark.xirr_value is not None
    assert result.benchmark.benchmark == "^N225"
    # 現金残高が負になる警告は出ていない
    assert not any("現金残高" in w for w in result.warnings)


def test_evaluate_position_mode(tmp_path: Path) -> None:
    p = tmp_path / "tx.csv"
    p.write_text(
        "date,code,side,shares,price,fee\n"
        "2024-06-03,7203,buy,100,2500,100\n"
        "2025-02-10,7203,sell,40,2800,100\n"
        "2025-03-27,7203,dividend,60,30,0\n",
        encoding="utf-8",
    )
    txns = load_transactions(p)
    result = evaluate_performance(txns, synthetic=True)
    assert result.mode == "position"
    assert result.cash_balance is None
    # 外部フロー: buy（負）・sell（正）・dividend（正）
    assert result.external_flows == [
        (dt.date(2024, 6, 3), -(100 * 2500 + 100)),
        (dt.date(2025, 2, 10), 40 * 2800 - 100),
        (dt.date(2025, 3, 27), 60 * 30.0),
    ]
    # 平均取得単価 = (100×2500 + 100)/100 = 2501円
    assert result.realized_pnl == pytest.approx((2800 - 2501) * 40 - 100)
    held = {h.code: h.shares for h in result.holdings}
    assert held == {"7203": 60}
    assert result.terminal_value == pytest.approx(sum(h.market_value for h in result.holdings))
    assert result.total_pnl == pytest.approx(
        result.realized_pnl + result.unrealized_pnl + result.dividends_received
    )
    assert result.xirr_value is not None


def test_evaluate_rejects_oversell(tmp_path: Path) -> None:
    p = tmp_path / "tx.csv"
    p.write_text(
        "date,code,side,shares,price\n"
        "2024-06-03,7203,buy,100,2500\n"
        "2025-02-10,7203,sell,150,2800\n",
        encoding="utf-8",
    )
    txns = load_transactions(p)
    with pytest.raises(TransactionValidationError, match="保有株数"):
        evaluate_performance(txns, synthetic=True)


def test_evaluate_warns_on_negative_cash(tmp_path: Path) -> None:
    p = tmp_path / "tx.csv"
    p.write_text(
        "date,code,side,shares,price,fee\n"
        "2024-06-03,,deposit,1,100000,0\n"
        "2024-06-04,7203,buy,100,2500,0\n",  # 25万円の買付 > 入金10万円
        encoding="utf-8",
    )
    txns = load_transactions(p)
    result = evaluate_performance(txns, synthetic=True)
    assert any("現金残高" in w and "deposit" in w for w in result.warnings)


def test_evaluate_warns_on_dividend_without_holding(tmp_path: Path) -> None:
    p = tmp_path / "tx.csv"
    p.write_text(
        "date,code,side,shares,price\n"
        "2024-06-03,7203,buy,100,2500\n"
        "2024-07-01,6758,dividend,100,40\n",  # 6758 は未保有
        encoding="utf-8",
    )
    txns = load_transactions(p)
    result = evaluate_performance(txns, synthetic=True)
    assert any("6758" in w and "配当" in w for w in result.warnings)


def test_replicate_on_benchmark_single_flow() -> None:
    close = synthetic_prices("^N225", days=504)["Close"]
    d0 = close.index[10].date()
    flows = [(d0, -1_000_000.0)]
    comp = replicate_on_benchmark(flows, close, benchmark="^N225")
    units = 1_000_000.0 / float(close.iloc[10])
    assert comp.terminal_value == pytest.approx(units * float(close.iloc[-1]))
    # 一括投下1本の XIRR はベンチマーク価格リターンの年率化と一致する
    t_years = (close.index[-1].date() - d0).days / 365.25
    expected = (comp.terminal_value / 1_000_000.0) ** (1.0 / t_years) - 1.0
    assert comp.xirr_value == pytest.approx(expected, rel=1e-6)


def test_replicate_on_benchmark_flow_before_series(tmp_path: Path) -> None:
    close = synthetic_prices("^N225", days=100)["Close"]
    early = close.index[0].date() - dt.timedelta(days=30)
    warnings: list[str] = []
    comp = replicate_on_benchmark([(early, -100.0)], close, "^N225", warnings)
    assert comp.terminal_value > 0
    assert any("系列の開始" in w for w in warnings)


def test_to_markdown_contains_key_sections() -> None:
    txns = load_transactions(TEMPLATE_CSV)
    result = evaluate_performance(txns, synthetic=True)
    md = result.to_markdown()
    for fragment in (
        "## サマリー",
        "損益の内訳",
        "金額加重リターン（MWR = XIRR",
        "ベンチマーク比較",
        "保有明細",
        "外部キャッシュフロー明細",
        "終端評価額",
    ):
        assert fragment in md


# ---------------------------------------------------------------------------
# CLI（subprocess + --synthetic）
# ---------------------------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_performance_report_cli() -> None:
    proc = _run(
        "analysis/performance_report.py",
        "--file", "analysis/templates/transactions-example.csv",
        "--synthetic",
    )
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    assert report_path.name == f"performance-{TODAY}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "免責事項" in content
    assert "金額加重リターン" in content
    assert "XIRR" in content
    assert "ベンチマーク比較" in content
    assert "合成データ" in content
    # knowledge 文書の枠組みと asset_plan への外挿注意が明記されている
    assert "performance-measurement-and-attribution.md" in content
    assert "asset_plan" in content
    assert "--return" in content


def test_performance_report_cli_missing_file() -> None:
    proc = _run("analysis/performance_report.py", "--file", "data/nonexistent-tx.csv", "--synthetic")
    assert proc.returncode == 1
    assert "テンプレート" in proc.stderr


def test_performance_report_cli_default_file_hint() -> None:
    # --file 未指定かつ data/transactions.csv が無い場合はテンプレートへの誘導を出す
    if (REPO_ROOT / "data" / "transactions.csv").exists():
        pytest.skip("data/transactions.csv が存在する環境ではスキップ")
    proc = _run("analysis/performance_report.py", "--synthetic")
    assert proc.returncode == 1
    assert "data/transactions.csv" in proc.stderr


def test_performance_report_cli_invalid_csv(tmp_path: Path) -> None:
    p = tmp_path / "tx.csv"
    p.write_text(
        "date,code,side,shares,price\n2024-01-10,7203,hold,100,2500\n",
        encoding="utf-8",
    )
    proc = _run("analysis/performance_report.py", "--file", str(p), "--synthetic")
    assert proc.returncode == 1
    assert "エラー" in proc.stderr

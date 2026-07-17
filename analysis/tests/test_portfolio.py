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
    DEFAULT_DRIFT_BAND,
    MANUAL_ASSET_SECTOR,
    STRESS_SCENARIOS,
    PortfolioValidationError,
    Position,
    build_drift_summary,
    build_input_warnings,
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
    assert len(positions) == 7
    first = positions[0]
    assert first.code == "7203"
    assert first.shares == 300
    assert first.avg_cost == 2450
    assert first.acquired_date == dt.date(2024, 6, 14)
    assert first.memo == "主力・輸送用機器"
    assert first.cost_value == 300 * 2450
    # memo は省略可（空文字）
    assert positions[1].memo == ""
    # fx_at_cost は任意列: 入力あり → float、空欄 → None（テンプレートは 9984 が空欄）
    assert first.fx_at_cost == pytest.approx(157.50)
    assert positions[2].code == "9984"
    assert positions[2].fx_at_cost is None
    # target_weight は % 入力を割合（0〜1）で保持し、テンプレートは合計 100%
    assert first.target_weight == pytest.approx(0.20)
    assert sum(p.target_weight or 0.0 for p in positions) == pytest.approx(1.0)
    # manual_price 行（投信・現金）は4桁コード以外の識別子を許容する
    fund = positions[5]
    assert fund.code == "emaxis-slim-allcountry"
    assert fund.manual_price == pytest.approx(3.0)
    assert fund.account == "nisa_tsumitate"
    # 投信行には proxy_ticker の記入例（連動対象の上場プロキシ）が入っている
    assert fund.proxy_ticker == "2559.T"
    cash = positions[6]
    assert cash.code == "cash"
    assert cash.manual_price == pytest.approx(1.0)
    assert cash.proxy_ticker is None
    # 上場株の行は manual_price なし（yfinance で評価）
    assert first.manual_price is None
    assert first.proxy_ticker is None


def test_load_portfolio_without_memo_column(tmp_path: Path) -> None:
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date\n7203,100,2500,2024-01-10\n",
        encoding="utf-8",
    )
    positions = load_portfolio(p)
    assert len(positions) == 1
    assert positions[0].memo == ""
    # fx_at_cost 列が無い CSV は後方互換（None のまま）
    assert positions[0].fx_at_cost is None


def test_load_portfolio_fx_at_cost_optional_per_row(tmp_path: Path) -> None:
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date,fx_at_cost\n"
        "7203,100,2500,2024-01-10,150.25\n"
        "6758,50,13000,2024-06-03,\n",  # 空欄 → None
        encoding="utf-8",
    )
    positions = load_portfolio(p)
    assert positions[0].fx_at_cost == pytest.approx(150.25)
    assert positions[1].fx_at_cost is None


def test_load_portfolio_rejects_invalid_fx_at_cost(tmp_path: Path) -> None:
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date,fx_at_cost\n"
        "7203,100,2500,2024-01-10,abc\n"    # 2行目: 非数値
        "6758,50,13000,2024-06-03,-150\n"   # 3行目: 負
        "9984,10,8200,2024-06-03,0\n",      # 4行目: ゼロ
        encoding="utf-8",
    )
    with pytest.raises(PortfolioValidationError) as exc_info:
        load_portfolio(p)
    msg = str(exc_info.value)
    assert "2行目" in msg and "fx_at_cost" in msg
    assert "3行目" in msg and "正の数" in msg
    assert "4行目" in msg


# ---------------------------------------------------------------------------
# load_portfolio: target_weight / manual_price（任意列）
# ---------------------------------------------------------------------------


def test_load_portfolio_target_weight_percent_to_fraction(tmp_path: Path) -> None:
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date,target_weight\n"
        "7203,100,2500,2024-01-10,60\n"
        "6758,50,13000,2024-06-03,40\n",
        encoding="utf-8",
    )
    positions = load_portfolio(p)
    assert positions[0].target_weight == pytest.approx(0.60)
    assert positions[1].target_weight == pytest.approx(0.40)


def test_load_portfolio_without_target_weight_column(tmp_path: Path) -> None:
    """target_weight 列の無い既存 CSV は後方互換（None のまま）。"""
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date\n7203,100,2500,2024-01-10\n",
        encoding="utf-8",
    )
    positions = load_portfolio(p)
    assert positions[0].target_weight is None
    assert positions[0].manual_price is None


def test_load_portfolio_rejects_invalid_target_weight(tmp_path: Path) -> None:
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date,target_weight\n"
        "7203,100,2500,2024-01-10,abc\n"   # 2行目: 非数値
        "6758,50,13000,2024-06-03,-10\n"   # 3行目: 負
        "9984,10,8200,2024-06-03,150\n",   # 4行目: 100 超
        encoding="utf-8",
    )
    with pytest.raises(PortfolioValidationError) as exc_info:
        load_portfolio(p)
    msg = str(exc_info.value)
    assert "2行目" in msg and "target_weight" in msg
    assert "3行目" in msg and "0〜100" in msg
    assert "4行目" in msg


def test_load_portfolio_rejects_partial_target_weight(tmp_path: Path) -> None:
    """target_weight は全行入力 or 全行空欄（一部入力はドリフト試算を誤らせる）。"""
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date,target_weight\n"
        "7203,100,2500,2024-01-10,60\n"
        "6758,50,13000,2024-06-03,\n",  # 空欄
        encoding="utf-8",
    )
    with pytest.raises(PortfolioValidationError, match="全行"):
        load_portfolio(p)


def test_load_portfolio_rejects_target_weight_sum_not_100(tmp_path: Path) -> None:
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date,target_weight\n"
        "7203,100,2500,2024-01-10,60\n"
        "6758,50,13000,2024-06-03,30\n",  # 合計 90%
        encoding="utf-8",
    )
    with pytest.raises(PortfolioValidationError, match="合計"):
        load_portfolio(p)


def test_load_portfolio_manual_price_allows_non_stock_code(tmp_path: Path) -> None:
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date,manual_price\n"
        "7203,100,2500,2024-01-10,\n"                      # 上場株は空欄のまま
        "emaxis-slim-allcountry,400000,2.5,2024-01-15,3.0\n"
        "cash,500000,1,2024-01-01,1\n",
        encoding="utf-8",
    )
    positions = load_portfolio(p)
    assert positions[0].manual_price is None
    assert positions[1].code == "emaxis-slim-allcountry"
    assert positions[1].manual_price == pytest.approx(3.0)
    assert positions[2].manual_price == pytest.approx(1.0)


def test_load_portfolio_non_stock_code_requires_manual_price(tmp_path: Path) -> None:
    """manual_price の無い行は従来どおり4桁コード形式を要求する。"""
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date,manual_price\n"
        "cash,500000,1,2024-01-01,\n",  # manual_price 空欄なのに非4桁コード
        encoding="utf-8",
    )
    with pytest.raises(PortfolioValidationError, match="manual_price"):
        load_portfolio(p)


def test_load_portfolio_rejects_invalid_manual_price(tmp_path: Path) -> None:
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date,manual_price\n"
        "7203,100,2500,2024-01-10,abc\n"   # 2行目: 非数値
        "6758,50,13000,2024-06-03,-3\n"    # 3行目: 負
        "9984,10,8200,2024-06-03,0\n",     # 4行目: ゼロ
        encoding="utf-8",
    )
    with pytest.raises(PortfolioValidationError) as exc_info:
        load_portfolio(p)
    msg = str(exc_info.value)
    assert "2行目" in msg and "manual_price" in msg
    assert "3行目" in msg and "正の数" in msg
    assert "4行目" in msg


def test_load_portfolio_proxy_ticker_optional_per_row(tmp_path: Path) -> None:
    """proxy_ticker は manual_price 行専用の任意列（空欄・列なしは None）。"""
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date,manual_price,proxy_ticker\n"
        "7203,100,2500,2024-01-10,,\n"                          # 上場株: 両方空欄
        "fund-x,400000,2.5,2024-01-15,3.0,2559.T\n"              # 投信 + プロキシ
        "cash,500000,1,2024-01-01,1,\n",                         # 現金: プロキシなし
        encoding="utf-8",
    )
    positions = load_portfolio(p)
    assert positions[0].proxy_ticker is None
    assert positions[1].proxy_ticker == "2559.T"
    assert positions[2].proxy_ticker is None


def test_load_portfolio_without_proxy_ticker_column(tmp_path: Path) -> None:
    """proxy_ticker 列の無い既存 CSV は後方互換（None のまま）。"""
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date\n7203,100,2500,2024-01-10\n",
        encoding="utf-8",
    )
    positions = load_portfolio(p)
    assert positions[0].proxy_ticker is None


def test_load_portfolio_rejects_proxy_ticker_without_manual_price(tmp_path: Path) -> None:
    """proxy_ticker は manual_price（手入力評価）行でのみ指定できる。"""
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date,manual_price,proxy_ticker\n"
        "7203,100,2500,2024-01-10,,1306.T\n",  # 上場株に proxy_ticker → エラー
        encoding="utf-8",
    )
    with pytest.raises(PortfolioValidationError) as exc_info:
        load_portfolio(p)
    msg = str(exc_info.value)
    assert "2行目" in msg and "proxy_ticker" in msg and "manual_price" in msg


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
# evaluate_portfolio: manual_price（手入力評価）行
# ---------------------------------------------------------------------------


def _positions_with_manual() -> list[Position]:
    return [
        Position(code="7203", shares=100, avg_cost=2500, acquired_date=dt.date(2024, 1, 10),
                 target_weight=0.50),
        Position(code="fund-x", shares=200_000, avg_cost=2.5, acquired_date=dt.date(2024, 1, 15),
                 target_weight=0.30, manual_price=3.0),
        Position(code="cash", shares=300_000, avg_cost=1, acquired_date=dt.date(2024, 1, 1),
                 target_weight=0.20, manual_price=1.0),
    ]


def test_evaluate_portfolio_manual_price_rows() -> None:
    review = evaluate_portfolio(_positions_with_manual(), period="1y", synthetic=True)

    mv_stock = 100 * _expected_last_close("7203")
    mv_fund = 200_000 * 3.0
    mv_cash = 300_000 * 1.0
    total_mv = mv_stock + mv_fund + mv_cash

    v_stock, v_fund, v_cash = review.positions
    # 手入力行は manual_price で評価され、β は NaN、セクターは固定ラベル
    assert v_fund.manual and v_cash.manual and not v_stock.manual
    assert v_fund.price == pytest.approx(3.0)
    assert v_fund.market_value == pytest.approx(mv_fund)
    assert v_cash.market_value == pytest.approx(mv_cash)
    assert np.isnan(v_fund.beta) and np.isnan(v_cash.beta)
    assert v_fund.sector == MANUAL_ASSET_SECTOR
    assert v_cash.name == "cash"

    # ウエイト・HHI は手入力行を含む全体ベース
    assert v_stock.weight == pytest.approx(mv_stock / total_mv)
    assert v_fund.weight == pytest.approx(mv_fund / total_mv)
    assert review.total_market_value == pytest.approx(total_mv)
    w = np.array([mv_stock, mv_fund, mv_cash]) / total_mv
    assert review.hhi == pytest.approx(float(np.sum(w**2)))
    assert review.sector_weights[MANUAL_ASSET_SECTOR] == pytest.approx(
        (mv_fund + mv_cash) / total_mv
    )

    # リスク指標は手入力行を除きウエイト再正規化（上場株1銘柄なので単独系列と一致）
    stock_rets = (
        synthetic_prices("7203.T", days=period_to_days("1y"))["Close"].pct_change().dropna()
    )
    assert review.ann_vol == pytest.approx(metrics.ann_vol(stock_rets))
    assert review.var_95 == pytest.approx(metrics.var_historical(stock_rets, 0.95))
    assert review.portfolio_beta == pytest.approx(v_stock.beta)

    md = review.to_markdown()
    assert "手入力" in md
    assert "リスク指標対象外" in md
    assert "※" in md
    assert "対象銘柄（`proxy_ticker` 無しの手入力行を除く）が1銘柄以下のため相関行列は省略" in md


def test_evaluate_portfolio_all_manual_needs_no_network() -> None:
    """全行手入力なら価格取得を一切行わない（synthetic=False でもオフラインで動く）。"""
    positions = [
        Position(code="fund-a", shares=100_000, avg_cost=2.0, acquired_date=dt.date(2024, 1, 1),
                 manual_price=2.4),
        Position(code="cash", shares=400_000, avg_cost=1, acquired_date=dt.date(2024, 1, 1),
                 manual_price=1.0),
    ]
    review = evaluate_portfolio(positions, period="1y", synthetic=False)
    assert review.total_market_value == pytest.approx(100_000 * 2.4 + 400_000 * 1.0)
    assert np.isnan(review.ann_vol)
    assert np.isnan(review.var_95)
    assert np.isnan(review.portfolio_beta)
    assert len(review.correlation) == 0
    md = review.to_markdown()  # NaN は "-" 表示で Markdown 化できる
    assert "手入力" in md


# ---------------------------------------------------------------------------
# 目標配分ドリフト（target_weight）
# ---------------------------------------------------------------------------


def test_evaluate_portfolio_without_target_weight_has_no_drift(tmp_path: Path) -> None:
    """target_weight の無い既存 CSV / Position は後方互換（ドリフト節なし）。"""
    positions = [
        Position(code="7203", shares=100, avg_cost=2500, acquired_date=dt.date(2024, 1, 10)),
    ]
    review = evaluate_portfolio(positions, period="6mo", synthetic=True)
    assert review.drift is None
    assert "目標配分とのドリフト" not in review.to_markdown()
    assert build_drift_summary(positions, review.positions) is None


def test_drift_summary_hand_computed() -> None:
    review = evaluate_portfolio(_positions_with_manual(), period="1y", synthetic=True)
    drift = review.drift
    assert drift is not None
    assert drift.band == pytest.approx(DEFAULT_DRIFT_BAND)

    mv_stock = 100 * _expected_last_close("7203")
    mv_fund = 200_000 * 3.0
    mv_cash = 300_000 * 1.0
    total_mv = mv_stock + mv_fund + mv_cash
    targets = {"7203": 0.50, "fund-x": 0.30, "cash": 0.20}

    assert drift.total_market_value == pytest.approx(total_mv)
    for entry, mv in zip(drift.entries, (mv_stock, mv_fund, mv_cash)):
        current = mv / total_mv
        target = targets[entry.code]
        assert entry.current_weight == pytest.approx(current)
        assert entry.target_weight == pytest.approx(target)
        assert entry.drift == pytest.approx(current - target)
        # 調整額 = (目標 − 現状) × 評価額合計（正=買付相当・負=売却相当の機械的試算）
        assert entry.trade_amount == pytest.approx((target - current) * total_mv)

    # 目標合計 = 100% なら調整額はネットでゼロ（追加資金なしの入れ替え）
    assert sum(e.trade_amount for e in drift.entries) == pytest.approx(0.0, abs=1e-6)

    # バンド超過判定 = |乖離| > band と一致
    expected_out = [e.code for e in drift.entries if abs(e.drift) > drift.band]
    assert [e.code for e in drift.outside_band] == expected_out
    assert drift.max_abs_drift == pytest.approx(max(abs(e.drift) for e in drift.entries))

    # セクター別ドリフト: 手入力2行は同一セクターに合算される
    sector_map = {sec: (cur, tgt, dr) for sec, cur, tgt, dr in drift.sector_drift}
    cur, tgt, dr = sector_map[MANUAL_ASSET_SECTOR]
    assert cur == pytest.approx((mv_fund + mv_cash) / total_mv)
    assert tgt == pytest.approx(0.50)
    assert dr == pytest.approx(cur - tgt)


def test_drift_markdown_measures_without_recommending() -> None:
    review = evaluate_portfolio(_positions_with_manual(), period="1y", synthetic=True)
    md = review.to_markdown()
    assert "## 目標配分とのドリフト" in md
    assert "銘柄別ドリフト" in md
    assert "セクター別ドリフト" in md
    assert "%pt" in md
    assert "±5.0%pt" in md  # 既定バンド
    # 摩擦の注記: 課税口座の売却課税・NISA枠（当年復活しない/生涯枠は翌年復活）・単元株
    assert "20.315%" in md and "2025年時点" in md
    assert "当年中は復活しない" in md
    assert "翌年" in md
    assert "単元株" in md
    # 出典（ナレッジベース）への参照
    assert "portfolio-construction-in-practice.md" in md
    assert "investment-horizons-framework.md" in md
    # 売買推奨はしない（測定と機械的試算のみ）
    assert "売るべき" not in md
    assert "買うべき" not in md


def test_drift_custom_band_changes_judgement() -> None:
    """バンドを広げると「超過」判定が減る（判定は |乖離| > band）。"""
    review_narrow = evaluate_portfolio(
        _positions_with_manual(), period="1y", synthetic=True, drift_band=0.001
    )
    review_wide = evaluate_portfolio(
        _positions_with_manual(), period="1y", synthetic=True, drift_band=0.99
    )
    assert review_narrow.drift is not None and review_wide.drift is not None
    assert len(review_narrow.drift.outside_band) >= len(review_wide.drift.outside_band)
    assert len(review_wide.drift.outside_band) == 0


# ---------------------------------------------------------------------------
# proxy_ticker: 手入力行のプロキシによるリスク指標組み込み
# ---------------------------------------------------------------------------


def _positions_with_proxy() -> list[Position]:
    """上場株 + プロキシ付き投信 + プロキシ無し現金の3行。"""
    return [
        Position(code="7203", shares=100, avg_cost=2500, acquired_date=dt.date(2024, 1, 10)),
        Position(code="fund-x", shares=200_000, avg_cost=2.5,
                 acquired_date=dt.date(2024, 1, 15), manual_price=3.0, proxy_ticker="1306"),
        Position(code="cash", shares=300_000, avg_cost=1,
                 acquired_date=dt.date(2024, 1, 1), manual_price=1.0),
    ]


def test_evaluate_portfolio_proxy_included_in_risk_metrics() -> None:
    review = evaluate_portfolio(_positions_with_proxy(), period="1y", synthetic=True)
    v_stock, v_fund, v_cash = review.positions

    # 評価額は従来どおり manual_price（プロキシの価格では評価しない）
    assert v_fund.market_value == pytest.approx(200_000 * 3.0)
    assert v_fund.price == pytest.approx(3.0)
    assert v_fund.manual and v_fund.proxy_ticker == "1306"
    assert v_fund.sector == MANUAL_ASSET_SECTOR

    # β はプロキシ（1306.T）の価格系列 vs ベンチマークで近似される
    days = period_to_days("1y")
    bench_rets = metrics.daily_returns(synthetic_prices("^N225", days=days)["Close"])
    proxy_rets = metrics.daily_returns(synthetic_prices("1306.T", days=days)["Close"])
    assert np.isfinite(v_fund.beta)
    assert v_fund.beta == pytest.approx(metrics.beta(proxy_rets, bench_rets))
    # プロキシ無しの現金は従来どおり対象外（β NaN）
    assert np.isnan(v_cash.beta)

    # 年率ボラ・VaR: 対象 = 7203 + fund-x（プロキシ系列）、cash を除きウエイト再正規化
    mv_stock, mv_fund = v_stock.market_value, v_fund.market_value
    closes = pd.concat(
        [
            synthetic_prices("7203.T", days=days)["Close"],
            synthetic_prices("1306.T", days=days)["Close"],
        ],
        axis=1,
        keys=["7203", "fund-x"],
    ).dropna()
    rets = closes.pct_change().dropna()
    w = np.array([mv_stock, mv_fund]) / (mv_stock + mv_fund)
    port_rets = pd.Series(rets.to_numpy() @ w, index=rets.index)
    assert review.ann_vol == pytest.approx(metrics.ann_vol(port_rets))
    assert review.var_95 == pytest.approx(metrics.var_historical(port_rets, 0.95))

    # 加重β も cash を除く再正規化ウエイトで計算される
    assert review.portfolio_beta == pytest.approx(
        (v_stock.weight * v_stock.beta + v_fund.weight * v_fund.beta)
        / (v_stock.weight + v_fund.weight)
    )

    # 相関行列にはプロキシ行が保有コード名（fund-x）で入る
    corr = review.correlation
    assert corr.shape == (2, 2)
    assert list(corr.columns) == ["7203", "fund-x"]


def test_proxy_markdown_notes_approximation_limits() -> None:
    review = evaluate_portfolio(_positions_with_proxy(), period="1y", synthetic=True)
    md = review.to_markdown()
    # プロキシによる近似の注記（信託報酬差・為替ヘッジ差・1営業日ズレ）は必ず入る
    assert "プロキシによる近似" in md
    assert "信託報酬差" in md
    assert "為替ヘッジ差" in md
    assert "1営業日ズレ" in md
    assert "fund-x → 1306" in md
    # プロキシ無しの手入力行（cash）は従来どおり対象外の注記を維持
    assert "リスク指標対象外" in md
    assert "`proxy_ticker` 未指定の行（対象: cash）" in md


def test_evaluate_portfolio_without_proxy_unchanged_backward_compat() -> None:
    """proxy_ticker の無い従来 Position では旧来のリスク指標（手入力行除外）と一致する。"""
    review = evaluate_portfolio(_positions_with_manual(), period="1y", synthetic=True)
    v_stock = review.positions[0]
    stock_rets = (
        synthetic_prices("7203.T", days=period_to_days("1y"))["Close"].pct_change().dropna()
    )
    assert review.ann_vol == pytest.approx(metrics.ann_vol(stock_rets))
    assert review.portfolio_beta == pytest.approx(v_stock.beta)
    assert all(v.proxy_ticker is None for v in review.positions)


# ---------------------------------------------------------------------------
# 下落ストレス感応度（β近似）
# ---------------------------------------------------------------------------


def test_stress_summary_hand_computed() -> None:
    review = evaluate_portfolio(_positions_with_proxy(), period="1y", synthetic=True)
    s = review.stress
    assert s is not None
    v_stock, v_fund, v_cash = review.positions

    # 対象 = β が有限な2銘柄（7203 + プロキシ付き fund-x）。cash は対象外
    assert s.n_covered == 2 and s.n_total == 3
    assert s.excluded_codes == ["cash"]
    assert s.proxied == [("fund-x", "1306")]
    assert s.benchmark == "^N225"
    covered_mv = v_stock.market_value + v_fund.market_value
    beta_exposure = (
        v_stock.market_value * v_stock.beta + v_fund.market_value * v_fund.beta
    )
    assert s.covered_market_value == pytest.approx(covered_mv)
    assert s.covered_beta == pytest.approx(beta_exposure / covered_mv)

    # ΔV ≈ Σ MV_i・β_i・Δm（β不明の cash は変動ゼロ扱い）
    assert [r.market_drop for r in s.scenarios] == list(STRESS_SCENARIOS)
    total_mv = review.total_market_value
    for r in s.scenarios:
        assert r.est_pnl == pytest.approx(beta_exposure * r.market_drop)
        assert r.est_value == pytest.approx(total_mv + r.est_pnl)
        assert r.est_pnl_pct == pytest.approx(r.est_pnl / total_mv)


def test_stress_markdown_disclaimers_and_exclusions() -> None:
    review = evaluate_portfolio(_positions_with_proxy(), period="1y", synthetic=True)
    md = review.to_markdown()
    assert "## 下落ストレス感応度（β近似）" in md
    assert "-10%" in md and "-20%" in md and "-30%" in md
    # 予測ではなくβ一定仮定の感応度試算という限界の注記
    assert "予測ではなく" in md and "β一定仮定" in md
    assert "上昇しがち" in md  # ストレス時はβ・相関が上昇しがち
    # β不明の手入力行は対象外（変動ゼロ扱い）と明記
    assert "変動ゼロ" in md and "cash" in md
    # 過去エピソードの参考値は「〜年時点の過去実績」表記
    assert "2008年" in md and "2020年" in md and "過去実績" in md
    # 家計のリスク受容力の読み方への参照
    assert "household-risk-capacity-and-allocation.md" in md
    # 安全宣言・売買推奨は書かない
    assert "耐えられる" not in md
    assert "売るべき" not in md and "買うべき" not in md


def test_stress_all_manual_without_proxy_is_omitted() -> None:
    """全行がプロキシ無し手入力なら、ストレス試算は省略の注記のみ（オフラインで動く）。"""
    positions = [
        Position(code="fund-a", shares=100_000, avg_cost=2.0,
                 acquired_date=dt.date(2024, 1, 1), manual_price=2.4),
        Position(code="cash", shares=400_000, avg_cost=1,
                 acquired_date=dt.date(2024, 1, 1), manual_price=1.0),
    ]
    review = evaluate_portfolio(positions, period="1y", synthetic=False)
    assert review.stress is not None
    assert review.stress.n_covered == 0
    md = review.to_markdown()
    assert "## 下落ストレス感応度（β近似）" in md
    assert "本節の試算は省略" in md


# ---------------------------------------------------------------------------
# 入力チェックの警告（つみたて投資枠に個別株コード）
# ---------------------------------------------------------------------------


def test_input_warning_stock_in_tsumitate() -> None:
    positions = [
        Position(code="7203", shares=100, avg_cost=2500,
                 acquired_date=dt.date(2024, 1, 10), account="nisa_tsumitate"),
    ]
    warnings = build_input_warnings(positions)
    assert len(warnings) == 1
    assert "つみたて投資枠では個別株は購入できない" in warnings[0]
    assert "2024年制度" in warnings[0]
    assert "入力ミス" in warnings[0]
    assert "7203" in warnings[0]

    review = evaluate_portfolio(positions, period="6mo", synthetic=True)
    md = review.to_markdown()
    assert "入力チェックの警告" in md
    assert "つみたて投資枠では個別株は購入できない" in md


def test_no_input_warning_for_fund_in_tsumitate_or_growth_stock() -> None:
    """投信（manual_price 行）のつみたて枠・個別株の成長枠は正当な組み合わせで警告なし。"""
    positions = [
        Position(code="fund-x", shares=100_000, avg_cost=2.0,
                 acquired_date=dt.date(2024, 1, 1), account="nisa_tsumitate",
                 manual_price=2.5),
        Position(code="7203", shares=100, avg_cost=2500,
                 acquired_date=dt.date(2024, 1, 10), account="nisa_growth"),
    ]
    assert build_input_warnings(positions) == []
    review = evaluate_portfolio(positions, period="6mo", synthetic=True)
    assert review.input_warnings == []
    assert "入力チェックの警告" not in review.to_markdown()


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


def test_portfolio_review_cli_drift_and_manual_sections() -> None:
    """テンプレート CSV（target_weight・manual_price 入り）でドリフト節・手入力注記が出る。"""
    proc = _run(
        "analysis/portfolio_review.py",
        "--file", str(TEMPLATE_CSV),
        "--period", "1y",
        "--drift-band", "3.0",
        "--synthetic",
    )
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "## 目標配分とのドリフト" in content
    assert "±3.0%pt" in content  # --drift-band 反映
    assert "manual_price" in content and "手入力" in content
    assert "リスク指標対象外" in content
    assert "emaxis-slim-allcountry" in content and "cash" in content
    assert "免責事項" in content
    # 売買推奨の断定はしない
    assert "売るべき" not in content
    assert "買うべき" not in content


def test_portfolio_review_cli_stress_and_proxy_sections() -> None:
    """テンプレート CSV（proxy_ticker 記入例入り）でストレス節・プロキシ注記が出る。"""
    proc = _run(
        "analysis/portfolio_review.py",
        "--file", str(TEMPLATE_CSV),
        "--period", "1y",
        "--synthetic",
    )
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "## 下落ストレス感応度（β近似）" in content
    assert "プロキシによる近似" in content
    assert "信託報酬差" in content
    assert "emaxis-slim-allcountry → 2559.T" in content
    # プロキシ無しの cash は従来どおり対象外の注記
    assert "リスク指標対象外" in content
    # テンプレートのつみたて枠行は投信（manual_price）なので警告は出ない
    assert "入力チェックの警告" not in content
    # 安全宣言は書かない
    assert "耐えられる" not in content
    assert "免責事項" in content


def test_portfolio_review_cli_tsumitate_stock_warning(tmp_path: Path) -> None:
    """account=nisa_tsumitate の個別株はエラーにせず警告をレポートに出す。"""
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date,account\n"
        "7203,100,2500,2024-01-10,nisa_tsumitate\n",
        encoding="utf-8",
    )
    proc = _run(
        "analysis/portfolio_review.py", "--file", str(p), "--period", "6mo", "--synthetic",
    )
    assert proc.returncode == 0, proc.stderr  # エラーにはしない
    content = Path(proc.stdout.strip().splitlines()[-1]).read_text(encoding="utf-8")
    assert "入力チェックの警告" in content
    assert "つみたて投資枠では個別株は購入できない" in content
    assert "2024年制度" in content


def test_portfolio_review_cli_rejects_nonpositive_drift_band(tmp_path: Path) -> None:
    proc = _run(
        "analysis/portfolio_review.py",
        "--file", str(TEMPLATE_CSV),
        "--drift-band", "0",
        "--synthetic",
    )
    assert proc.returncode == 2  # argparse エラー
    assert "drift-band" in proc.stderr


def test_portfolio_review_cli_no_drift_section_without_target(tmp_path: Path) -> None:
    """target_weight 列の無い既存 CSV は従来どおり（ドリフト節なし）。"""
    p = tmp_path / "pf.csv"
    p.write_text(
        "code,shares,avg_cost,acquired_date\n7203,100,2500,2024-01-10\n",
        encoding="utf-8",
    )
    proc = _run(
        "analysis/portfolio_review.py", "--file", str(p), "--period", "6mo", "--synthetic",
    )
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    content = report_path.read_text(encoding="utf-8")
    assert "目標配分とのドリフト" not in content
    assert "手入力" not in content


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

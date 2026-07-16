"""--in-usd / --in-currency（各 CLI の基準通貨建て評価）のテスト。

合成データ（--synthetic 相当）で円建てと基準通貨建ての統計が恒等式
(1 + r_B) = (1 + r_JPY) / (1 + r_FX) を満たすことと、
CLI ``--in-usd`` / ``--in-currency`` の ``--synthetic`` スモークを検証する。
対象: run_backtest.py / portfolio_review.py / screen.py / daily_brief.py。
ネットワーク不使用。既存 ``test_currency.py`` のスタイルに合わせる。
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import daily_brief
from screen import ScreenCriteria, screen
from stocklib import currency, metrics, report
from stocklib.backtest import ma_cross_signal, run_backtest
from stocklib.data import fetch_prices
from stocklib.portfolio import Position, evaluate_portfolio

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


def _synthetic_close_and_fx(period: str = "1y") -> tuple[pd.Series, pd.DataFrame]:
    """合成データの円建て終値と USDJPY OHLCV を返す（決定論的）。"""
    close = fetch_prices("7203", period=period, synthetic=True)["7203"]["Close"]
    fx_df = fetch_prices(currency.FX_TICKER, period=period, synthetic=True)[currency.FX_TICKER]
    return close, fx_df


# ---------------------------------------------------------------------------
# ライブラリレベル: to_usd_returns の恒等式
# ---------------------------------------------------------------------------


def test_to_usd_returns_matches_equity_conversion() -> None:
    """to_usd_returns の累積積は「円建てエクイティ / 為替（初日基準）」に厳密一致する。"""
    close, fx_df = _synthetic_close_and_fx()
    rets_jpy = metrics.daily_returns(close)
    rets_usd = currency.to_usd_returns(rets_jpy, fx_df["Close"])

    eq_jpy = (1.0 + rets_jpy).cumprod()
    eq_usd = (1.0 + rets_usd).cumprod()
    fx = currency.align_fx(pd.DatetimeIndex(rets_jpy.index), fx_df["Close"])
    expected = eq_jpy * (float(fx.iloc[0]) / fx)
    assert np.allclose(eq_usd.to_numpy(), expected.to_numpy())


def test_to_usd_returns_daily_identity() -> None:
    """各日で (1 + r_USD) = (1 + r_JPY) / (1 + r_FX) が成立する（初日は為替リターン0）。"""
    close, fx_df = _synthetic_close_and_fx()
    rets_jpy = metrics.daily_returns(close)
    rets_usd = currency.to_usd_returns(rets_jpy, fx_df["Close"])
    fx = currency.align_fx(pd.DatetimeIndex(rets_jpy.index), fx_df["Close"])
    r_fx = fx.pct_change().fillna(0.0)
    assert np.allclose(
        (1.0 + rets_usd).to_numpy(),
        ((1.0 + rets_jpy) / (1.0 + r_fx)).to_numpy(),
    )


# ---------------------------------------------------------------------------
# バックテスト: 戦略統計の円建て/ドル建て恒等式
# ---------------------------------------------------------------------------


def test_backtest_usd_total_return_identity() -> None:
    """戦略のトータルリターンが (1+TR_USD) = (1+TR_JPY) / (1+r_FX) を満たす。

    シグナルは円建て価格で計算し、確定した円建て日次リターンのみを換算する
    （run_backtest.py の --in-usd と同じ手順）。
    """
    close, fx_df = _synthetic_close_and_fx(period="2y")
    positions = ma_cross_signal(close, fast=25, slow=75)
    result = run_backtest(close, positions, cost_bps=10.0)

    rets_jpy = result.equity_curve.pct_change()
    rets_jpy.iloc[0] = float(result.equity_curve.iloc[0]) - 1.0
    rets_usd = currency.to_usd_returns(rets_jpy, fx_df["Close"])
    eq_usd = (1.0 + rets_usd).cumprod()

    fx = currency.align_fx(pd.DatetimeIndex(result.equity_curve.index), fx_df["Close"])
    fx_change = float(fx.iloc[-1] / fx.iloc[0] - 1.0)
    total_usd = float(eq_usd.iloc[-1] - 1.0)
    assert (1.0 + total_usd) == pytest.approx((1.0 + result.total_return) / (1.0 + fx_change))
    # 統計が有限値で計算できること
    assert np.isfinite(metrics.ann_return(rets_usd))
    assert np.isfinite(metrics.ann_vol(rets_usd))
    assert metrics.max_drawdown(eq_usd) <= 0.0


def test_run_backtest_cli_in_usd() -> None:
    proc = _run(
        "analysis/run_backtest.py", "--strategy", "ma_cross", "--code", "7203",
        "--period", "1y", "--synthetic", "--in-usd", "--no-charts",
    )
    assert proc.returncode == 0, proc.stderr
    report_path = REPO_ROOT / "reports" / f"backtest-ma_cross-7203-{TODAY}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "ドル建て評価（海外投資家視点）" in content
    # 設計理由（シグナルは円建て価格で計算）と換算の近似がレポートに明記される
    assert "売買シグナルは円建て（現地）価格で計算" in content
    assert "同日終値換算・ヘッジなしの近似" in content
    assert "USDJPY" in content
    assert "| 指標 | 円建て | ドル建て |" in content
    assert "免責" in content


def test_run_backtest_cli_without_in_usd_has_no_usd_section() -> None:
    proc = _run(
        "analysis/run_backtest.py", "--strategy", "ma_cross", "--code", "7203",
        "--period", "1y", "--synthetic", "--no-charts",
    )
    assert proc.returncode == 0, proc.stderr
    assert "ドル建て評価" not in proc.stdout


# ---------------------------------------------------------------------------
# ポートフォリオ: ドル建て評価額・リスク指標
# ---------------------------------------------------------------------------


def _positions() -> list[Position]:
    return [
        Position(code="7203", shares=100, avg_cost=2500.0, acquired_date=dt.date(2024, 1, 15)),
        Position(code="6758", shares=50, avg_cost=13000.0, acquired_date=dt.date(2024, 6, 3)),
    ]


def test_evaluate_portfolio_in_usd_identity() -> None:
    """ドル建て評価額 = 円建て評価額 / 直近 USDJPY で、リスク指標が恒等式換算になる。"""
    review = evaluate_portfolio(_positions(), period="1y", synthetic=True, in_usd=True)
    assert review.usd is not None
    u = review.usd
    assert u.fx_ticker == currency.FX_TICKER
    assert u.fx_rate > 0.0
    assert u.total_market_value == pytest.approx(review.total_market_value / u.fx_rate)
    for v in review.positions:
        assert u.market_values[v.code] == pytest.approx(v.market_value / u.fx_rate)
    assert np.isfinite(u.ann_vol) and u.ann_vol > 0.0
    assert np.isfinite(u.var_95)
    # 為替ボラが加わるためドル建てのリスク指標は円建てと一般に異なる
    assert u.ann_vol != pytest.approx(review.ann_vol, abs=0.0)


def test_evaluate_portfolio_default_has_no_usd() -> None:
    review = evaluate_portfolio(_positions(), period="1y", synthetic=True)
    assert review.usd is None
    assert "ドル建て評価" not in review.to_markdown()


# ---------------------------------------------------------------------------
# ポートフォリオ: fx_at_cost による基準通貨建て損益と株価/為替要因分解
# ---------------------------------------------------------------------------


def _positions_with_fx() -> list[Position]:
    """fx_at_cost あり1銘柄 + なし1銘柄（分解の恒等性と設計思想の両方を検証する）。"""
    return [
        Position(
            code="7203", shares=100, avg_cost=2500.0,
            acquired_date=dt.date(2024, 1, 15), fx_at_cost=150.0,
        ),
        Position(code="6758", shares=50, avg_cost=13000.0, acquired_date=dt.date(2024, 6, 3)),
    ]


def test_fx_at_cost_pnl_decomposition_identity() -> None:
    """損益（基準通貨）= 株価要因 + 為替要因 の恒等性と、各項の定義を検証する。"""
    review = evaluate_portfolio(
        _positions_with_fx(), period="1y", synthetic=True, in_currency="USD"
    )
    assert review.usd is not None
    u = review.usd
    fx0, fx1 = 150.0, u.fx_rate

    assert set(u.pnl_breakdown) == {"7203"}
    assert u.no_fx_at_cost == ["6758"]

    v = next(p for p in review.positions if p.code == "7203")
    b = u.pnl_breakdown["7203"]
    cost_jpy = 100 * 2500.0
    mv_jpy = v.market_value
    pnl_jpy = mv_jpy - cost_jpy

    # 定義: 取得原価 = 円建て取得原価 ÷ fx_at_cost、評価額 = 円建て評価額 ÷ 直近為替
    assert b.fx_at_cost == pytest.approx(fx0)
    assert b.cost_value == pytest.approx(cost_jpy / fx0)
    assert b.market_value == pytest.approx(mv_jpy / fx1)
    assert b.market_value == pytest.approx(u.market_values["7203"])
    assert b.pnl == pytest.approx(b.market_value - b.cost_value)
    assert b.pnl_pct == pytest.approx(b.market_value / b.cost_value - 1.0)

    # 分解: 株価要因 = 円建て損益 ÷ 直近為替、為替要因 = 残差
    assert b.pnl_price == pytest.approx(pnl_jpy / fx1)
    assert b.pnl_fx == pytest.approx(cost_jpy * (fx0 / fx1 - 1.0) / fx0)
    # 恒等性: 合計が損益（基準通貨）に一致
    assert b.pnl_price + b.pnl_fx == pytest.approx(b.pnl)

    # 恒等式 (1 + r_B) = (1 + r_JPY) / (1 + r_FX)（r_FX: 取得時→直近の為替変化率）
    r_jpy = mv_jpy / cost_jpy - 1.0
    r_fx = fx1 / fx0 - 1.0
    assert 1.0 + b.pnl_pct == pytest.approx((1.0 + r_jpy) / (1.0 + r_fx))


def test_fx_at_cost_markdown_breakdown_and_note() -> None:
    """レポートに損益分解の列と「取得時為替未入力のため円建てのみ」の注記が出る。"""
    review = evaluate_portfolio(
        _positions_with_fx(), period="1y", synthetic=True, in_currency="USD"
    )
    md = review.to_markdown()
    assert "損益（USD）" in md
    assert "うち株価要因" in md
    assert "うち為替要因" in md
    assert "取得原価（USD）" in md
    assert "取得時為替未入力のため円建てのみ" in md
    assert "6758" in md
    # fx_at_cost がある場合、旧来の「損益の換算は行わない」宣言は出ない
    assert "損益（P&L）のドル建て換算は行わない" not in md


def test_without_fx_at_cost_keeps_jpy_only_design() -> None:
    """fx_at_cost が全銘柄で無い場合は現行動作（損益は円建てのみ）を維持する。"""
    review = evaluate_portfolio(_positions(), period="1y", synthetic=True, in_currency="USD")
    assert review.usd is not None
    assert review.usd.pnl_breakdown == {}
    assert review.usd.no_fx_at_cost == ["7203", "6758"]
    md = review.to_markdown()
    assert "損益（P&L）のドル建て換算は行わない" in md
    assert "うち株価要因" not in md


def test_portfolio_review_cli_in_currency_with_fx_at_cost(tmp_path: Path) -> None:
    csv_path = tmp_path / "portfolio.csv"
    csv_path.write_text(
        "code,shares,avg_cost,acquired_date,memo,fx_at_cost\n"
        "7203,100,2500,2024-01-15,test,150.0\n"
        "6758,50,13000,2024-06-03,,\n",
        encoding="utf-8",
    )
    proc = _run(
        "analysis/portfolio_review.py", "--file", str(csv_path),
        "--period", "1y", "--synthetic", "--in-currency", "EUR",
    )
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "ユーロ建て評価（海外投資家視点）" in content
    assert "損益（EUR）" in content
    assert "うち株価要因" in content
    assert "うち為替要因" in content
    assert "取得時為替未入力のため円建てのみ" in content
    assert "免責" in content


def test_portfolio_review_cli_in_usd(tmp_path: Path) -> None:
    csv_path = tmp_path / "portfolio.csv"
    csv_path.write_text(
        "code,shares,avg_cost,acquired_date,memo\n"
        "7203,100,2500,2024-01-15,test\n"
        "6758,50,13000,2024-06-03,\n",
        encoding="utf-8",
    )
    proc = _run(
        "analysis/portfolio_review.py", "--file", str(csv_path),
        "--period", "1y", "--synthetic", "--in-usd",
    )
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "ドル建て評価（海外投資家視点）" in content
    # 損益のドル建て換算は行わない旨（購入時為替が無い）の明記
    assert "損益（P&L）のドル建て換算は行わない" in content
    assert "同日終値・ヘッジなしの近似" in content
    assert "評価額合計（USD）" in content
    assert "免責" in content


# ---------------------------------------------------------------------------
# スクリーニング: 基準通貨建てでの条件評価
# ---------------------------------------------------------------------------


def _mini_universe() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"code": "7203", "name": "トヨタ自動車", "sector": "輸送用機器"},
            {"code": "6758", "name": "ソニーグループ", "sector": "電気機器"},
        ]
    )


def test_screen_in_currency_period_return_identity() -> None:
    """screen の基準通貨建て期間リターンが (1+r_B) = (1+r_JPY) / (1+r_FX) を満たす。"""
    universe = _mini_universe()
    jpy, errs_jpy = screen(universe, "1y", ScreenCriteria(), synthetic=True)
    usd, errs_usd = screen(universe, "1y", ScreenCriteria(), synthetic=True, in_currency="USD")
    assert not errs_jpy and not errs_usd
    assert list(jpy["code"]) == list(usd["code"])

    fx_df = currency.fetch_fx("USD", "1y", synthetic=True)
    for code in universe["code"]:
        close = fetch_prices(code, period="1y", synthetic=True)[code]["Close"]
        fx = currency.align_fx(close.index, fx_df["Close"])
        fx_change = float(fx.iloc[-1] / fx.iloc[0] - 1.0)
        r_jpy = float(jpy.loc[jpy["code"] == code, "ret_period"].iloc[0])
        r_usd = float(usd.loc[usd["code"] == code, "ret_period"].iloc[0])
        assert (1.0 + r_usd) == pytest.approx((1.0 + r_jpy) / (1.0 + fx_change))


def test_screen_in_currency_flips_return_condition() -> None:
    """円建てと基準通貨建てで期間リターン条件の合否が変わり得ることを検証する。

    合成為替はボラを持つため円建てとドル建ての期間リターンは一致しない。
    両者の間に閾値を置くと、円建てとドル建てのどちらか一方のみが条件を満たす。
    """
    universe = _mini_universe().iloc[:1]  # 7203 のみ
    jpy, _ = screen(universe, "1y", ScreenCriteria(), synthetic=True)
    usd, _ = screen(universe, "1y", ScreenCriteria(), synthetic=True, in_currency="USD")
    r_jpy = float(jpy["ret_period"].iloc[0]) * 100.0
    r_usd = float(usd["ret_period"].iloc[0]) * 100.0
    assert r_jpy != pytest.approx(r_usd), "合成為替が横ばいで閾値を挟めない（前提の破れ）"
    threshold = (r_jpy + r_usd) / 2.0

    crit = ScreenCriteria(return_above=threshold)
    hit_jpy, _ = screen(universe, "1y", crit, synthetic=True)
    hit_usd, _ = screen(universe, "1y", crit, synthetic=True, in_currency="USD")
    assert len(hit_jpy) + len(hit_usd) == 1  # 閾値の置き方からちょうど一方のみ合致


def test_screen_in_currency_volume_and_valuation_unconverted() -> None:
    """出来高（株数）とバリュエーション比率は基準通貨に依存しない。"""
    universe = _mini_universe()
    crit = ScreenCriteria(per_below=1e9)  # 実質フィルタなしで per/pbr 列を出す
    jpy, _ = screen(universe, "1y", crit, synthetic=True)
    usd, _ = screen(universe, "1y", crit, synthetic=True, in_currency="USD")
    for col in ("vol_surge", "per", "pbr"):
        assert np.allclose(
            jpy[col].to_numpy(dtype=float), usd[col].to_numpy(dtype=float), equal_nan=True
        ), col
    # 価格系列は換算される（終値は円建てと異なる）
    assert not np.allclose(jpy["close"].to_numpy(dtype=float), usd["close"].to_numpy(dtype=float))


def test_screen_cli_in_currency() -> None:
    universe = REPO_ROOT / "analysis" / "universe" / "liquid30.csv"
    proc = _run(
        "analysis/screen.py", "--universe", str(universe),
        "--period", "1y", "--synthetic", "--in-currency", "EUR",
    )
    assert proc.returncode == 0, proc.stderr
    assert "基準通貨: EUR" in proc.stdout
    report_path = REPO_ROOT / "reports" / f"screen-eur-{TODAY}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "スクリーニング結果（ユーロ建て）" in content
    assert "EURJPY=X" in content
    assert "同日終値換算・為替ヘッジなしの近似" in content
    assert "PER/PBR/配当利回りは通貨に依存しない比率のため無変換" in content
    assert "免責" in content


def test_screen_cli_in_usd_alias() -> None:
    universe = REPO_ROOT / "analysis" / "universe" / "liquid30.csv"
    proc = _run(
        "analysis/screen.py", "--universe", str(universe),
        "--period", "1y", "--synthetic", "--in-usd",
    )
    assert proc.returncode == 0, proc.stderr
    content = (REPO_ROOT / "reports" / f"screen-usd-{TODAY}.md").read_text(encoding="utf-8")
    assert "スクリーニング結果（ドル建て）" in content
    assert "USDJPY=X" in content


# ---------------------------------------------------------------------------
# デイリーブリーフ: 市況テーブルの基準通貨建て ^N225 行
# ---------------------------------------------------------------------------


def test_daily_brief_market_section_in_currency_row_identity() -> None:
    """基準通貨建て ^N225 行の値が to_base_series による換算と一致する。"""
    lines, errors, n_market = daily_brief.build_market_section("1y", True, in_currency="USD")
    assert not errors
    text = "\n".join(lines)
    row = next(l for l in text.splitlines() if "日経平均（^N225、ドル建て）" in l)

    n225 = fetch_prices("^N225", period="1y", synthetic=True)["^N225"]["Close"]
    fx_df = currency.fetch_fx("USD", "1y", synthetic=True)
    base = currency.to_base_series(n225, fx_df["Close"])
    # 恒等式: 基準通貨建て前日比 = (1+r_JPY)/(1+r_FX) - 1
    fx = currency.align_fx(n225.index, fx_df["Close"])
    r_expected = (1.0 + float(n225.iloc[-1] / n225.iloc[-2] - 1.0)) / (
        1.0 + float(fx.iloc[-1] / fx.iloc[-2] - 1.0)
    ) - 1.0
    assert float(base.iloc[-1] / base.iloc[-2] - 1.0) == pytest.approx(r_expected)
    for lag in (1, 5, 21):
        assert report.fmt_pct(float(base.iloc[-1] / base.iloc[-1 - lag] - 1.0)) in row
    assert report.fmt_num(float(base.iloc[-1])) in row
    assert "同日終値換算・為替ヘッジなしの近似" in text


def test_daily_brief_market_section_n_market_excludes_fx_row() -> None:
    """取得成功数（data= 判定に使う値）は基準通貨建て行を数えない（契約不変）。"""
    _, _, n_plain = daily_brief.build_market_section("1y", True)
    _, _, n_ccy = daily_brief.build_market_section("1y", True, in_currency="EUR")
    assert n_ccy == n_plain


def test_daily_brief_cli_in_currency_keeps_result_contract(tmp_path: Path) -> None:
    watchlist = tmp_path / "watchlist.csv"
    watchlist.write_text("code,note\n7203,テスト\n", encoding="utf-8")
    proc = _run(
        "analysis/daily_brief.py", "--watchlist", str(watchlist),
        "--period", "1y", "--synthetic", "--in-currency", "USD",
    )
    assert proc.returncode == 0, proc.stderr
    last = proc.stdout.strip().splitlines()[-1]
    assert re.fullmatch(r"RESULT signals=\d+ watch=1/1 data=synthetic", last), last
    assert "日経平均（^N225、ドル建て）" in proc.stdout
    assert "同日終値換算・為替ヘッジなしの近似" in proc.stdout


def test_daily_brief_cli_default_has_no_ccy_row(tmp_path: Path) -> None:
    watchlist = tmp_path / "watchlist.csv"
    watchlist.write_text("code,note\n7203,\n", encoding="utf-8")
    proc = _run(
        "analysis/daily_brief.py", "--watchlist", str(watchlist),
        "--period", "1y", "--synthetic",
    )
    assert proc.returncode == 0, proc.stderr
    assert "ドル建て" not in proc.stdout

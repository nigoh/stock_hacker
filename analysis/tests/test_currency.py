"""stocklib.currency（基準通貨建て換算）のテスト。

人工系列による換算の正しさ（対数リターンの恒等式
log(1+r_B) = log(1+r_JPY) - log(1+r_FX)、基準通貨 B は USD/EUR/GBP で同型）、
基準通貨の一般化（get_fx_ticker / fetch_fx / to_base_*）と旧 USD 固定 API の
後方互換エイリアス、CLI ``--in-currency`` / ``--in-usd`` ``--synthetic`` の
スモークを検証する。ネットワーク不使用。
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stocklib import currency
from stocklib.data import fetch_prices, synthetic_prices

REPO_ROOT = Path(__file__).resolve().parents[2]
TODAY = dt.date.today().isoformat()


def _bdays(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2025-01-06", periods=n, freq="B")


def _artificial_pair(n: int = 200, seed: int = 42) -> tuple[pd.Series, pd.Series]:
    """人工の円建て終値系列と USDJPY 終値系列を返す（同一の営業日インデックス）。"""
    rng = np.random.default_rng(seed)
    idx = _bdays(n)
    jpy = pd.Series(3000.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.02, n))), index=idx)
    fx = pd.Series(150.0 * np.exp(np.cumsum(rng.normal(0.0, 0.006, n))), index=idx)
    return jpy, fx


def test_to_usd_series_is_division_by_fx() -> None:
    jpy, fx = _artificial_pair()
    usd = currency.to_usd_series(jpy, fx)
    assert np.allclose(usd.to_numpy(), (jpy / fx).to_numpy())


def test_log_return_identity() -> None:
    """円建て対数リターン − 為替対数リターン = ドル建て対数リターン（厳密に成立）。"""
    jpy, fx = _artificial_pair()
    usd = currency.to_usd_series(jpy, fx)
    log_r_jpy = np.log(jpy / jpy.shift(1)).dropna()
    log_r_fx = np.log(fx / fx.shift(1)).dropna()
    log_r_usd = np.log(usd / usd.shift(1)).dropna()
    assert np.allclose(log_r_usd.to_numpy(), (log_r_jpy - log_r_fx).to_numpy())


def test_simple_return_identity() -> None:
    """(1 + r_JPY) / (1 + r_FX) = (1 + r_USD) が期間全体でも成立する。"""
    jpy, fx = _artificial_pair()
    usd = currency.to_usd_series(jpy, fx)
    r_jpy = float(jpy.iloc[-1] / jpy.iloc[0] - 1.0)
    r_fx = float(fx.iloc[-1] / fx.iloc[0] - 1.0)
    r_usd = float(usd.iloc[-1] / usd.iloc[0] - 1.0)
    assert (1.0 + r_usd) == pytest.approx((1.0 + r_jpy) / (1.0 + r_fx))


def test_to_usd_ohlcv() -> None:
    """OHLC は為替で除され、Volume は変わらない。"""
    df = synthetic_prices("7203", days=100)
    fx_df = synthetic_prices("USDJPY=X", days=100)
    usd = currency.to_usd(df, fx_df)
    fx_close = fx_df["Close"].reindex(df.index).ffill().bfill()
    for col in ("Open", "High", "Low", "Close"):
        assert np.allclose(usd[col].to_numpy(), (df[col] / fx_close).to_numpy())
    assert (usd["Volume"] == df["Volume"]).all()
    # 元の DataFrame は変更されない
    assert df["Close"].iloc[0] > 100.0


def test_align_fx_forward_fills_missing_days() -> None:
    """為替が欠損する営業日は直前の値で補完される。"""
    idx = _bdays(5)
    fx = pd.Series([150.0, 151.0, 152.0, 153.0, 154.0], index=idx)
    fx_holey = fx.drop(idx[2])  # 3日目が休場
    aligned = currency.align_fx(idx, fx_holey)
    assert float(aligned.iloc[2]) == 151.0  # 直前値
    assert len(aligned) == 5
    assert not aligned.isna().any()


def test_align_fx_no_overlap_raises() -> None:
    idx = _bdays(5)
    fx = pd.Series([150.0, 151.0], index=pd.date_range("2010-01-04", periods=2, freq="B"))
    fx_nan = pd.Series([float("nan")] * 5, index=idx)
    with pytest.raises(ValueError):
        currency.align_fx(idx, fx_nan)
    # 過去の日付しかない場合は ffill で埋まる（例外にならない）ことも確認
    aligned = currency.align_fx(idx, fx)
    assert float(aligned.iloc[0]) == 151.0


def test_synthetic_fx_is_deterministic_and_realistic() -> None:
    """合成 USDJPY は決定論的で、現実的な水準（およそ 50〜400 円/ドル）に収まる。"""
    a = fetch_prices("USDJPY=X", period="1y", synthetic=True)["USDJPY=X"]
    b = fetch_prices("USDJPY=X", period="1y", synthetic=True)["USDJPY=X"]
    pd.testing.assert_frame_equal(a, b)
    assert (a["Close"] > 50.0).all()
    assert (a["Close"] < 400.0).all()


def test_get_fx_ticker_whitelist() -> None:
    """対応通貨はクロス円ティッカーに解決され、大文字小文字は区別しない。"""
    assert currency.get_fx_ticker("USD") == "USDJPY=X"
    assert currency.get_fx_ticker("EUR") == "EURJPY=X"
    assert currency.get_fx_ticker("gbp") == "GBPJPY=X"
    assert currency.get_fx_ticker(" usd ") == "USDJPY=X"


def test_get_fx_ticker_unsupported_raises_with_guidance() -> None:
    """未対応通貨は導入方法（SUPPORTED_CURRENCIES への登録）を示す ValueError。"""
    with pytest.raises(ValueError, match="SUPPORTED_CURRENCIES"):
        currency.get_fx_ticker("CHF")
    with pytest.raises(ValueError):
        currency.currency_label("AUD")


def test_currency_label() -> None:
    assert currency.currency_label("USD") == "ドル"
    assert currency.currency_label("EUR") == "ユーロ"
    assert currency.currency_label("GBP") == "ポンド"


def test_backward_compat_aliases() -> None:
    """旧 USD 固定 API は一般形のエイリアスとして残る（呼び出し側の破壊的変更なし）。"""
    assert currency.to_usd_series is currency.to_base_series
    assert currency.to_usd_returns is currency.to_base_returns
    assert currency.to_usd is currency.to_base_currency
    assert currency.FX_TICKER == "USDJPY=X"


def test_fetch_usdjpy_is_thin_wrapper_of_fetch_fx() -> None:
    a = currency.fetch_usdjpy("6mo", synthetic=True)
    b = currency.fetch_fx("USD", "6mo", synthetic=True)
    pd.testing.assert_frame_equal(a, b)


def test_eur_conversion_log_return_identity() -> None:
    """EURJPY 水準の為替でも log(1+r_EUR) = log(1+r_JPY) - log(1+r_FX) が厳密に成立。"""
    rng = np.random.default_rng(7)
    idx = _bdays(150)
    jpy = pd.Series(3000.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.02, 150))), index=idx)
    fx = pd.Series(165.0 * np.exp(np.cumsum(rng.normal(0.0, 0.005, 150))), index=idx)  # EURJPY 相当
    eur = currency.to_base_series(jpy, fx)
    log_r_jpy = np.log(jpy / jpy.shift(1)).dropna()
    log_r_fx = np.log(fx / fx.shift(1)).dropna()
    log_r_eur = np.log(eur / eur.shift(1)).dropna()
    assert np.allclose(log_r_eur.to_numpy(), (log_r_jpy - log_r_fx).to_numpy())


def test_to_base_returns_matches_series_conversion_gbp() -> None:
    """to_base_returns の累積積は GBPJPY 換算した価格系列の累積リターンと一致する。"""
    rng = np.random.default_rng(11)
    idx = _bdays(120)
    jpy = pd.Series(5000.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.015, 120))), index=idx)
    fx = pd.Series(190.0 * np.exp(np.cumsum(rng.normal(0.0, 0.006, 120))), index=idx)  # GBPJPY 相当
    rets_jpy = jpy.pct_change().fillna(0.0)
    rets_gbp = currency.to_base_returns(rets_jpy, fx)
    eq_gbp = float((1.0 + rets_gbp).prod())
    gbp = currency.to_base_series(jpy, fx)
    assert eq_gbp == pytest.approx(float(gbp.iloc[-1] / gbp.iloc[0]))


def test_synthetic_eur_gbp_fx_deterministic_and_realistic() -> None:
    """合成 EURJPY / GBPJPY は決定論的で、現実的な水準（およそ 50〜400 円）に収まる。"""
    for ccy in ("EUR", "GBP"):
        a = currency.fetch_fx(ccy, "1y", synthetic=True)
        b = currency.fetch_fx(ccy, "1y", synthetic=True)
        pd.testing.assert_frame_equal(a, b)
        assert (a["Close"] > 50.0).all(), ccy
        assert (a["Close"] < 400.0).all(), ccy


def test_evaluate_portfolio_in_currency_eur() -> None:
    """evaluate_portfolio(in_currency="EUR") は EURJPY 換算の評価節を生成する。"""
    from stocklib.portfolio import BaseCurrencyValuation, UsdValuation, load_portfolio, evaluate_portfolio

    assert UsdValuation is BaseCurrencyValuation  # 後方互換エイリアス
    positions = load_portfolio(REPO_ROOT / "analysis" / "templates" / "portfolio-example.csv")
    review = evaluate_portfolio(positions, period="6mo", synthetic=True, in_currency="EUR")
    assert review.usd is not None
    assert review.usd.ccy == "EUR"
    assert review.usd.fx_ticker == "EURJPY=X"
    md = review.to_markdown()
    assert "ユーロ建て評価（海外投資家視点）" in md
    assert "評価額（EUR）" in md


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_analyze_stock_cli_in_usd() -> None:
    proc = _run(
        "analysis/analyze_stock.py", "7203",
        "--period", "1y", "--synthetic", "--in-usd", "--no-charts",
    )
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    assert report_path.name == f"analyze-7203-{TODAY}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "ドル建てパフォーマンス（海外投資家視点）" in content
    assert "為替寄与" in content
    assert "USDJPY" in content
    assert "免責事項" in content


def test_compare_cli_in_usd() -> None:
    proc = _run(
        "analysis/compare.py", "7203", "6758",
        "--period", "1y", "--synthetic", "--in-usd", "--no-charts",
    )
    assert proc.returncode == 0, proc.stderr
    report_path = REPO_ROOT / "reports" / f"compare-7203-6758-usd-{TODAY}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "ドル建て換算（海外投資家視点）" in content
    assert "USDJPY=X" in content
    assert "相関行列" in content


def test_analyze_stock_cli_in_currency_eur() -> None:
    proc = _run(
        "analysis/analyze_stock.py", "7203",
        "--period", "1y", "--synthetic", "--in-currency", "EUR", "--no-charts",
    )
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "ユーロ建てパフォーマンス（海外投資家視点）" in content
    assert "為替寄与" in content
    assert "EURJPY" in content
    assert "免責事項" in content


def test_compare_cli_in_currency_gbp() -> None:
    proc = _run(
        "analysis/compare.py", "7203", "6758",
        "--period", "1y", "--synthetic", "--in-currency", "GBP", "--no-charts",
    )
    assert proc.returncode == 0, proc.stderr
    report_path = REPO_ROOT / "reports" / f"compare-7203-6758-gbp-{TODAY}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "ポンド建て換算（海外投資家視点）" in content
    assert "GBPJPY=X" in content
    assert "相関行列" in content


def test_run_backtest_cli_in_currency_eur() -> None:
    proc = _run(
        "analysis/run_backtest.py", "--strategy", "ma_cross", "--code", "7203",
        "--period", "1y", "--synthetic", "--in-currency", "EUR", "--no-charts",
    )
    assert proc.returncode == 0, proc.stderr
    assert "ユーロ建て評価（海外投資家視点）" in proc.stdout
    assert "EURJPY=X" in proc.stdout

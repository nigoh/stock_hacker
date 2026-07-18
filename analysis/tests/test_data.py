"""data モジュールの検証（合成データ・コード正規化・データソース選択。ネットワーク不使用）。"""

from __future__ import annotations

import argparse
import os

import pandas as pd
import pytest

import stocklib.data as data_mod
import stocklib.jquants as jquants_mod
from stocklib.data import (
    SOURCE_ENV,
    VALID_SOURCES,
    DataFetchError,
    _cache_path,
    add_source_argument,
    fetch_info,
    fetch_prices,
    normalize_code,
    period_to_days,
    resolve_source,
    set_default_source,
    synthetic_prices,
)


@pytest.fixture(autouse=True)
def _isolate_source_env():
    """各テストの前後で ``STOCK_HACKER_SOURCE`` を復元し、環境変数の汚染を防ぐ。"""
    original = os.environ.get(SOURCE_ENV)
    try:
        yield
    finally:
        if original is None:
            os.environ.pop(SOURCE_ENV, None)
        else:
            os.environ[SOURCE_ENV] = original


def test_normalize_code() -> None:
    assert normalize_code("7203") == "7203.T"
    assert normalize_code(" 6758 ") == "6758.T"
    assert normalize_code("^N225") == "^N225"
    assert normalize_code("7203.T") == "7203.T"
    assert normalize_code("AAPL") == "AAPL"


def test_normalize_code_alpha_2024() -> None:
    # 2024年以降の英字入りコード（例: 130A ロゴスHD、135A ベルシステム24 等の形式）
    assert normalize_code("130A") == "130A.T"
    assert normalize_code("135A") == "135A.T"
    # 小文字入力は大文字化して正規化
    assert normalize_code("130a") == "130A.T"
    assert normalize_code(" 135a ") == "135A.T"


def test_normalize_code_passthrough() -> None:
    # 指数・通貨ペア・接尾辞付きは素通し
    assert normalize_code("^N225") == "^N225"
    assert normalize_code("USDJPY=X") == "USDJPY=X"
    assert normalize_code("130A.T") == "130A.T"


def test_normalize_code_invalid_passthrough() -> None:
    # パターン外（先頭・3文字目が数字でない、桁数不一致など）は変換しない
    assert normalize_code("AAPL") == "AAPL"
    assert normalize_code("13A0") == "13A0"  # 3文字目が英字
    assert normalize_code("A123") == "A123"  # 先頭が英字
    assert normalize_code("12345") == "12345"  # 5桁
    assert normalize_code("720") == "720"  # 3桁
    assert normalize_code("") == ""


def test_period_to_days() -> None:
    assert period_to_days("1y") == 252
    assert period_to_days("2y") == 504
    assert period_to_days("6mo") == 126
    assert period_to_days("30d") == 30
    assert period_to_days("max") == 2520
    with pytest.raises(ValueError):
        period_to_days("abc")


def test_synthetic_prices_deterministic() -> None:
    a = synthetic_prices("7203", days=100)
    b = synthetic_prices("7203", days=100)
    pd.testing.assert_frame_equal(a, b)
    c = synthetic_prices("6758", days=100)
    assert not a["Close"].equals(c["Close"])  # 銘柄ごとに異なる系列


def test_synthetic_prices_structure() -> None:
    df = synthetic_prices("9984", days=250)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert len(df) == 250
    assert isinstance(df.index, pd.DatetimeIndex)
    assert (df["High"] >= df[["Open", "Close"]].max(axis=1) - 1e-9).all()
    assert (df["Low"] <= df[["Open", "Close"]].min(axis=1) + 1e-9).all()
    assert (df["Close"] > 0).all()
    assert (df["Volume"] > 0).all()


def test_synthetic_prices_alpha_code_case_insensitive() -> None:
    # 英字入りコードは大文字化してから正規化されるため、大小どちらの入力でも同一系列
    a = synthetic_prices("130A", days=50)
    b = synthetic_prices("130a", days=50)
    pd.testing.assert_frame_equal(a, b)


def test_fetch_prices_synthetic_multi() -> None:
    out = fetch_prices(["7203", "^N225"], period="1y", synthetic=True)
    assert set(out.keys()) == {"7203", "^N225"}
    for df in out.values():
        assert len(df) == 252
        assert "Close" in df.columns


def test_fetch_info_synthetic() -> None:
    info = fetch_info("7203", synthetic=True)
    assert "PER（実績）" in info
    assert "時価総額" in info
    assert info == fetch_info("7203", synthetic=True)  # 決定論的


# ---------------------------------------------------------------------------
# データソース選択（yfinance / jquants）
# ---------------------------------------------------------------------------


def test_resolve_source_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SOURCE_ENV, raising=False)
    assert resolve_source() == "yfinance"  # 既定
    assert resolve_source("jquants") == "jquants"
    assert resolve_source("JQuants") == "jquants"  # 大文字小文字を吸収
    monkeypatch.setenv(SOURCE_ENV, "jquants")
    assert resolve_source() == "jquants"  # 環境変数を参照
    assert resolve_source("yfinance") == "yfinance"  # 引数が環境変数に優先
    for bad in ("bloomberg", "quandl"):
        with pytest.raises(ValueError):
            resolve_source(bad)


def test_set_default_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SOURCE_ENV, raising=False)
    set_default_source(None)
    assert SOURCE_ENV not in os.environ  # None は no-op
    set_default_source("")
    assert SOURCE_ENV not in os.environ  # 空文字も no-op
    set_default_source("jquants")
    assert os.environ[SOURCE_ENV] == "jquants"
    with pytest.raises(ValueError):
        set_default_source("nope")


def test_cache_path_includes_source() -> None:
    p_yf = _cache_path("7203.T", "1y", "1d", "yfinance")
    p_jq = _cache_path("7203.T", "1y", "1d", "jquants")
    assert p_yf != p_jq  # ソースごとにキャッシュを分離
    assert p_yf.name.endswith("-yfinance.csv")
    assert p_jq.name.endswith("-jquants.csv")


def test_add_source_argument() -> None:
    parser = argparse.ArgumentParser()
    add_source_argument(parser)
    assert parser.parse_args([]).source is None  # 既定は None（=環境変数/既定に委ねる）
    assert parser.parse_args(["--source", "jquants"]).source == "jquants"
    with pytest.raises(SystemExit):  # choices 外は argparse が弾く
        parser.parse_args(["--source", "bogus"])


def test_fetch_prices_source_jquants_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def fake_fetch_daily_quotes(codes, period="1y", **_kw):  # type: ignore[no-untyped-def]
        calls["codes"], calls["period"] = codes, period
        return {codes: synthetic_prices(str(codes), days=30)}

    monkeypatch.setattr(jquants_mod, "fetch_daily_quotes", fake_fetch_daily_quotes)
    # yfinance には出ないことを保証
    monkeypatch.setattr(
        data_mod, "_fetch_one_yfinance",
        lambda *a, **k: pytest.fail("jquants ソースで yfinance が呼ばれてはいけない"),
    )
    out = fetch_prices("7203", period="6mo", source="jquants", use_cache=False)
    assert set(out.keys()) == {"7203"}
    assert calls["codes"] == "7203" and calls["period"] == "6mo"


def test_fetch_prices_source_jquants_index_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # 指数・為替は J-Quants 非対応 → yfinance にフォールバックする
    seen: dict[str, str] = {}

    def fake_yf(ticker, period, interval):  # type: ignore[no-untyped-def]
        seen["ticker"] = ticker
        return synthetic_prices(ticker, days=20)

    monkeypatch.setattr(data_mod, "_fetch_one_yfinance", fake_yf)
    monkeypatch.setattr(
        jquants_mod, "fetch_daily_quotes",
        lambda *a, **k: pytest.fail("指数は jquants を呼ばずフォールバックすべき"),
    )
    out = fetch_prices("^N225", period="1mo", source="jquants", use_cache=False)
    assert "^N225" in out
    assert seen["ticker"] == "^N225"


def test_fetch_prices_jquants_interval_guard() -> None:
    # 日足以外 + jquants はネットワークに出る前にエラー
    with pytest.raises(DataFetchError):
        fetch_prices("7203", period="1mo", interval="1wk", source="jquants", use_cache=False)


def test_fetch_prices_synthetic_ignores_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        jquants_mod, "fetch_daily_quotes",
        lambda *a, **k: pytest.fail("synthetic は価格ソースを呼んではいけない"),
    )
    out = fetch_prices("7203", period="1mo", synthetic=True, source="jquants")
    assert "7203" in out


def test_valid_sources_constant() -> None:
    assert VALID_SOURCES == ("yfinance", "jquants")

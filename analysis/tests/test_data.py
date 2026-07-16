"""data モジュールの検証（合成データ・コード正規化。ネットワーク不使用）。"""

from __future__ import annotations

import pandas as pd
import pytest

from stocklib.data import (
    fetch_info,
    fetch_prices,
    normalize_code,
    period_to_days,
    synthetic_prices,
)


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

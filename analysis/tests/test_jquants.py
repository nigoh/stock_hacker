"""jquants モジュール（V2・APIキー方式）の検証。

urllib.request.urlopen をモックしネットワーク不使用。V2 の仕様（``x-api-key`` 認証、
``/equities/master`` / ``/equities/bars/daily`` エンドポイント、``data`` 配列 +
``pagination_key``、短縮カラム名 O/H/L/C/Vo・AdjO 等）を検証する。
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from typing import Any, Callable

import pytest

from stocklib import jquants
from stocklib.jquants import (
    API_BASE,
    API_KEY_ENV,
    SIGNUP_URL,
    JQuantsAuthError,
    JQuantsError,
    _quotes_to_ohlcv,
    fetch_daily_quotes,
    fetch_listed_info,
    get_api_key,
    normalize_jquants_code,
)


# ---------------------------------------------------------------------------
# モックの部品
# ---------------------------------------------------------------------------


class _FakeResponse:
    """urllib.request.urlopen の戻り値（コンテキストマネージャ）を模す。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _install_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[str], dict[str, Any]],
    *,
    headers_sink: list[dict[str, str]] | None = None,
) -> list[str]:
    """urlopen をモックし、呼び出された URL の履歴リストを返す。

    handler(url) が辞書を返せばそれを JSON 応答とし、
    urllib.error.HTTPError 等を raise すればそのまま伝播させる。
    ``headers_sink`` を渡すと各リクエストのヘッダ辞書を追記する。
    """
    calls: list[str] = []

    def fake_urlopen(req: Any, timeout: float | None = None) -> _FakeResponse:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append(url)
        if headers_sink is not None:
            headers_sink.append(dict(req.header_items()))
        return _FakeResponse(handler(url))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


def _http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.jquants.com/v2/dummy",
        code=code,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body.encode("utf-8")),
    )


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """各テストで API キーの環境変数をリセットする。"""
    monkeypatch.delenv(API_KEY_ENV, raising=False)


# ---------------------------------------------------------------------------
# normalize_jquants_code
# ---------------------------------------------------------------------------


def test_normalize_4digit_to_5digit() -> None:
    assert normalize_jquants_code("7203") == "72030"
    assert normalize_jquants_code(" 6758 ") == "67580"


def test_normalize_alpha_codes() -> None:
    # 2024年以降の英字入りコード（例: 130A）も5桁化される
    assert normalize_jquants_code("130A") == "130A0"
    assert normalize_jquants_code("130a") == "130A0"  # 小文字は大文字化
    assert normalize_jquants_code("130A0") == "130A0"  # 既に5桁はそのまま


def test_normalize_strips_yfinance_suffix() -> None:
    assert normalize_jquants_code("7203.T") == "72030"
    assert normalize_jquants_code("72030") == "72030"


def test_normalize_invalid_raises_value_error() -> None:
    for bad in ["", "72", "720", "7203.US", "^N225", "A203", "720300"]:
        with pytest.raises(ValueError):
            normalize_jquants_code(bad)


# ---------------------------------------------------------------------------
# get_api_key（V2・APIキー方式）
# ---------------------------------------------------------------------------


def test_get_api_key_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, "key-abc")
    assert get_api_key() == "key-abc"


def test_get_api_key_arg_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, "key-env")
    assert get_api_key("key-explicit") == "key-explicit"


def test_get_api_key_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(JQuantsAuthError) as exc_info:
        get_api_key()
    assert API_KEY_ENV in str(exc_info.value)
    assert SIGNUP_URL in str(exc_info.value)


def test_requests_send_api_key_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, "key-xyz")
    headers: list[dict[str, str]] = []
    _install_urlopen(
        monkeypatch,
        lambda url: {"data": [{"Code": "72030", "CoName": "トヨタ自動車"}]},
        headers_sink=headers,
    )
    fetch_listed_info()
    # urllib はヘッダ名を capitalize するため "X-api-key" として送られる
    assert any(v == "key-xyz" for h in headers for v in h.values())


# ---------------------------------------------------------------------------
# 認証エラー（HTTP 401/403）と非認証エラー（500）
# ---------------------------------------------------------------------------


def test_auth_http_error_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, "bad-key")

    def handler(url: str) -> dict[str, Any]:
        raise _http_error(401, '{"message": "The incoming token is invalid"}')

    _install_urlopen(monkeypatch, handler)
    with pytest.raises(JQuantsAuthError) as exc_info:
        fetch_listed_info()
    msg = str(exc_info.value)
    assert "401" in msg
    assert API_KEY_ENV in msg  # 再発行手順への導線
    assert "invalid" in msg  # API 応答の詳細を含む


def test_non_auth_http_error_is_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, "key")

    def handler(url: str) -> dict[str, Any]:
        raise _http_error(500, "server error")

    _install_urlopen(monkeypatch, handler)
    with pytest.raises(JQuantsError) as exc_info:
        fetch_listed_info()
    assert not isinstance(exc_info.value, JQuantsAuthError)
    assert "500" in str(exc_info.value)


def test_missing_key_does_not_hit_network(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_urlopen(monkeypatch, lambda url: pytest.fail("ネットワークに出てはいけない"))
    with pytest.raises(JQuantsAuthError):
        fetch_listed_info()  # API キー未設定なら通信前に失敗する


# ---------------------------------------------------------------------------
# _quotes_to_ohlcv — V2 の調整済み列（AdjO 等）優先と欠損列エラー
# ---------------------------------------------------------------------------


def _quote_row(date: str, base: float, *, adjusted: bool = True) -> dict[str, Any]:
    row: dict[str, Any] = {
        "Date": date,
        "O": base,
        "H": base + 10,
        "L": base - 10,
        "C": base + 5,
        "Vo": 1000,
    }
    if adjusted:
        # 調整済み系列は生値の半分（分割調整を模す）
        row |= {
            "AdjO": base / 2,
            "AdjH": (base + 10) / 2,
            "AdjL": (base - 10) / 2,
            "AdjC": (base + 5) / 2,
            "AdjVo": 2000,
        }
    return row


def test_quotes_to_ohlcv_prefers_adjusted_columns() -> None:
    rows = [_quote_row("2025-01-06", 3000.0), _quote_row("2025-01-07", 3100.0)]
    df = _quotes_to_ohlcv(rows, "7203")
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df["Close"].iloc[0] == pytest.approx(3005.0 / 2)  # AdjC を採用
    assert df["Volume"].iloc[0] == 2000
    assert df.index.is_monotonic_increasing


def test_quotes_to_ohlcv_falls_back_to_raw_columns() -> None:
    rows = [_quote_row("2025-01-06", 3000.0, adjusted=False)]
    df = _quotes_to_ohlcv(rows, "7203")
    assert df["Close"].iloc[0] == pytest.approx(3005.0)  # 生の C を採用


def test_quotes_to_ohlcv_sorts_by_date() -> None:
    rows = [_quote_row("2025-01-08", 3200.0), _quote_row("2025-01-06", 3000.0)]
    df = _quotes_to_ohlcv(rows, "7203")
    assert df.index[0] < df.index[1]


def test_quotes_to_ohlcv_empty_raises() -> None:
    with pytest.raises(JQuantsError) as exc_info:
        _quotes_to_ohlcv([], "7203")
    assert "7203" in str(exc_info.value)


def test_quotes_to_ohlcv_missing_columns_raises() -> None:
    rows = [{"Date": "2025-01-06", "C": 3000.0}]  # O/H/L/Vo が欠損
    with pytest.raises(JQuantsError) as exc_info:
        _quotes_to_ohlcv(rows, "7203")
    assert "欠損列" in str(exc_info.value)
    assert "O" in str(exc_info.value)


# ---------------------------------------------------------------------------
# fetch_listed_info / fetch_daily_quotes — V2 エンドポイント・pagination・正規化
# ---------------------------------------------------------------------------


def test_fetch_listed_info_follows_pagination_and_renames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(url: str) -> dict[str, Any]:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if query.get("pagination_key") == "KEY1":
            return {"data": [{"Code": "67580", "CoName": "ソニーグループ", "S33Nm": "電気機器"}]}
        return {
            "data": [{"Code": "72030", "CoName": "トヨタ自動車", "S33Nm": "輸送用機器"}],
            "pagination_key": "KEY1",
        }

    calls = _install_urlopen(monkeypatch, handler)
    monkeypatch.setenv(API_KEY_ENV, "key-abc")
    df = fetch_listed_info()
    assert list(df["Code"]) == ["72030", "67580"]  # 2ページ分が結合される
    # V2 の短縮カラム名が V1 相当の安定名へ正規化される
    assert "CompanyName" in df.columns
    assert "Sector33CodeName" in df.columns
    assert list(df["CompanyName"]) == ["トヨタ自動車", "ソニーグループ"]
    master_calls = [c for c in calls if "/equities/master" in c]
    assert len(master_calls) == 2
    assert "pagination_key=KEY1" in master_calls[1]
    assert "pagination_key" not in master_calls[0]


def test_fetch_daily_quotes_pagination_and_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(url: str) -> dict[str, Any]:
        assert "/equities/bars/daily" in url  # V2 エンドポイント
        assert "code=72030" in url  # 4桁コードが5桁に正規化されて送られる
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if query.get("pagination_key") == "P2":
            return {"data": [_quote_row("2025-01-07", 3100.0)]}
        return {
            "data": [_quote_row("2025-01-06", 3000.0)],
            "pagination_key": "P2",
        }

    _install_urlopen(monkeypatch, handler)
    result = fetch_daily_quotes("7203", period="1y", api_key="key-abc")
    assert set(result.keys()) == {"7203"}  # キーは入力コードのまま
    df = result["7203"]
    assert len(df) == 2  # ページを跨いだ行が結合される
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_fetch_listed_info_empty_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(url: str) -> dict[str, Any]:
        return {"data": []}

    _install_urlopen(monkeypatch, handler)
    monkeypatch.setenv(API_KEY_ENV, "key-abc")
    with pytest.raises(JQuantsError):
        fetch_listed_info()


def test_endpoints_use_v2_base(monkeypatch: pytest.MonkeyPatch) -> None:
    assert API_BASE == "https://api.jquants.com/v2"
    calls = _install_urlopen(
        monkeypatch, lambda url: {"data": [{"Code": "72030", "CoName": "トヨタ自動車"}]}
    )
    monkeypatch.setenv(API_KEY_ENV, "key-abc")
    fetch_listed_info()
    assert calls[0].startswith("https://api.jquants.com/v2/equities/master")

"""jquants モジュールの検証（urllib.request.urlopen をモックし、ネットワーク不使用）。"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from typing import Any, Callable

import pytest

from stocklib import jquants
from stocklib.jquants import (
    JQuantsAuthError,
    JQuantsError,
    _quotes_to_ohlcv,
    fetch_daily_quotes,
    fetch_listed_info,
    get_id_token,
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
) -> list[str]:
    """urlopen をモックし、呼び出された URL の履歴リストを返す。

    handler(url) が辞書を返せばそれを JSON 応答とし、
    urllib.error.HTTPError 等を raise すればそのまま伝播させる。
    """
    calls: list[str] = []

    def fake_urlopen(req: Any, timeout: float | None = None) -> _FakeResponse:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append(url)
        return _FakeResponse(handler(url))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


def _http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.jquants.com/v1/dummy",
        code=code,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body.encode("utf-8")),
    )


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """各テストで idToken キャッシュと環境変数をリセットする。"""
    monkeypatch.setattr(jquants, "_id_token_cache", None)
    monkeypatch.delenv(jquants.REFRESH_TOKEN_ENV, raising=False)


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
# get_id_token — キャッシュと force_refresh
# ---------------------------------------------------------------------------


def test_get_id_token_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_urlopen(monkeypatch, lambda url: {"idToken": "tok-1"})
    assert get_id_token("refresh-abc") == "tok-1"
    assert get_id_token("refresh-abc") == "tok-1"  # 2回目はキャッシュ
    assert len(calls) == 1
    assert "auth_refresh" in calls[0]
    assert "refresh-abc" in urllib.parse.unquote(calls[0])


def test_get_id_token_force_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    tokens = iter(["tok-1", "tok-2"])
    calls = _install_urlopen(monkeypatch, lambda url: {"idToken": next(tokens)})
    assert get_id_token("refresh-abc") == "tok-1"
    assert get_id_token("refresh-abc", force_refresh=True) == "tok-2"
    assert len(calls) == 2


def test_get_id_token_expired_cache_refetches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_urlopen(monkeypatch, lambda url: {"idToken": "tok-new"})
    # TTL（23時間）を超えた古いキャッシュは無視される
    monkeypatch.setattr(jquants, "_id_token_cache", ("tok-old", 0.0))
    assert get_id_token("refresh-abc") == "tok-new"
    assert len(calls) == 1


def test_get_id_token_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(jquants.REFRESH_TOKEN_ENV, "env-token")
    calls = _install_urlopen(monkeypatch, lambda url: {"idToken": "tok-env"})
    assert get_id_token() == "tok-env"
    assert "env-token" in urllib.parse.unquote(calls[0])


def test_get_id_token_missing_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_urlopen(monkeypatch, lambda url: pytest.fail("ネットワークに出てはいけない"))
    with pytest.raises(JQuantsAuthError) as exc_info:
        get_id_token()
    assert jquants.REFRESH_TOKEN_ENV in str(exc_info.value)
    assert jquants.SIGNUP_URL in str(exc_info.value)


def test_get_id_token_missing_token_in_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_urlopen(monkeypatch, lambda url: {"message": "no token"})
    with pytest.raises(JQuantsAuthError):
        get_id_token("refresh-abc")


# ---------------------------------------------------------------------------
# 認証エラー（HTTP 401 等）
# ---------------------------------------------------------------------------


def test_auth_http_error_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(url: str) -> dict[str, Any]:
        raise _http_error(401, '{"message": "The incoming token is invalid"}')

    _install_urlopen(monkeypatch, handler)
    with pytest.raises(JQuantsAuthError) as exc_info:
        get_id_token("expired-token")
    msg = str(exc_info.value)
    assert "401" in msg
    assert jquants.REFRESH_TOKEN_ENV in msg  # 再発行手順への導線
    assert "invalid" in msg  # API 応答の詳細を含む


def test_non_auth_http_error_is_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(url: str) -> dict[str, Any]:
        raise _http_error(500, "server error")

    _install_urlopen(monkeypatch, handler)
    with pytest.raises(JQuantsError) as exc_info:
        get_id_token("refresh-abc")
    assert not isinstance(exc_info.value, JQuantsAuthError)
    assert "500" in str(exc_info.value)


# ---------------------------------------------------------------------------
# _quotes_to_ohlcv — 調整済み列の優先と欠損列エラー
# ---------------------------------------------------------------------------


def _quote_row(date: str, base: float, *, adjusted: bool = True) -> dict[str, Any]:
    row: dict[str, Any] = {
        "Date": date,
        "Open": base,
        "High": base + 10,
        "Low": base - 10,
        "Close": base + 5,
        "Volume": 1000,
    }
    if adjusted:
        # 調整済み系列は生値の半分（分割調整を模す）
        row |= {
            "AdjustmentOpen": base / 2,
            "AdjustmentHigh": (base + 10) / 2,
            "AdjustmentLow": (base - 10) / 2,
            "AdjustmentClose": (base + 5) / 2,
            "AdjustmentVolume": 2000,
        }
    return row


def test_quotes_to_ohlcv_prefers_adjusted_columns() -> None:
    rows = [_quote_row("2025-01-06", 3000.0), _quote_row("2025-01-07", 3100.0)]
    df = _quotes_to_ohlcv(rows, "7203")
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df["Close"].iloc[0] == pytest.approx(3005.0 / 2)  # AdjustmentClose を採用
    assert df["Volume"].iloc[0] == 2000
    assert df.index.is_monotonic_increasing


def test_quotes_to_ohlcv_falls_back_to_raw_columns() -> None:
    rows = [_quote_row("2025-01-06", 3000.0, adjusted=False)]
    df = _quotes_to_ohlcv(rows, "7203")
    assert df["Close"].iloc[0] == pytest.approx(3005.0)  # 生の Close を採用


def test_quotes_to_ohlcv_sorts_by_date() -> None:
    rows = [_quote_row("2025-01-08", 3200.0), _quote_row("2025-01-06", 3000.0)]
    df = _quotes_to_ohlcv(rows, "7203")
    assert df.index[0] < df.index[1]


def test_quotes_to_ohlcv_empty_raises() -> None:
    with pytest.raises(JQuantsError) as exc_info:
        _quotes_to_ohlcv([], "7203")
    assert "7203" in str(exc_info.value)


def test_quotes_to_ohlcv_missing_columns_raises() -> None:
    rows = [{"Date": "2025-01-06", "Close": 3000.0}]  # Open/High/Low/Volume が欠損
    with pytest.raises(JQuantsError) as exc_info:
        _quotes_to_ohlcv(rows, "7203")
    assert "欠損列" in str(exc_info.value)
    assert "Open" in str(exc_info.value)


# ---------------------------------------------------------------------------
# pagination_key の追跡
# ---------------------------------------------------------------------------


def test_fetch_listed_info_follows_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(url: str) -> dict[str, Any]:
        if "auth_refresh" in url:
            return {"idToken": "tok-1"}
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if query.get("pagination_key") == "KEY1":
            return {"info": [{"Code": "67580", "CompanyName": "ソニーグループ"}]}
        return {
            "info": [{"Code": "72030", "CompanyName": "トヨタ自動車"}],
            "pagination_key": "KEY1",
        }

    calls = _install_urlopen(monkeypatch, handler)
    monkeypatch.setenv(jquants.REFRESH_TOKEN_ENV, "refresh-abc")
    df = fetch_listed_info()
    assert list(df["Code"]) == ["72030", "67580"]  # 2ページ分が結合される
    listed_calls = [c for c in calls if "/listed/info" in c]
    assert len(listed_calls) == 2
    assert "pagination_key=KEY1" in listed_calls[1]
    assert "pagination_key" not in listed_calls[0]


def test_fetch_daily_quotes_pagination_and_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(url: str) -> dict[str, Any]:
        if "auth_refresh" in url:
            return {"idToken": "tok-1"}
        assert "code=72030" in url  # 4桁コードが5桁に正規化されて送られる
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if query.get("pagination_key") == "P2":
            return {"daily_quotes": [_quote_row("2025-01-07", 3100.0)]}
        return {
            "daily_quotes": [_quote_row("2025-01-06", 3000.0)],
            "pagination_key": "P2",
        }

    _install_urlopen(monkeypatch, handler)
    result = fetch_daily_quotes("7203", period="1y", id_token="tok-1")
    assert set(result.keys()) == {"7203"}  # キーは入力コードのまま
    df = result["7203"]
    assert len(df) == 2  # ページを跨いだ行が結合される
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_fetch_listed_info_empty_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(url: str) -> dict[str, Any]:
        if "auth_refresh" in url:
            return {"idToken": "tok-1"}
        return {"info": []}

    _install_urlopen(monkeypatch, handler)
    monkeypatch.setenv(jquants.REFRESH_TOKEN_ENV, "refresh-abc")
    with pytest.raises(JQuantsError):
        fetch_listed_info()

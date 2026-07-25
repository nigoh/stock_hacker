"""edinet モジュールの検証（stocklib.edinet._http_get をモックし、ネットワーク不使用）。"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any, Callable

import pytest

from stocklib import edinet
from stocklib.edinet import (
    EdinetAuthError,
    EdinetError,
    fetch_document_csv,
    normalize_sec_code,
    search_documents,
)


# ---------------------------------------------------------------------------
# モックの部品
# ---------------------------------------------------------------------------


class _FakeResponse:
    """requests.Response の必要最小限（status_code / content / text / json()）を模す。"""

    def __init__(self, payload: dict[str, Any] | bytes, status_code: int = 200) -> None:
        self.status_code = status_code
        if isinstance(payload, bytes):
            self.content = payload
        else:
            self.content = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


def _install_http_get(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[str, dict[str, str]], _FakeResponse],
) -> list[tuple[str, dict[str, str]]]:
    """edinet._http_get をモックし、(url, params) の呼び出し履歴を返す。"""
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_http_get(url: str, params: dict[str, str]) -> _FakeResponse:
        calls.append((url, dict(params)))
        return handler(url, params)

    monkeypatch.setattr(edinet, "_http_get", fake_http_get)
    return calls


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(edinet.API_KEY_ENV, raising=False)


def _doc(
    doc_id: str,
    sec_code: str | None,
    doc_type: str,
    submitted: str,
    description: str = "有価証券報告書",
) -> dict[str, Any]:
    return {
        "docID": doc_id,
        "secCode": sec_code,
        "docTypeCode": doc_type,
        "docDescription": description,
        "filerName": "テスト株式会社",
        "submitDateTime": submitted,
        "periodStart": "2025-04-01",
        "periodEnd": "2026-03-31",
        "edinetCode": "E00000",
        "csvFlag": "1",
    }


# ---------------------------------------------------------------------------
# normalize_sec_code
# ---------------------------------------------------------------------------


def test_normalize_sec_code() -> None:
    assert normalize_sec_code("7203") == "72030"
    assert normalize_sec_code("7203.T") == "72030"
    assert normalize_sec_code("130a") == "130A0"
    assert normalize_sec_code("72030") == "72030"


def test_normalize_sec_code_invalid() -> None:
    for bad in ["", "72", "^N225", "720300"]:
        with pytest.raises(ValueError):
            normalize_sec_code(bad)


# ---------------------------------------------------------------------------
# 認証（EdinetAuthError）
# ---------------------------------------------------------------------------


def test_missing_api_key_raises_with_setup_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http_get(monkeypatch, lambda url, params: pytest.fail("ネットワークに出てはいけない"))
    with pytest.raises(EdinetAuthError) as exc_info:
        search_documents("7203", days=3)
    msg = str(exc_info.value)
    assert edinet.API_KEY_ENV in msg
    assert edinet.SIGNUP_URL in msg  # 利用登録ページへの導線


def test_http_401_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http_get(
        monkeypatch,
        lambda url, params: _FakeResponse({"message": "Access denied"}, status_code=401),
    )
    monkeypatch.setenv(edinet.API_KEY_ENV, "bad-key")
    with pytest.raises(EdinetAuthError) as exc_info:
        search_documents("7203", days=3, end_date="2026-07-16")
    assert "401" in str(exc_info.value)
    assert edinet.API_KEY_ENV in str(exc_info.value)


def test_http_500_is_generic_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http_get(
        monkeypatch,
        lambda url, params: _FakeResponse({"message": "server error"}, status_code=500),
    )
    monkeypatch.setenv(edinet.API_KEY_ENV, "key")
    with pytest.raises(EdinetError) as exc_info:
        search_documents("7203", days=3, end_date="2026-07-16")
    assert not isinstance(exc_info.value, EdinetAuthError)
    assert "500" in str(exc_info.value)


# ---------------------------------------------------------------------------
# search_documents — 書類一覧のパース・照合・日付レンジ
# ---------------------------------------------------------------------------


def test_search_documents_filters_and_sorts(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(url: str, params: dict[str, str]) -> _FakeResponse:
        assert url.endswith("/documents.json")
        assert params["type"] == "2"
        if params["date"] == "2026-07-14":
            return _FakeResponse(
                {
                    "results": [
                        _doc("D001", "72030", "120", "2026-07-14 15:00"),  # 対象
                        _doc("D002", "67580", "120", "2026-07-14 15:01"),  # 別銘柄
                        _doc("D003", "72030", "180", "2026-07-14 15:02"),  # 対象外の書類種別
                        _doc("D004", None, "120", "2026-07-14 15:03"),  # secCode なし（ファンド等）
                    ]
                }
            )
        if params["date"] == "2026-07-16":
            return _FakeResponse(
                {"results": [_doc("D005", "72030", "160", "2026-07-16 15:00", "半期報告書")]}
            )
        return _FakeResponse({"results": []})

    calls = _install_http_get(monkeypatch, handler)
    monkeypatch.setenv(edinet.API_KEY_ENV, "test-key")

    # 2026-07-16(木) から 5 日遡ると 07-12(日) は土日スキップ → 呼び出しは 4 回
    df = search_documents("7203", days=5, end_date="2026-07-16")
    assert len(calls) == 4
    assert all(p["Subscription-Key"] == "test-key" for _, p in calls)
    queried_dates = {p["date"] for _, p in calls}
    assert "2026-07-12" not in queried_dates  # 日曜はスキップ

    assert list(df.columns) == list(edinet.RESULT_COLUMNS)
    assert list(df["docID"]) == ["D005", "D001"]  # 提出日時の降順
    assert list(df["docTypeCode"]) == ["160", "120"]


def test_search_documents_empty_returns_empty_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http_get(monkeypatch, lambda url, params: _FakeResponse({"results": []}))
    monkeypatch.setenv(edinet.API_KEY_ENV, "test-key")
    df = search_documents("7203", days=3, end_date="2026-07-16")
    assert df.empty
    assert list(df.columns) == list(edinet.RESULT_COLUMNS)


def test_search_documents_handles_missing_results_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # 休日等で results キー自体が無い応答でも落ちない
    _install_http_get(monkeypatch, lambda url, params: _FakeResponse({"metadata": {}}))
    monkeypatch.setenv(edinet.API_KEY_ENV, "test-key")
    df = search_documents("7203", days=2, end_date="2026-07-16")
    assert df.empty


def test_search_documents_custom_doc_types(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(url: str, params: dict[str, str]) -> _FakeResponse:
        return _FakeResponse(
            {
                "results": [
                    _doc("D010", "72030", "120", f"{params['date']} 15:00"),
                    _doc("D011", "72030", "160", f"{params['date']} 15:01"),
                ]
            }
        )

    _install_http_get(monkeypatch, handler)
    monkeypatch.setenv(edinet.API_KEY_ENV, "test-key")
    df = search_documents("7203", doc_types=("160",), days=1, end_date="2026-07-16")
    assert set(df["docTypeCode"]) == {"160"}


# ---------------------------------------------------------------------------
# fetch_document_csv — zip 内 CSV のパース
# ---------------------------------------------------------------------------


def _make_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _edinet_csv_bytes(rows: list[tuple[str, str, str]]) -> bytes:
    """EDINET 実物と同じ UTF-16LE(BOM)・タブ区切りの CSV バイト列を作る。"""
    lines = ["要素ID\t項目名\t値"]
    lines += [f"{a}\t{b}\t{c}" for a, b, c in rows]
    return "\n".join(lines).encode("utf-16")


def test_fetch_document_csv_parses_zip(monkeypatch: pytest.MonkeyPatch) -> None:
    zip_bytes = _make_zip(
        {
            "XBRL_TO_CSV/jpcrp030000-asr-001_E00000-000_2026-03-31_01.csv": _edinet_csv_bytes(
                [("jpcrp_cor:NetSales", "売上高", "1000000"), ("jpcrp_cor:OperatingIncome", "営業利益", "100000")]
            ),
            "XBRL_TO_CSV/jpaud-aar-cn-001_E00000-000_2026-03-31_01.csv": _edinet_csv_bytes(
                [("jpaud:AuditOpinion", "監査意見", "無限定適正")]
            ),
            "XBRL_TO_CSV/manifest.xml": b"<manifest/>",  # CSV 以外は無視される
        }
    )

    def handler(url: str, params: dict[str, str]) -> _FakeResponse:
        assert url.endswith("/documents/S100TEST")
        assert params["type"] == "5"
        return _FakeResponse(zip_bytes)

    _install_http_get(monkeypatch, handler)
    monkeypatch.setenv(edinet.API_KEY_ENV, "test-key")

    df = fetch_document_csv("S100TEST")
    assert len(df) == 3  # 2ファイル分が結合される
    assert "ソースファイル" in df.columns
    sales = df[df["要素ID"] == "jpcrp_cor:NetSales"]
    assert sales["値"].iloc[0] == "1000000"
    assert sales["ソースファイル"].iloc[0].startswith("jpcrp")


def test_fetch_document_csv_not_zip_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # csvFlag が立っていない書類等では JSON エラーが返る
    _install_http_get(
        monkeypatch,
        lambda url, params: _FakeResponse({"metadata": {"status": "404", "message": "not found"}}),
    )
    monkeypatch.setenv(edinet.API_KEY_ENV, "test-key")
    with pytest.raises(EdinetError) as exc_info:
        fetch_document_csv("S100BAD")
    assert "S100BAD" in str(exc_info.value)


def test_fetch_document_csv_zip_without_csv_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    zip_bytes = _make_zip({"XBRL_TO_CSV/manifest.xml": b"<manifest/>"})
    _install_http_get(monkeypatch, lambda url, params: _FakeResponse(zip_bytes))
    monkeypatch.setenv(edinet.API_KEY_ENV, "test-key")
    with pytest.raises(EdinetError):
        fetch_document_csv("S100TEST")


def test_fetch_document_csv_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http_get(monkeypatch, lambda url, params: pytest.fail("ネットワークに出てはいけない"))
    with pytest.raises(EdinetAuthError):
        fetch_document_csv("S100TEST")


# --------------------------------------------------------------------------
# API キーの漏洩防止（セキュリティ回帰）
# --------------------------------------------------------------------------

def test_connection_error_does_not_leak_api_key(monkeypatch) -> None:
    """接続エラー時、例外メッセージに API キーが含まれない（回帰）。

    EDINET はキーをクエリ（Subscription-Key）で渡す仕様のため、requests の
    例外文字列（URL 全体を含む）をそのまま埋め込むとキーが漏れる。
    その文字列は fundamentals_report がレポート本文に書くため実害が大きい。
    """
    import requests
    secret = "SUPERSECRETKEY123456"
    monkeypatch.setenv(edinet.API_KEY_ENV, secret)

    def _boom(url, params=None, timeout=None):
        # requests が実際に出す形（URL とクエリを含む）を模す
        raise requests.ConnectionError(
            f"HTTPSConnectionPool(host='api.edinet-fsa.go.jp', port=443): "
            f"Max retries exceeded with url: /api/v2/documents.json"
            f"?date=2026-01-01&Subscription-Key={secret}"
        )

    monkeypatch.setattr(requests, "get", _boom)
    with pytest.raises(edinet.EdinetError) as ei:
        edinet._api_get("/documents.json", {"date": "2026-01-01"})
    msg = str(ei.value)
    assert secret not in msg, f"API キーが例外メッセージに漏れている: {msg}"
    # 例外連鎖からも漏れないこと（__cause__ を辿ってもキーが出ない）
    assert ei.value.__cause__ is None


def test_error_response_body_redacts_api_key(monkeypatch) -> None:
    """応答本文にキーがエコーバックされても伏字化される（回帰）。"""
    secret = "SUPERSECRETKEY123456"
    monkeypatch.setenv(edinet.API_KEY_ENV, secret)

    class _Resp:
        status_code = 500
        text = f"error for url ?Subscription-Key={secret}"

    monkeypatch.setattr(edinet, "_http_get", lambda url, params: _Resp())
    with pytest.raises(edinet.EdinetError) as ei:
        edinet._api_get("/documents.json", {"date": "2026-01-01"})
    assert secret not in str(ei.value)
    assert "<REDACTED>" in str(ei.value)

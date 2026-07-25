"""API キーが外部に漏れないことを、全ての秘密を扱うモジュール横断で守る回帰テスト。

**このテストが存在する理由（実際に起きた漏洩）**

EDINET API はキーを URL クエリ（``Subscription-Key``）で渡す仕様である。そこへ
requests の例外文字列をそのまま埋め込んでいたため、キーが流出していた。
requests の例外メッセージは **リクエスト URL 全体を含む**:

    HTTPSConnectionPool(host='api.edinet-fsa.go.jp', port=443): Max retries
    exceeded with url: /api/v2/documents.json?date=...&Subscription-Key=<KEY>

さらに ``fundamentals_report.py`` はこの例外文字列を **レポート本文に書き込む**
ため、``reports/fundamentals-*.md`` にキーが平文で焼き込まれていた。攻撃者は不要で、
DNS 失敗やタイムアウトが一度起きるだけで発生する。``search_documents(days=365)``
は約250回 GET するため遭遇確率も低くない。

**不変条件**: 秘密（API キー）は、例外メッセージ・例外連鎖・レポート本文の
いずれにも決して現れてはならない。新しい API を追加したときも同じ検査が
効くよう、モジュール横断のパラメータ化テストにしてある。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

from stocklib import edinet, jquants

SECRET = "SUPERSECRETKEY_do_not_leak_1234567890"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _connection_error_with_url(secret: str) -> requests.RequestException:
    """requests が実際に出す形（URL とクエリを含む）の接続例外を作る。"""
    return requests.ConnectionError(
        "HTTPSConnectionPool(host='example.invalid', port=443): "
        "Max retries exceeded with url: /api/v2/documents.json"
        f"?date=2026-01-01&Subscription-Key={secret} (Caused by NewConnectionError)"
    )


def _assert_no_secret(exc: BaseException, secret: str, where: str) -> None:
    """秘密が「人・レポートに届く経路」に現れないことを検証する。

    検査するのは実際に外部へ出る2経路のみ:

    1. ``str(exc)`` — レポート本文・stderr にそのまま埋め込まれる文字列
    2. ``traceback.format_exception(exc)`` — 未捕捉時に表示される全文

    例外連鎖を素朴に辿ると ``raise ... from None``（``__suppress_context__``）で
    抑制済みの文脈まで拾ってしまい、実際には表示されないものを漏洩と誤判定する。
    Python の traceback 機構と同じ判断基準に合わせる。
    """
    import traceback

    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    for label, text in (("str(exc)", str(exc)), ("traceback 表示", rendered)):
        assert secret not in text, (
            f"{where}: API キーが {label} に漏れています。\n"
            f"この文字列はレポート本文・stderr に届くため、"
            f"URL を含む例外文字列をそのまま埋め込まないでください。\n{text[:600]}"
        )


# ---------------------------------------------------------------------------
# EDINET（キーをクエリで渡す = 最も漏れやすい）
# ---------------------------------------------------------------------------


def test_edinet_connection_error_does_not_leak_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """接続エラー時、キーが例外にも例外連鎖にも現れない。"""
    monkeypatch.setenv(edinet.API_KEY_ENV, SECRET)
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: (_ for _ in ()).throw(_connection_error_with_url(SECRET))
    )
    with pytest.raises(edinet.EdinetError) as ei:
        edinet._api_get("/documents.json", {"date": "2026-01-01"})
    _assert_no_secret(ei.value, SECRET, "edinet._api_get（接続エラー）")


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_edinet_error_response_does_not_leak_key(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """応答本文にキーがエコーバックされても伏字化される。"""
    monkeypatch.setenv(edinet.API_KEY_ENV, SECRET)

    class _Resp:
        status_code = status
        text = f"error: /api/v2/documents.json?Subscription-Key={SECRET}"

    monkeypatch.setattr(edinet, "_http_get", lambda url, params: _Resp())
    with pytest.raises(edinet.EdinetError) as ei:
        edinet._api_get("/documents.json", {"date": "2026-01-01"})
    _assert_no_secret(ei.value, SECRET, f"edinet._api_get（HTTP {status}）")


def test_edinet_failure_message_written_to_report_has_no_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fundamentals_report がレポートに書く失敗メッセージにキーが入らない。

    これが実際に起きた漏洩の最終地点（reports/*.md への焼き込み）。
    """
    monkeypatch.setenv(edinet.API_KEY_ENV, SECRET)
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: (_ for _ in ()).throw(_connection_error_with_url(SECRET))
    )
    try:
        edinet.search_documents("7203", days=1)
        message = ""
    except edinet.EdinetError as exc:
        message = f"EDINET 書類一覧の取得に失敗した: {exc}"
    assert SECRET not in message, (
        "レポート本文に書かれる失敗メッセージに API キーが混入しています。"
        f"\n{message[:400]}"
    )


# ---------------------------------------------------------------------------
# J-Quants（キーをヘッダで渡す = 本来漏れない。退行を防ぐ）
# ---------------------------------------------------------------------------


def test_jquants_key_is_sent_as_header_not_query() -> None:
    """J-Quants はキーをヘッダで送る（URL に載せると EDINET と同じ穴が開く）。

    実装がクエリ渡しに変わると、例外・ログ経由の漏洩経路が生まれるため固定する。
    """
    source = (REPO_ROOT / "analysis" / "stocklib" / "jquants.py").read_text(encoding="utf-8")
    assert "x-api-key" in source, "J-Quants のキーはヘッダ（x-api-key）で送ってください"
    # Python のキーワード引数（api_key=...）は正常な使い方なので除外し、
    # URL クエリ文字列としてキーを載せる形だけを検出する。
    query_key_patterns = (
        "?apikey=", "&apikey=", "?api_key=", "&api_key=",
        "?key=", "&key=", "Subscription-Key",
    )
    offenders = [p for p in query_key_patterns if p in source]
    assert not offenders, (
        f"J-Quants の実装に URL クエリでキーを渡す形跡があります: {offenders}。"
        "クエリに載せると例外メッセージ経由で漏洩します（EDINET と同じ穴）。"
    )


def test_jquants_auth_error_does_not_leak_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """J-Quants の認証エラーでもキーが例外に現れない。"""
    monkeypatch.setenv(jquants.API_KEY_ENV, SECRET)
    err = jquants.JQuantsAuthError(jquants._setup_message())
    _assert_no_secret(err, SECRET, "jquants の認証エラーメッセージ")


# ---------------------------------------------------------------------------
# 横断: 秘密を扱うモジュール全体の静的検査
# ---------------------------------------------------------------------------


def test_no_module_embeds_raw_exception_with_url_in_user_facing_error() -> None:
    """秘密を扱うモジュールが、URL を含む例外を素で埋め込んでいないか。

    ``raise XxxError(f"...: {exc}")`` の形は、requests の例外だと URL ごと
    露出する。秘密をクエリで渡すモジュールでは特に危険なので、
    ``type(exc).__name__`` を使うか伏字化を通す運用に寄せる。
    """
    secret_modules = ["edinet.py"]  # キーをクエリで渡すモジュール
    offenders: list[str] = []
    for name in secret_modules:
        path = REPO_ROOT / "analysis" / "stocklib" / name
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            # f-string で例外オブジェクトをそのまま展開している行
            if "{exc}" in stripped and "type(exc)" not in stripped and "_redact" not in stripped:
                offenders.append(f"{name}:{lineno}: {stripped}")
    assert not offenders, (
        "URL を含む例外文字列をそのまま埋め込んでいます（API キー漏洩の経路）:\n"
        + "\n".join(offenders)
    )

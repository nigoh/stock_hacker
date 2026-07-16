"""EDINET API v2 クライアント（金融庁の法定開示書類システム）。

有価証券報告書・半期報告書などの法定開示書類の一覧検索（``documents.json``）と、
書類本体の CSV（XBRL を CSV 化したもの、type=5）の取得を提供する。
エンドポイントは ``https://api.edinet-fsa.go.jp/api/v2/...``。

**APIキーが必須（2024年時点）**: 2024年以降、EDINET API の利用には利用登録による
APIキー（``Subscription-Key``）の発行が必要になった。環境変数 ``EDINET_API_KEY``
に設定して使う。

事前準備（無料）:

1. EDINET の API利用登録ページ（https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1 、
   EDINET トップ https://disclosure2.edinet-fsa.go.jp/ の「EDINET API」からも辿れる）で
   アカウントを登録し、APIキーを発行する。
2. 環境変数を設定する::

       export EDINET_API_KEY="<APIキー>"

使用例::

    from stocklib.edinet import search_documents, fetch_document_csv

    docs = search_documents("7203", days=365)      # 直近1年の有報・半期報告書等
    print(docs[["docID", "docTypeCode", "docDescription", "submitDateTime"]])
    df = fetch_document_csv(docs["docID"].iloc[0])  # 財務諸表CSV（XBRL項目の縦持ち）

注意（2024年時点の仕様）:

- EDINET の証券コード（``secCode``）は5桁（従来の4桁コード + 予備桁 ``0``）。本モジュール
  は4桁コードを自動で5桁に正規化して照合する（例: ``"7203"`` → ``"72030"``）。
- **決算短信は適時開示（TDnet、東証）であり EDINET では取得できない。** EDINET で取れる
  のは金商法に基づく法定開示（有価証券報告書・半期報告書・大量保有報告書等）のみ。
- 四半期報告書（docTypeCode=140）は2024年4月以降の金商法改正で廃止され、半期報告書
  （160）に一本化された。140 は過去分の検索用に既定値に残している。
- 書類一覧 API は「1リクエスト = 1日分」の設計のため、期間検索は日数分のリクエストを
  発行する（土日はスキップ）。``days`` を大きくすると時間がかかる。

ネットワーク層は :func:`_http_get`（requests の薄いラッパー）に集約しており、テストでは
これをモックする（``analysis/tests/test_edinet.py`` 参照）。
"""

from __future__ import annotations

import datetime as dt
import io
import os
import re
import zipfile
from typing import Any, Sequence

import pandas as pd

from stocklib.data import DataFetchError

API_BASE: str = "https://api.edinet-fsa.go.jp/api/v2"
API_KEY_ENV: str = "EDINET_API_KEY"
SIGNUP_URL: str = "https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1"

_REQUEST_TIMEOUT: float = 30.0

# 主要な書類種別コード（2024年時点。四半期報告書は2024年4月以降廃止、過去分検索用）
DOC_TYPE_LABELS: dict[str, str] = {
    "120": "有価証券報告書",
    "130": "訂正有価証券報告書",
    "140": "四半期報告書",
    "150": "訂正四半期報告書",
    "160": "半期報告書",
    "170": "訂正半期報告書",
}

# search_documents の返す DataFrame の列（この順）
RESULT_COLUMNS: tuple[str, ...] = (
    "docID",
    "docTypeCode",
    "docDescription",
    "filerName",
    "submitDateTime",
    "periodStart",
    "periodEnd",
    "secCode",
    "edinetCode",
    "csvFlag",
)


class EdinetError(DataFetchError):
    """EDINET API の呼び出しに失敗したことを示す例外。"""


class EdinetAuthError(EdinetError):
    """EDINET の APIキーが未設定・無効であることを示す例外。"""


def _setup_message() -> str:
    return (
        f"EDINET の APIキーが環境変数 {API_KEY_ENV} に設定されていません。\n"
        "2024年以降、EDINET API の利用にはAPIキーの登録が必須です（無料）。導入手順:\n"
        f"  1. EDINET API の利用登録ページ（{SIGNUP_URL}）でアカウントを登録し、APIキーを発行する\n"
        "     （EDINET トップ https://disclosure2.edinet-fsa.go.jp/ の「EDINET API」からも辿れる）。\n"
        f'  2. 環境変数を設定する: export {API_KEY_ENV}="<APIキー>"\n'
        "詳細は knowledge/data-sources/data-apis-and-tools.md の EDINET 節を参照してください。"
    )


def normalize_sec_code(code: str) -> str:
    """銘柄コードを EDINET の ``secCode``（5桁）形式に正規化する。

    - 4桁コード（``"7203"``、英字入り ``"130A"`` も可）→ 予備桁 ``"0"`` を付けて5桁化
    - ``"7203.T"`` のような yfinance 形式 → サフィックスを外して5桁化
    - 既に5桁（``"72030"`` 等）はそのまま返す。
    """
    code = code.strip().upper().removesuffix(".T")
    if re.fullmatch(r"[0-9][0-9A-Z][0-9][0-9A-Z]", code):
        return f"{code}0"
    if re.fullmatch(r"[0-9][0-9A-Z][0-9][0-9A-Z][0-9]", code):
        return code
    raise ValueError(f"EDINET の証券コードとして解釈できません: {code!r}（例: '7203', '72030'）")


def _http_get(url: str, params: dict[str, str]) -> Any:
    """requests.get の薄いラッパー。テストではこの関数をモックする。

    接続エラーは :class:`EdinetError` に変換する。HTTP ステータスの解釈は呼び出し側
    （:func:`_api_get`）が行う。
    """
    import requests

    try:
        return requests.get(url, params=params, timeout=_REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise EdinetError(
            f"EDINET API に接続できませんでした（ネットワークを確認してください）: {exc}"
        ) from exc


def _api_get(path: str, params: dict[str, str], api_key: str | None = None) -> Any:
    """APIキーを付与して GET し、ステータスを検証したレスポンスを返す。"""
    key = api_key if api_key is not None else os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise EdinetAuthError(_setup_message())
    query = dict(params)
    query["Subscription-Key"] = key
    resp = _http_get(f"{API_BASE}{path}", query)
    if resp.status_code in (401, 403):
        raise EdinetAuthError(
            f"EDINET API の認証に失敗しました（HTTP {resp.status_code}）。"
            f"APIキーが無効か失効している可能性があります。{SIGNUP_URL} で確認・再発行し、"
            f"環境変数 {API_KEY_ENV} を更新してください。応答: {resp.text[:300]}"
        )
    if resp.status_code != 200:
        raise EdinetError(
            f"EDINET API 呼び出しに失敗しました（HTTP {resp.status_code}）: {resp.text[:300]}"
        )
    return resp


def search_documents(
    code_4digit: str,
    doc_types: Sequence[str] = ("120", "140", "160"),
    days: int = 365,
    *,
    end_date: str | dt.date | None = None,
    api_key: str | None = None,
) -> pd.DataFrame:
    """書類一覧 API（``documents.json``, type=2）を日付レンジで叩き、対象銘柄の書類を返す。

    銘柄コードは EDINET の ``secCode``（5桁 = 4桁コード + 予備桁 ``0``）で照合する。
    既定の書類種別は有価証券報告書（120）・四半期報告書（140、2024年4月以降廃止・過去分用）・
    半期報告書（160）。**決算短信は東証の適時開示（TDnet）であり EDINET には存在しない**
    点に注意（法定開示のみが対象）。

    書類一覧 API は1リクエストで1日分しか返さないため、``days`` 日分（土日を除く）の
    リクエストを新しい日付から順に発行する。``days=365`` で250回程度の HTTP GET になる。

    Args:
        code_4digit: 銘柄コード（4桁。``"72030"`` のような5桁も可）。
        doc_types: 対象の ``docTypeCode``（:data:`DOC_TYPE_LABELS` 参照）。
        days: 遡る日数（暦日）。
        end_date: 検索の終端日（既定: 今日）。テスト・再現用。
        api_key: APIキーを直接渡す場合に指定。``None`` なら環境変数から読む。

    Returns:
        :data:`RESULT_COLUMNS` を列に持つ ``pd.DataFrame``（提出日時の降順）。
        該当書類が無ければ空の DataFrame（列は同じ）を返す。

    Raises:
        EdinetAuthError: APIキー未設定・認証失敗の場合。
        EdinetError: API 呼び出し失敗の場合。
    """
    sec_code = normalize_sec_code(code_4digit)
    wanted = {str(t) for t in doc_types}
    if end_date is None:
        end = dt.date.today()
    elif isinstance(end_date, str):
        end = dt.date.fromisoformat(end_date)
    else:
        end = end_date

    rows: list[dict[str, Any]] = []
    for offset in range(days):
        day = end - dt.timedelta(days=offset)
        if day.weekday() >= 5:  # 土日は法定開示の提出が無いためスキップ
            continue
        resp = _api_get(
            "/documents.json",
            {"date": day.isoformat(), "type": "2"},
            api_key=api_key,
        )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise EdinetError(
                f"EDINET API の応答を JSON として解釈できませんでした（date={day}）: "
                f"{resp.text[:200]}"
            ) from exc
        for item in payload.get("results") or []:
            if item.get("secCode") != sec_code:
                continue
            if str(item.get("docTypeCode")) not in wanted:
                continue
            rows.append({col: item.get(col) for col in RESULT_COLUMNS})

    df = pd.DataFrame(rows, columns=list(RESULT_COLUMNS))
    if not df.empty:
        df = df.sort_values("submitDateTime", ascending=False).reset_index(drop=True)
    return df


def _read_member_csv(raw: bytes) -> pd.DataFrame:
    """zip 内の CSV 1ファイルを DataFrame にする（EDINET は UTF-16LE・タブ区切り）。"""
    last_error: Exception | None = None
    for encoding, sep in (("utf-16", "\t"), ("utf-8-sig", ","), ("cp932", ",")):
        try:
            return pd.read_csv(io.BytesIO(raw), sep=sep, encoding=encoding, dtype=str)
        except Exception as exc:  # noqa: BLE001 — 次の候補エンコーディングを試す
            last_error = exc
    raise EdinetError(f"財務諸表 CSV を解釈できませんでした: {last_error}")


def fetch_document_csv(doc_id: str, *, api_key: str | None = None) -> pd.DataFrame:
    """書類取得 API（type=5, CSV）で zip を取得し、財務諸表 CSV を DataFrame にして返す。

    EDINET の CSV は XBRL 項目の縦持ち形式（``要素ID`` / ``項目名`` / ``コンテキストID`` /
    ``相対年度`` / ``連結・個別`` / ``期間・時点`` / ``単位`` / ``値`` などの列）。zip には
    複数の CSV（本体 ``jpcrp...`` と監査報告 ``jpaud...`` 等）が含まれるため、全て結合し
    ``ソースファイル`` 列で由来を区別できるようにする。財務諸表本体は ``jpcrp`` 系。

    Args:
        doc_id: 書類管理番号（:func:`search_documents` の ``docID`` 列）。
        api_key: APIキーを直接渡す場合に指定。``None`` なら環境変数から読む。

    Returns:
        zip 内の全 CSV を縦結合した ``pd.DataFrame``（全列 ``str``、``ソースファイル`` 列付き）。

    Raises:
        EdinetAuthError: APIキー未設定・認証失敗の場合。
        EdinetError: 書類が存在しない・CSV 未提供（``csvFlag != "1"``）・解釈不能の場合。
    """
    resp = _api_get(f"/documents/{doc_id}", {"type": "5"}, api_key=api_key)
    content: bytes = resp.content
    if not content.startswith(b"PK"):  # zip マジックナンバー
        raise EdinetError(
            f"{doc_id} の CSV（zip）を取得できませんでした。書類が CSV 非対応"
            f"（csvFlag != '1'）か docID が不正の可能性があります。応答冒頭: {content[:200]!r}"
        )
    frames: list[pd.DataFrame] = []
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for name in zf.namelist():
            if not name.lower().endswith(".csv"):
                continue
            df = _read_member_csv(zf.read(name))
            df["ソースファイル"] = name.rsplit("/", 1)[-1]
            frames.append(df)
    if not frames:
        raise EdinetError(f"{doc_id} の zip に CSV ファイルが含まれていませんでした。")
    return pd.concat(frames, ignore_index=True)

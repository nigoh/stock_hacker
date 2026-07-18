"""J-Quants API（JPX総研）接続モジュール — **V2（APIキー方式）対応**。

環境変数 ``JQUANTS_API_KEY`` に設定した API キーを ``x-api-key`` ヘッダに載せて、
上場銘柄一覧（``/equities/master``）と日足四本値（``/equities/bars/daily``）を取得する。
日足は :func:`stocklib.data.fetch_prices` と同じ OHLCV DataFrame 形式
（``Open/High/Low/Close/Volume`` 列、営業日 ``DatetimeIndex``）で返す。

外部依存は標準ライブラリ（urllib）+ pandas のみ。

**V1→V2 移行（2025年12月〜、2026年時点）**: J-Quants は 2025/12/22 に V2 へ刷新され、
認証が「リフレッシュトークン→IDトークン方式（V1）」から「**APIキー方式（V2）**」に変わった。
API キーは無期限（V1 のリフレッシュトークンの約1週間の期限は撤廃）。旧 V1 は並走期間を経て
廃止された。本モジュールは V2 のみに対応する。

事前準備（無料プランで可）:

1. https://jpx-jquants.com/ でアカウント登録（Free プランは12週間遅延データ）。
2. ダッシュボードで発行される **API キー**（無期限）を環境変数に設定::

       export JQUANTS_API_KEY="<APIキー>"

使用例::

    from stocklib.jquants import fetch_daily_quotes, fetch_listed_info

    # 日足四本値（4桁コードは内部で J-Quants の5桁形式 "72030" に正規化）
    prices = fetch_daily_quotes(["7203", "6758"], period="1y")
    df = prices["7203"]          # Open/High/Low/Close/Volume の DataFrame
    print(df["Close"].tail())

    # 上場銘柄一覧（全銘柄スクリーニングのユニバース構築に利用）
    listed = fetch_listed_info()
    print(listed[["Code", "CompanyName", "Sector33CodeName"]].head())

注意（2026年時点の V2 仕様）:

- J-Quants の銘柄コードは5桁（従来の4桁コード + 予備桁）。本モジュールは4桁コードを
  自動で5桁に正規化する（例: ``"7203"`` → ``"72030"``）。
- 株価は分割・併合調整済みの ``AdjO`` 等（V2の短縮カラム名）を優先して使用する。配当落ち
  調整は含まれないため、yfinance（``auto_adjust=True``、配当込み調整）の系列とは一致しない。
- V2 のレスポンスはデータ配列を ``data`` キー、ページングを ``pagination_key`` で返す。
  四本値のカラム名は短縮形（``O``/``H``/``L``/``C``/``Vo``、調整済みは ``AdjO`` 等）。
  上場銘柄一覧（``/equities/master``）のカラムも短縮形（``CoName``/``S33Nm`` 等）のため、
  本モジュールが V1 相当の安定名（``CompanyName``/``Sector33CodeName`` 等）へ正規化して返す。
- 詳細は ``knowledge/data-sources/data-apis-and-tools.md`` の J-Quants 節、および
  ``knowledge/data-sources/market-data-apis-catalog.md`` を参照。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Sequence

import pandas as pd

from stocklib.data import OHLCV_COLUMNS, DataFetchError, period_to_days

API_BASE: str = "https://api.jquants.com/v2"
API_KEY_ENV: str = "JQUANTS_API_KEY"
SIGNUP_URL: str = "https://jpx-jquants.com/"

_REQUEST_TIMEOUT: float = 30.0

# J-Quants V2 → stocklib OHLCV 列の対応（調整済み系列を優先）。
# V2 の日足四本値カラムは短縮形（O/H/L/C/Vo、調整済みは AdjO/AdjH/AdjL/AdjC/AdjVo）。
_ADJUSTED_COLUMNS: dict[str, str] = {
    "AdjO": "Open",
    "AdjH": "High",
    "AdjL": "Low",
    "AdjC": "Close",
    "AdjVo": "Volume",
}
_RAW_COLUMNS: dict[str, str] = {
    "O": "Open",
    "H": "High",
    "L": "Low",
    "C": "Close",
    "Vo": "Volume",
}

# /equities/master（V2、短縮カラム名）→ V1 相当の安定名。build_universe.py 等が
# ``CompanyName`` / ``Sector33CodeName`` / ``MarketCodeName`` を前提にするため正規化する。
_MASTER_RENAME: dict[str, str] = {
    "CoName": "CompanyName",
    "CoNameEn": "CompanyNameEnglish",
    "S17": "Sector17Code",
    "S17Nm": "Sector17CodeName",
    "S33": "Sector33Code",
    "S33Nm": "Sector33CodeName",
    "ScaleCat": "ScaleCategory",
    "Mkt": "MarketCode",
    "MktNm": "MarketCodeName",
}


class JQuantsError(DataFetchError):
    """J-Quants API の呼び出しに失敗したことを示す例外。"""


class JQuantsAuthError(JQuantsError):
    """J-Quants の認証情報が未設定・無効であることを示す例外。"""


def _setup_message() -> str:
    return (
        f"J-Quants の API キーが環境変数 {API_KEY_ENV} に設定されていません。\n"
        "導入手順（無料プランで利用可能・V2 APIキー方式、2026年時点）:\n"
        f"  1. {SIGNUP_URL} でアカウントを登録する（Free プランは12週間遅延データ）。\n"
        "  2. ダッシュボードで API キー（無期限）を発行する。\n"
        f'  3. 環境変数を設定する: export {API_KEY_ENV}="<APIキー>"\n'
        "（2025年12月の V2 移行でリフレッシュトークン方式は廃止されました。）\n"
        "詳細は knowledge/data-sources/data-apis-and-tools.md の J-Quants 節を参照してください。"
    )


def normalize_jquants_code(code: str) -> str:
    """銘柄コードを J-Quants の5桁形式に正規化する。

    - 4桁数字（``"7203"``）→ 予備桁 ``"0"`` を付けて ``"72030"``
    - ``"7203.T"`` のような yfinance 形式 → サフィックスを外して5桁化
    - 既に5桁英数字（``"72030"``、``"130A0"`` 等）はそのまま返す。
    """
    code = code.strip().upper().removesuffix(".T")
    if re.fullmatch(r"[0-9][0-9A-Z][0-9][0-9A-Z]", code):
        return f"{code}0"
    if re.fullmatch(r"[0-9][0-9A-Z][0-9][0-9A-Z][0-9]", code):
        return code
    raise ValueError(f"J-Quants の銘柄コードとして解釈できません: {code!r}（例: '7203', '72030'）")


def get_api_key(api_key: str | None = None) -> str:
    """J-Quants V2 の API キーを取得する（引数 > 環境変数 ``JQUANTS_API_KEY``）。

    Args:
        api_key: 明示的に渡す API キー。``None`` の場合は環境変数から読み取る。

    Returns:
        ``x-api-key`` ヘッダに載せる API キー。

    Raises:
        JQuantsAuthError: API キーが未設定の場合（導入手順つきメッセージ）。
    """
    if api_key is None:
        api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise JQuantsAuthError(_setup_message())
    return api_key


def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """urllib で JSON API を呼び出し、パース済み辞書を返す。"""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        if exc.code in (400, 401, 403):
            raise JQuantsAuthError(
                f"J-Quants API の認証に失敗しました（HTTP {exc.code}）。"
                f"API キーが無効または未設定の可能性があります。"
                f"{SIGNUP_URL} のダッシュボードで確認・再発行し、環境変数 {API_KEY_ENV} を"
                f"更新してください。応答: {detail}"
            ) from exc
        raise JQuantsError(f"J-Quants API 呼び出しに失敗しました（HTTP {exc.code}）: {detail}") from exc
    except urllib.error.URLError as exc:
        raise JQuantsError(
            f"J-Quants API に接続できませんでした（ネットワークを確認してください）: {exc.reason}"
        ) from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise JQuantsError(f"J-Quants API の応答を JSON として解釈できませんでした: {body[:200]}") from exc


def _get_paginated(
    path: str,
    params: dict[str, str],
    result_key: str = "data",
    *,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """pagination_key を辿りながら全ページの結果を結合して返す（V2、APIキー認証）。"""
    key = get_api_key(api_key)
    headers = {"x-api-key": key}
    rows: list[dict[str, Any]] = []
    page_params = dict(params)
    while True:
        data = _http_json(f"{API_BASE}{path}", headers=headers, params=page_params)
        rows.extend(data.get(result_key, []))
        pagination_key = data.get("pagination_key")
        if not pagination_key:
            return rows
        page_params = dict(params)
        page_params["pagination_key"] = str(pagination_key)


def fetch_listed_info(
    code: str | None = None,
    date: str | None = None,
    *,
    api_key: str | None = None,
) -> pd.DataFrame:
    """上場銘柄一覧（V2 ``/equities/master``）を取得する。

    全銘柄スクリーニングのユニバース構築に利用できる。V2 の短縮カラム名
    （``CoName``/``S33Nm`` 等）は V1 相当の安定名（``CompanyName``/``Sector33CodeName``/
    ``MarketCodeName`` 等）へ正規化して返す（``Code`` は5桁のまま）。

    Args:
        code: 特定銘柄のみ取得する場合の銘柄コード（4桁/5桁どちらでも可）。``None`` で全銘柄。
        date: 基準日（``"2025-01-06"`` または ``"20250106"``）。``None`` で最新。
        api_key: 明示的に渡す API キー。``None`` なら環境変数 ``JQUANTS_API_KEY`` から取得。

    Returns:
        1行 = 1銘柄の ``pd.DataFrame``。

    Raises:
        JQuantsAuthError: API キー未設定・認証失敗の場合。
        JQuantsError: API 呼び出し失敗・空データの場合。
    """
    params: dict[str, str] = {}
    if code is not None:
        params["code"] = normalize_jquants_code(code)
    if date is not None:
        params["date"] = date
    rows = _get_paginated("/equities/master", params, "data", api_key=api_key)
    if not rows:
        raise JQuantsError("上場銘柄一覧が空でした。プランの提供範囲と指定日を確認してください。")
    df = pd.DataFrame(rows)
    return df.rename(columns={k: v for k, v in _MASTER_RENAME.items() if k in df.columns})


def _quotes_to_ohlcv(rows: list[dict[str, Any]], code: str) -> pd.DataFrame:
    """daily bars の応答行を fetch_prices 互換の OHLCV DataFrame に変換する（V2 カラム）。"""
    if not rows:
        raise JQuantsError(
            f"{code} の日足データが空でした。銘柄コードと期間を確認してください"
            "（Free プランは12週間遅延のため直近データは取得できません）。"
        )
    df = pd.DataFrame(rows)
    mapping = _ADJUSTED_COLUMNS if all(c in df.columns for c in _ADJUSTED_COLUMNS) else _RAW_COLUMNS
    missing = [c for c in mapping if c not in df.columns]
    if missing:
        raise JQuantsError(f"{code} の応答に四本値の列がありません（欠損列: {missing}）")
    out = df[["Date", *mapping.keys()]].rename(columns=mapping)
    out["Date"] = pd.to_datetime(out["Date"])
    out = out.set_index("Date").sort_index()
    out = out[list(OHLCV_COLUMNS)].apply(pd.to_numeric, errors="coerce")
    out.index.name = None
    return out


def fetch_daily_quotes(
    codes: str | Sequence[str],
    period: str = "1y",
    *,
    start: str | None = None,
    end: str | None = None,
    api_key: str | None = None,
) -> dict[str, pd.DataFrame]:
    """日足四本値（V2 ``/equities/bars/daily``）を fetch_prices と同じ形式で取得する。

    :func:`stocklib.data.fetch_prices` と同じく、入力コードをキー、
    ``Open/High/Low/Close/Volume`` 列の DataFrame（``DatetimeIndex``）を値とする辞書を返す。
    株価は分割・併合調整済み系列（``AdjC`` 等）を優先して使用する。

    Args:
        codes: 銘柄コード（4桁は内部で5桁に正規化）。単一文字列またはリスト。
        period: 取得期間（``"6mo"``, ``"1y"``, ``"2y"`` 等。``start`` 指定時は無視）。
        start: 取得開始日（``"2024-01-01"``）。指定時は ``period`` より優先。
        end: 取得終了日。``None`` で最新（Free プランは12週間遅延）。
        api_key: 明示的に渡す API キー。``None`` なら環境変数 ``JQUANTS_API_KEY`` から取得。

    Returns:
        入力コード（正規化前の文字列）をキー、OHLCV DataFrame を値とする辞書。

    Raises:
        JQuantsAuthError: API キー未設定・認証失敗の場合。
        JQuantsError: API 呼び出し失敗・空データの場合。
    """
    code_list: list[str] = [codes] if isinstance(codes, str) else list(codes)
    if start is None:
        # 営業日数 → 暦日数の概算（週5営業日 + 余裕）で from 日付を決める
        calendar_days = int(period_to_days(period) * 7 / 5) + 10
        start = (pd.Timestamp.today() - pd.Timedelta(days=calendar_days)).strftime("%Y-%m-%d")
    key = get_api_key(api_key)

    result: dict[str, pd.DataFrame] = {}
    for code in code_list:
        params: dict[str, str] = {"code": normalize_jquants_code(code), "from": start}
        if end is not None:
            params["to"] = end
        rows = _get_paginated("/equities/bars/daily", params, "data", api_key=key)
        result[code] = _quotes_to_ohlcv(rows, code)
    return result

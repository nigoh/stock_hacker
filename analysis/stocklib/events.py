"""カタリスト（決算・配当イベント）の取得と接近判定。

個別銘柄の「次の決算発表日」「配当落ち日」は、イベントドリブン分析（決算後ドリフト
PEAD、配当取り・権利落ち）における主要なカタリストである。Yahoo Finance の
``calendarEvents`` を requests で取得し（yfinance ライブラリ非依存）、直近 N 日以内に
到来するイベントをレーダーとして並べる。

**イベント日は将来予定であり変更されうる。取得日時点の予定であって確定ではない。**
発表の中身の予測でも売買助言でもない。背景は
:file:`knowledge/strategies/event-driven-japan.md` を参照。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np

from stocklib.data import DataFetchError, _seed_from_code, normalize_code

_QS_MODULES: str = "calendarEvents"


@dataclass
class CalendarEvents:
    """1銘柄のカタリスト予定日。"""

    code: str
    name: str
    earnings_date: dt.date | None
    ex_dividend_date: dt.date | None
    data: str = "real"

    def upcoming_events(self, asof: dt.date, within_days: int) -> list[tuple[str, dt.date, int]]:
        """asof から within_days 日以内に到来するイベントを (種別, 日付, 残日数) で返す。

        過去日は除外する（残日数 >= 0 のみ）。
        """
        out: list[tuple[str, dt.date, int]] = []
        for label, date in (("決算発表", self.earnings_date), ("配当落ち", self.ex_dividend_date)):
            if date is None:
                continue
            days = (date - asof).days
            if 0 <= days <= within_days:
                out.append((label, date, days))
        out.sort(key=lambda t: t[1])
        return out


def _to_date(value: object) -> dt.date | None:
    """``{"fmt": "YYYY-MM-DD"}`` / ``{"raw": epoch}`` / 文字列 から date を取り出す。"""
    if isinstance(value, dict):
        fmt = value.get("fmt")
        if isinstance(fmt, str):
            try:
                return dt.date.fromisoformat(fmt[:10])
            except ValueError:
                return None
        raw = value.get("raw")
        if isinstance(raw, (int, float)):
            return dt.datetime.utcfromtimestamp(int(raw)).date()
        return None
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def parse_calendar_events(payload: dict, code: str, name: str = "") -> CalendarEvents:
    """quoteSummary(calendarEvents) の JSON を :class:`CalendarEvents` に変換する。

    ``earnings.earningsDate`` はレンジ（2要素）で返ることがあるため最も早い日を採る。
    """
    results = (payload.get("quoteSummary") or {}).get("result") or []
    earnings_date: dt.date | None = None
    ex_div: dt.date | None = None
    if results:
        cal = results[0].get("calendarEvents") or {}
        earnings = cal.get("earnings") or {}
        raw_dates = earnings.get("earningsDate") or []
        parsed = [d for d in (_to_date(x) for x in raw_dates) if d is not None]
        if parsed:
            earnings_date = min(parsed)
        ex_div = _to_date(cal.get("exDividendDate"))
    return CalendarEvents(
        code=code, name=name, earnings_date=earnings_date, ex_dividend_date=ex_div,
    )


def _synthetic_events(code: str, name: str, asof: dt.date) -> CalendarEvents:
    """合成のカタリスト日（コード由来シードで決定論的）。実在企業の予定ではない。"""
    rng = np.random.default_rng(_seed_from_code(code) ^ 0xCA1EDA)
    earnings = asof + dt.timedelta(days=int(rng.integers(1, 90)))
    ex_div = asof + dt.timedelta(days=int(rng.integers(1, 120)))
    return CalendarEvents(
        code=code, name=name, earnings_date=earnings, ex_dividend_date=ex_div, data="synthetic",
    )


def _fetch_calendar_http(code: str, name: str) -> CalendarEvents:
    """Yahoo quoteSummary(calendarEvents) を crumb 付き requests で取得する。"""
    from stocklib import data as data_mod

    ticker = normalize_code(code)
    session, crumb = data_mod._yahoo_session_and_crumb()
    params = {"modules": _QS_MODULES, "crumb": crumb}
    for host in data_mod._YAHOO_HOSTS:
        url = f"https://{host}/v10/finance/quoteSummary/{ticker}"
        try:
            resp = session.get(url, params=params, timeout=20)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            continue
        if resp.status_code != 200:
            continue
        try:
            return parse_calendar_events(resp.json(), code, name)
        except ValueError:
            continue
    raise DataFetchError(f"{ticker} のカレンダーイベントを取得できませんでした。")


def fetch_calendar_events(code: str, name: str = "", *, synthetic: bool = False,
                          asof: dt.date | None = None) -> CalendarEvents:
    """銘柄の決算・配当落ち予定日を取得する。

    Args:
        code: 銘柄コード。
        name: 表示名（任意）。
        synthetic: True なら決定論的な合成イベント（ネットワーク不要。実在の予定ではない）。
        asof: 合成イベントの基準日（既定は今日）。

    Raises:
        DataFetchError: 実データ取得に失敗した場合（synthetic=False）。
    """
    if synthetic:
        return _synthetic_events(code, name, asof or dt.date.today())
    return _fetch_calendar_http(code, name)

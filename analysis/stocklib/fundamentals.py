"""業績（決算数値）の時系列取得と成長分析。データソース非依存の分析層。

数値系列は Yahoo Finance を正とする。取得は **標準 ``requests`` による
fundamentals-timeseries API 直叩き**（:func:`_fetch_history_http`）を第一手段とし、
失敗時のみ yfinance ライブラリ（``Ticker.income_stmt`` 等）にフォールバックする
（curl_cffi がプロキシで reset される問題を回避。詳細は :mod:`stocklib.data` の
docstring 参照）。ネットワーク不要の合成業績（``synthetic=True``）も提供する。

**設計判断（EDINET との役割分担）**: :mod:`stocklib.edinet` は「有価証券報告書等の
原文（XBRL/CSV）の取得・確認用」と位置づけ、業績の数値系列は yfinance を正とする。
EDINET の XBRL 値は会計基準（日本基準 / IFRS / 米国基準）やタクソノミの版によって
要素IDと段階損益の定義が異なり、銘柄横断で機械的に揃えるのが難しい。一方 yfinance は
（非公式ソースではあるが）銘柄横断で正規化済みの項目名を返すため時系列分析に向く。
両者の数値は会計基準差異・連結範囲・組替により一致しないことがある点に注意。

返す業績 DataFrame の形式:

- index: 会計期間の期末日（``DatetimeIndex``、昇順）
- columns: ``売上高 / 営業利益 / 純利益 / 自己資本 / 営業CF``（円建て。欠損は NaN）
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Sequence

import numpy as np
import pandas as pd

from stocklib.data import DataFetchError, _seed_from_code, normalize_code

#: 業績 DataFrame の列（この順）
HISTORY_COLUMNS: tuple[str, ...] = ("売上高", "営業利益", "純利益", "自己資本", "営業CF")

# yfinance の財務諸表 index ラベル候補（先勝ち）。バージョン・銘柄で揺れるため複数持つ。
_YF_INCOME_ROWS: dict[str, tuple[str, ...]] = {
    "売上高": ("Total Revenue", "Operating Revenue"),
    "営業利益": ("Operating Income", "Total Operating Income As Reported"),
    "純利益": ("Net Income", "Net Income Common Stockholders"),
}
_YF_BALANCE_ROWS: dict[str, tuple[str, ...]] = {
    "自己資本": ("Stockholders Equity", "Common Stock Equity"),
}
_YF_CASHFLOW_ROWS: dict[str, tuple[str, ...]] = {
    "営業CF": ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"),
}


def _synthetic_history(code: str, years: int) -> pd.DataFrame:
    """決定論的な合成業績を生成する（成長トレンド + ノイズ、シードはコード由来）。"""
    # 株価系列（synthetic_prices）とはシード系統を分ける
    rng = np.random.default_rng(_seed_from_code(code) ^ 0x0F1CE)

    base_revenue = float(rng.uniform(2e11, 5e12))  # 2,000億〜5兆円の売上規模
    growth = float(rng.uniform(0.00, 0.10))  # 年率成長トレンド
    base_op_margin = float(rng.uniform(0.05, 0.15))
    payout_retention = 0.65  # 純利益の内部留保率（自己資本の積み上げに使用）

    today = dt.date.today()
    last_fy_year = today.year if today >= dt.date(today.year, 3, 31) else today.year - 1
    index = pd.DatetimeIndex(
        [pd.Timestamp(year=y, month=3, day=31) for y in range(last_fy_year - years + 1, last_fy_year + 1)]
    )

    revenue = np.empty(years)
    op_income = np.empty(years)
    net_income = np.empty(years)
    equity = np.empty(years)
    ocf = np.empty(years)

    rev = base_revenue
    eq = base_revenue * float(rng.uniform(0.4, 0.8))
    for i in range(years):
        rev = rev * (1.0 + growth + float(rng.normal(0.0, 0.03)))
        margin = max(base_op_margin + float(rng.normal(0.0, 0.01)), 0.01)
        op = rev * margin
        net = op * float(rng.uniform(0.60, 0.75))
        eq = eq + net * payout_retention
        revenue[i] = rev
        op_income[i] = op
        net_income[i] = net
        equity[i] = eq
        ocf[i] = net * float(rng.uniform(1.1, 1.5))

    return pd.DataFrame(
        {
            "売上高": revenue,
            "営業利益": op_income,
            "純利益": net_income,
            "自己資本": equity,
            "営業CF": ocf,
        },
        index=index,
    )


# Yahoo fundamentals-timeseries の型名 → 業績列名の対応。
_TS_TYPE_MAP: dict[str, str] = {
    "売上高": "annualTotalRevenue",
    "営業利益": "annualOperatingIncome",
    "純利益": "annualNetIncome",
    "自己資本": "annualStockholdersEquity",
    "営業CF": "annualOperatingCashFlow",
}


def _parse_timeseries(payload: dict) -> pd.DataFrame | None:
    """fundamentals-timeseries の JSON を業績 DataFrame に変換する。

    各 type は ``[{asOfDate, reportedValue:{raw}}, ...]`` の配列。売上高が取れなければ
    ``None`` を返す（呼び出し側がフォールバックする）。
    """
    results = (payload.get("timeseries") or {}).get("result") or []
    inverse = {v: k for k, v in _TS_TYPE_MAP.items()}
    columns: dict[str, dict[str, float]] = {}
    for item in results:
        types = (item.get("meta") or {}).get("type") or []
        if not types:
            continue
        type_name = types[0]
        col = inverse.get(type_name)
        if col is None:
            continue
        series: dict[str, float] = {}
        for point in item.get(type_name) or []:
            if not point:
                continue
            as_of = point.get("asOfDate")
            raw = (point.get("reportedValue") or {}).get("raw")
            if as_of and raw is not None:
                series[as_of] = float(raw)
        if series:
            columns[col] = series
    if "売上高" not in columns or not columns["売上高"]:
        return None
    df = pd.DataFrame(columns)
    df.index = pd.to_datetime(df.index)
    df = df.reindex(columns=list(HISTORY_COLUMNS)).sort_index()
    return df[df["売上高"].notna()]


def _fetch_history_http(code: str, years: int) -> pd.DataFrame | None:
    """Yahoo fundamentals-timeseries を requests（crumb 付き）で取得する。

    yfinance ライブラリ（curl_cffi）に依存しないため、TLS 再終端プロキシ環境でも
    到達できる。取得失敗・空データ時は ``None`` を返す（呼び出し側がライブラリに
    フォールバックする）。yfinance の ~4期より長い期間が取れることが多い。
    """
    from stocklib import data as data_mod

    ticker = normalize_code(code)
    try:
        session, crumb = data_mod._yahoo_session_and_crumb()
    except DataFetchError:
        return None
    types = ",".join(_TS_TYPE_MAP.values())
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    period1 = now - (years + 3) * 366 * 86400  # 余裕を持って遡る
    params = {
        "symbol": ticker, "type": types,
        "period1": period1, "period2": now, "merge": "false", "crumb": crumb,
    }
    for host in data_mod._YAHOO_HOSTS:
        url = f"https://{host}/ws/fundamentals-timeseries/v1/finance/timeseries/{ticker}"
        try:
            resp = session.get(url, params=params, timeout=25)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            continue
        if resp.status_code != 200:
            continue
        try:
            df = _parse_timeseries(resp.json())
        except ValueError:
            continue
        if df is not None and not df.empty:
            return df.tail(years)
    return None


def _pick_row(df: pd.DataFrame, candidates: Sequence[str]) -> pd.Series | None:
    """yfinance の財務諸表 DataFrame から最初に見つかった行ラベルの系列を返す。"""
    if df is None or df.empty:
        return None
    for label in candidates:
        if label in df.index:
            return df.loc[label]
    return None


def _yfinance_history(code: str, years: int) -> pd.DataFrame:
    """Yahoo から年次業績を取得する（requests 直叩きを優先、失敗時にライブラリ）。

    第一手段は :func:`_fetch_history_http`（fundamentals-timeseries を requests で取得。
    プロキシ環境でも到達しやすく、期間も長い）。空・失敗時のみ yfinance ライブラリの
    財務諸表にフォールバックする。
    """
    http_df = _fetch_history_http(code, years)
    if http_df is not None and not http_df.empty:
        return http_df
    return _yfinance_history_lib(code, years)


def _yfinance_history_lib(code: str, years: int) -> pd.DataFrame:
    ticker = normalize_code(code)
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover
        raise DataFetchError(
            "yfinance がインストールされていません。`pip install yfinance` を実行するか、"
            "synthetic=True（CLI では --synthetic）で合成業績を使用してください。"
        ) from exc
    try:
        t = yf.Ticker(ticker)
        income = t.income_stmt
        balance = t.balance_sheet
        cashflow = t.cashflow
    except Exception as exc:
        raise DataFetchError(
            f"{ticker} の財務諸表の取得に失敗しました（ネットワーク・ティッカー名を確認して"
            f"ください。オフライン検証には --synthetic を使用できます）: {exc}"
        ) from exc

    columns: dict[str, pd.Series] = {}
    for name, candidates in _YF_INCOME_ROWS.items():
        series = _pick_row(income, candidates)
        if series is not None:
            columns[name] = series
    for name, candidates in _YF_BALANCE_ROWS.items():
        series = _pick_row(balance, candidates)
        if series is not None:
            columns[name] = series
    for name, candidates in _YF_CASHFLOW_ROWS.items():
        series = _pick_row(cashflow, candidates)
        if series is not None:
            columns[name] = series

    if "売上高" not in columns or columns["売上高"].dropna().empty:
        raise DataFetchError(
            f"{ticker} の業績データが空でした。yfinance が財務諸表を提供していない銘柄"
            "（REIT・指数等）か、一時的な取得失敗の可能性があります。"
            "時間をおいて再試行するか、--synthetic で手法のみ確認できます。"
        )

    df = pd.DataFrame(columns).apply(pd.to_numeric, errors="coerce")
    df = df.reindex(columns=list(HISTORY_COLUMNS))
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    df = df[df["売上高"].notna()]
    return df.tail(years)


def fetch_financial_history(
    code: str,
    years: int = 5,
    *,
    synthetic: bool = False,
) -> pd.DataFrame:
    """年次の業績時系列（売上高・営業利益・純利益・自己資本・営業CF）を取得する。

    Args:
        code: 銘柄コード（4桁数字。内部で ``.T`` 形式に正規化）。
        years: 取得する最大年数。yfinance の提供範囲（通常4〜5期程度）が上限になる。
        synthetic: True なら決定論的な合成業績（同じコードは常に同じ値）を返す。
            **合成業績は実在企業の数値ではない**。レポートに使う場合は必ず明記する。

    Returns:
        期末日 ``DatetimeIndex``（昇順）× :data:`HISTORY_COLUMNS` の ``pd.DataFrame``（円建て）。

    Raises:
        DataFetchError: yfinance からの取得失敗・データ空の場合（--synthetic への導線つき）。
    """
    if years < 1:
        raise ValueError("years は 1 以上を指定してください")
    if synthetic:
        return _synthetic_history(code, years)
    return _yfinance_history(code, years)


def _cagr(series: pd.Series) -> float:
    """年平均成長率。始点・終点が正でない、または2期未満なら NaN。"""
    s = series.dropna()
    if len(s) < 2:
        return float("nan")
    start, end = float(s.iloc[0]), float(s.iloc[-1])
    if start <= 0 or end <= 0:
        return float("nan")
    return (end / start) ** (1.0 / (len(s) - 1)) - 1.0


def _growth_streak(series: pd.Series) -> int:
    """直近期から遡って何期連続で前期比プラスかを数える。"""
    diffs = series.dropna().diff().dropna()
    streak = 0
    for value in reversed(diffs.tolist()):
        if value > 0:
            streak += 1
        else:
            break
    return streak


def analyze_growth(history: pd.DataFrame) -> dict[str, object]:
    """業績時系列から成長性・収益性の指標を計算する。

    計算内容:

    - 売上高・営業利益・純利益の CAGR（始点・終点が正でない場合は NaN）
    - 営業利益率・純利益率・ROE（純利益 ÷ 自己資本）の年次推移
    - 増収・営業増益・純増益の連続期数（直近期から遡ってカウント）

    **会社予想（ガイダンス）・コンセンサスとの比較は扱わない。** 本関数の入力は実績の
    年次時系列のみで、会社予想データを持たないため。予想と実績の関係の解釈は
    ``knowledge/fundamental/earnings-guidance-and-consensus.md`` を参照のこと。

    Args:
        history: :func:`fetch_financial_history` が返す形式の DataFrame（昇順）。

    Returns:
        以下のキーを持つ辞書:

        - ``"years"`` (int): 期数
        - ``"revenue_cagr"`` / ``"op_income_cagr"`` / ``"net_income_cagr"`` (float)
        - ``"op_margin"`` / ``"net_margin"`` / ``"roe"`` (pd.Series): 年次推移
        - ``"revenue_streak"`` / ``"op_income_streak"`` / ``"net_income_streak"`` (int):
          連続増収・増益の期数
    """
    if history.empty:
        raise ValueError("history が空です")
    missing = [c for c in HISTORY_COLUMNS if c not in history.columns]
    if missing:
        raise ValueError(f"history に必要な列がありません: {missing}")

    revenue = history["売上高"]
    op_income = history["営業利益"]
    net_income = history["純利益"]
    equity = history["自己資本"]

    op_margin = op_income / revenue
    net_margin = net_income / revenue
    roe = net_income / equity

    return {
        "years": len(history),
        "revenue_cagr": _cagr(revenue),
        "op_income_cagr": _cagr(op_income),
        "net_income_cagr": _cagr(net_income),
        "op_margin": op_margin,
        "net_margin": net_margin,
        "roe": roe,
        "revenue_streak": _growth_streak(revenue),
        "op_income_streak": _growth_streak(op_income),
        "net_income_streak": _growth_streak(net_income),
    }


def is_nan(value: object) -> bool:
    """float の NaN 判定ヘルパー（レポート整形用）。"""
    return isinstance(value, float) and math.isnan(value)

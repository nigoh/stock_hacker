"""データ取得モジュール。

Yahoo Finance からの株価・基本情報取得（4桁コード・2024年以降の英字入りコード →
``.T`` 正規化、data/cache/ への CSV キャッシュ）と、ネットワーク不要の合成 OHLCV
データ生成（GBM + ボラティリティクラスタ）を提供する。

Yahoo への取得は **標準 ``requests`` による API 直叩きを第一手段**とし（価格は
chart API、基本情報は crumb 付き quoteSummary API）、失敗時のみ yfinance
ライブラリにフォールバックする。yfinance 新版が使う curl_cffi のブラウザ偽装 TLS は、
TLS を再終端するエージェントプロキシ環境で接続 reset されることがあるため、
ライブラリに依存しない経路を優先することでリモート環境でも実データを取得できる。
"""

from __future__ import annotations

import datetime as dt
import os
import re
import threading
import time
import zlib
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

# リポジトリルート（stocklib/ → analysis/ → リポジトリルート）
REPO_ROOT: Path = Path(__file__).resolve().parents[2]
CACHE_DIR: Path = REPO_ROOT / "data" / "cache"

OHLCV_COLUMNS: tuple[str, ...] = ("Open", "High", "Low", "Close", "Volume")

_PERIOD_RE = re.compile(r"^(\d+)(d|mo|y)$")

# 価格データソースの選択。fetch_prices は既定でこの環境変数を参照する（未設定なら
# "yfinance"）。CLI の --source フラグやプログラムからの source 引数でも上書きできる。
SOURCE_ENV: str = "STOCK_HACKER_SOURCE"
VALID_SOURCES: tuple[str, ...] = ("yfinance", "jquants")


class DataFetchError(RuntimeError):
    """株価・銘柄情報の取得に失敗したことを示す例外。"""


def resolve_source(source: str | None = None) -> str:
    """価格データソース名を解決する（優先順位: 引数 > 環境変数 ``STOCK_HACKER_SOURCE`` > 既定 ``"yfinance"``）。

    Args:
        source: 明示的に指定するデータソース名（``"yfinance"`` / ``"jquants"``）。
            ``None`` の場合は環境変数、それも無ければ既定の ``"yfinance"`` を使う。

    Returns:
        小文字化・検証済みのデータソース名。

    Raises:
        ValueError: ``VALID_SOURCES`` にない値が指定された場合。
    """
    if source is None:
        source = os.environ.get(SOURCE_ENV, "").strip() or "yfinance"
    source = source.lower()
    if source not in VALID_SOURCES:
        raise ValueError(
            f"未知のデータソース: {source!r}（{', '.join(VALID_SOURCES)} のいずれかを指定してください）"
        )
    return source


def set_default_source(source: str | None) -> None:
    """CLI の ``--source`` 引数を環境変数に反映し、以降の :func:`fetch_prices` の既定ソースにする。

    ``stocklib`` 内部（``portfolio`` / ``currency`` など）から呼ばれる ``fetch_prices`` にも
    一括で効かせるため、値を ``STOCK_HACKER_SOURCE`` に書き込む。``source`` が ``None`` /
    空文字なら何もしない（既存の環境変数・既定を尊重する）。

    Raises:
        ValueError: ``source`` に未知の値が指定された場合。
    """
    if source:
        os.environ[SOURCE_ENV] = resolve_source(source)


def add_source_argument(parser: object) -> None:
    """argparse パーサに共通の ``--source`` フラグを追加する（各 CLI で使い回す）。

    パース後に :func:`set_default_source` へ ``args.source`` を渡すことで、その CLI 内の
    すべての :func:`fetch_prices` 呼び出し（``stocklib`` 内部経由を含む）に効かせられる。
    """
    parser.add_argument(  # type: ignore[attr-defined]
        "--source",
        choices=list(VALID_SOURCES),
        default=None,
        help="価格データソース（既定: yfinance。jquants は要 JQUANTS_API_KEY・日足のみ・"
        "無料プランは12週間遅延。指数/為替は yfinance に自動フォールバック）",
    )


def normalize_code(code: str) -> str:
    """銘柄コードを yfinance のティッカー形式に正規化する。

    - 4桁数字（例: ``"7203"``）→ ``"7203.T"``（東証）
    - 2024年以降に付与が始まった英字入り4文字コード（例: ``"130A"``、``"135A"``）も
      同様に ``.T`` を付与する。小文字は大文字化する（``"130a"`` → ``"130A.T"``）。
      パターンは ``jquants.normalize_jquants_code`` と同じ
      「数字・英大文字の4文字（先頭と3文字目は数字）」。
    - ``"^N225"`` などの指数、既に接尾辞付き（``"7203.T"``）、``"USDJPY=X"`` のような
      通貨ペアはそのまま返す。
    """
    code = code.strip()
    if re.fullmatch(r"[0-9][0-9A-Za-z][0-9][0-9A-Za-z]", code):
        return f"{code.upper()}.T"
    return code


def period_to_days(period: str) -> int:
    """yfinance の period 文字列を概算の営業日数に変換する。

    ``"1y"`` → 252、``"6mo"`` → 126、``"30d"`` → 30。``"max"`` は 2520（約10年）、
    ``"ytd"`` は年初からの日数とする。
    """
    period = period.strip().lower()
    if period == "max":
        return 2520
    if period == "ytd":
        today = dt.date.today()
        delta = (today - dt.date(today.year, 1, 1)).days
        return max(int(delta * 5 / 7), 21)
    m = _PERIOD_RE.match(period)
    if m is None:
        raise ValueError(f"不正な period 指定です: {period!r}（例: '30d', '6mo', '1y', '2y', 'max'）")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "d":
        return max(n, 2)
    if unit == "mo":
        return n * 21
    return n * 252  # unit == "y"


def _seed_from_code(code: str) -> int:
    """銘柄コード文字列から決定論的にシードを導出する。"""
    return zlib.crc32(normalize_code(code).encode("utf-8"))


# synthetic モードのクロス円レートの初期水準レンジ（円/基準通貨、2020年代の実勢に
# 合わせた緩い範囲）。未登録の為替ペアは _FX_DEFAULT_RANGE を使う。
_FX_SYNTHETIC_RANGES: dict[str, tuple[float, float]] = {
    "USDJPY=X": (100.0, 180.0),
    "EURJPY=X": (110.0, 190.0),
    "GBPJPY=X": (140.0, 220.0),
}
_FX_DEFAULT_RANGE: tuple[float, float] = (100.0, 180.0)


def synthetic_prices(code: str, days: int = 500, seed: int | None = None) -> pd.DataFrame:
    """合成 OHLCV データを生成する（ネットワーク不要、シード固定で再現可能）。

    日次対数リターンを GBM + GARCH(1,1) 型のボラティリティクラスタで生成する:

    $$ r_t = \\mu + \\sigma_t z_t,\\quad
       \\sigma_t^2 = \\omega + \\alpha r_{t-1}^2 + \\beta \\sigma_{t-1}^2 $$

    ``"USDJPY=X"`` / ``"EURJPY=X"`` のような為替ペア（``=X`` サフィックス）は、
    株式より現実的なパラメータ（ドリフト 0・長期ボラ年率10%・通貨ごとの現実的な
    水準レンジ ``_FX_SYNTHETIC_RANGES``、例: USDJPY 100〜180 円・EURJPY 110〜190 円・
    GBPJPY 140〜220 円程度）で決定論的に生成する（基準通貨建て換算のオフライン検証用）。

    Args:
        code: 銘柄コード（シード導出に使用。同じコードは常に同じ系列を返す）。
        days: 生成する営業日数。
        seed: 乱数シード。``None`` の場合はコードから決定論的に導出。

    Returns:
        ``Open/High/Low/Close/Volume`` 列を持つ ``pd.DataFrame``（営業日 DatetimeIndex）。
    """
    if days < 2:
        raise ValueError("days は 2 以上を指定してください")
    ticker = normalize_code(code)
    is_fx = ticker.upper().endswith("=X")
    if seed is None:
        seed = _seed_from_code(code)
    rng = np.random.default_rng(seed)

    if is_fx:
        mu = 0.0  # 為替はドリフトなし
        long_run_vol = 0.10  # 長期ボラ 年率10% 相当
    else:
        mu = 0.06 / 252.0  # 年率6%相当のドリフト
        long_run_vol = 0.20  # 長期ボラ 年率20% 相当
    # var = (vol/sqrt(252))^2, omega = var * (1 - alpha - beta)
    alpha, beta_ = 0.10, 0.85
    long_run_var = (long_run_vol / np.sqrt(252.0)) ** 2
    omega = long_run_var * (1.0 - alpha - beta_)

    var = np.empty(days)
    ret = np.empty(days)
    var[0] = long_run_var
    z = rng.standard_normal(days)
    for t in range(days):
        if t > 0:
            var[t] = omega + alpha * ret[t - 1] ** 2 + beta_ * var[t - 1]
        ret[t] = mu + np.sqrt(var[t]) * z[t]

    if is_fx:
        # 為替: 通貨ペアごとの現実的な水準レンジ内でシードから決定論的に初期値を選ぶ
        lo, hi = _FX_SYNTHETIC_RANGES.get(ticker.upper(), _FX_DEFAULT_RANGE)
        base_price = lo + float(seed % int(hi - lo))
    else:
        base_price = 300.0 + float(seed % 9000)  # コードごとに異なる価格帯
    close = base_price * np.exp(np.cumsum(ret))

    prev_close = np.concatenate([[base_price], close[:-1]])
    gap = rng.normal(0.0, 0.003, days)
    open_ = prev_close * np.exp(gap)
    intraday = np.abs(rng.normal(0.0, 0.006, days))
    high = np.maximum(open_, close) * np.exp(intraday)
    low = np.minimum(open_, close) * np.exp(-intraday)
    volume = (1e6 * np.exp(rng.normal(0.0, 0.3, days)) * (1.0 + 50.0 * np.abs(ret))).astype(np.int64)

    index = pd.date_range(end=dt.date.today(), periods=days, freq="B")
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=index,
    )


def _cache_path(ticker: str, period: str, interval: str, source: str = "yfinance") -> Path:
    safe = ticker.replace("^", "_").replace("/", "_").replace("=", "_")
    return CACHE_DIR / f"{safe}-{period}-{interval}-{source}.csv"


def _load_cache(path: Path) -> pd.DataFrame | None:
    """当日中に保存されたキャッシュのみ有効とみなして読み込む。"""
    if not path.exists():
        return None
    mtime = dt.date.fromtimestamp(path.stat().st_mtime)
    if mtime != dt.date.today():
        return None
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
    except Exception:
        return None
    if df.empty:
        return None
    return df


def _save_cache(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path)


# Yahoo Finance chart API（requests 直叩き）で使う定数。
# yfinance の新版は curl_cffi のブラウザ偽装 TLS を使うため、TLS を再終端する
# エージェントプロキシ環境では接続が reset されることがある（curl error 35）。
# 素の requests + ブラウザ UA なら chart API に到達できるため、こちらを第一手段にする。
_YAHOO_HOSTS: tuple[str, ...] = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
# User-Agent は「自ツールを名乗る」形にする。ブラウザを完全に騙る文字列は、
# ボット判定という技術的措置の回避と評価される余地があり、本リポジトリ自身が
# knowledge/data-sources/ で説く「User-Agent を明示する」作法とも矛盾する。
# Mozilla/5.0 トークンだけは、多くのサーバの UA パーサ互換のために残す。
_YAHOO_UA: str = (
    "Mozilla/5.0 (compatible; stock-hacker/1.0; "
    "+https://github.com/nigoh/stock_hacker) python-requests"
)

# --- Yahoo への負荷を抑えるスロットル -------------------------------------
# 非公式エンドポイントに対し、ユニバース一括取得（large70 なら69銘柄）が
# 無待機で連射されるのを防ぐ。0 にすれば無効化できるが推奨しない。
_YAHOO_MIN_INTERVAL: float = max(
    0.0, float(os.environ.get("STOCK_HACKER_YAHOO_MIN_INTERVAL", "0.5") or 0.5)
)
_YAHOO_LOCK = threading.Lock()
_YAHOO_LAST_CALL: float = 0.0


def yahoo_throttle() -> None:
    """Yahoo へのリクエスト間隔を :data:`_YAHOO_MIN_INTERVAL` 秒以上に保つ。"""
    global _YAHOO_LAST_CALL
    if _YAHOO_MIN_INTERVAL <= 0:
        return
    with _YAHOO_LOCK:
        wait = _YAHOO_MIN_INTERVAL - (time.monotonic() - _YAHOO_LAST_CALL)
        if wait > 0:
            time.sleep(wait)
        _YAHOO_LAST_CALL = time.monotonic()


def ensure_yahoo_allowed() -> None:
    """``STOCK_HACKER_DISABLE_YAHOO`` が設定されていれば Yahoo 経路を拒否する。

    Yahoo は非公式エンドポイントであり、自動取得の規約適合性は利用者の責任で
    判断する必要がある。厳密に運用したい利用者が自衛できるようスイッチを設ける。
    """
    flag = os.environ.get("STOCK_HACKER_DISABLE_YAHOO", "").strip()
    if flag and flag != "0":
        raise DataFetchError(
            "STOCK_HACKER_DISABLE_YAHOO により Yahoo 経路は無効化されています。"
            "--source jquants（要 JQUANTS_API_KEY）または --synthetic を使ってください。"
        )
# chart API の range= がそのまま受け付ける期間トークン。これ以外は period1/period2 に落とす。
_YAHOO_RANGES: frozenset[str] = frozenset(
    {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}
)


def _fetch_one_yahoo_http(ticker: str, period: str, interval: str) -> pd.DataFrame:
    """Yahoo Finance chart API を requests で取得し OHLCV DataFrame にする。

    yfinance ライブラリ（curl_cffi）に依存せず、標準の ``requests`` で
    ``/v8/finance/chart/<ticker>`` を叩く。``auto_adjust=True`` 相当（分割・配当調整）を
    ``adjclose`` から再現し、値が確定していない当日の進行中バー（close が null）は落とす。

    Raises:
        DataFetchError: ネットワーク失敗・非200・空データ・requests 未導入の場合。
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover
        raise DataFetchError("requests がインストールされていません。") from exc

    params: dict[str, object] = {"interval": interval, "events": "div,splits"}
    if period in _YAHOO_RANGES:
        params["range"] = period
    else:
        # 未知の期間トークンは period1/period2（エポック秒）で指定する。
        # period_to_days は「営業日」概算を返すため（1y→252, 1mo→21）、暦日オフセットに
        # 使うときは営業日→暦日（≈ ×7/5）に換算しないと取得窓が約5/7に縮む（過少取得）。
        span_days = period_to_days(period)
        cal_days = int(span_days * 7 / 5) + 10  # 営業日→暦日 + 週末/祝日の余裕
        now = int(dt.datetime.now(dt.timezone.utc).timestamp())
        params["period1"] = now - cal_days * 86400
        params["period2"] = now

    last_err: str = "unknown"
    for host in _YAHOO_HOSTS:
        url = f"https://{host}/v8/finance/chart/{ticker}"
        try:
            ensure_yahoo_allowed()
            yahoo_throttle()
            resp = requests.get(url, params=params, headers={"User-Agent": _YAHOO_UA}, timeout=20)
        except Exception as exc:  # noqa: BLE001 - ネットワーク例外は次ホストで再試行
            last_err = f"{type(exc).__name__}: {exc}"
            continue
        if resp.status_code != 200:
            last_err = f"HTTP {resp.status_code}"
            continue
        try:
            payload = resp.json()
        except ValueError as exc:
            last_err = f"JSON decode error: {exc}"
            continue
        df = _parse_yahoo_chart(payload)
        if df is not None and not df.empty:
            return df
        last_err = "空データ（result/timestamp なし）"
    raise DataFetchError(
        f"{ticker} を Yahoo chart API から取得できませんでした（{last_err}）。"
        "銘柄コード・期間指定・ネットワークを確認してください（オフライン検証は --synthetic）。"
    )


def _parse_yahoo_chart(payload: dict) -> pd.DataFrame | None:
    """chart API の JSON を OHLCV DataFrame に変換する（auto_adjust 相当）。

    値が揃わない進行中バー（close が None）は除外する。``adjclose`` があれば
    OHLC を ``adjclose/close`` 倍して分割・配当調整済み系列に揃え、Close に adjclose を使う。

    注意: ``Volume`` は無調整の生値を返す（``adjclose/close`` 倍率は分割と配当の両方を
    含むため出来高にそのまま適用できない——配当落ちは出来高をスケールしないため）。
    分割をまたぐ期間では価格は調整済み・出来高は未調整となる点に留意すること。
    """
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        return None
    res = result[0]
    timestamps = res.get("timestamp")
    quote_list = (res.get("indicators") or {}).get("quote") or []
    if not timestamps or not quote_list:
        return None
    quote = quote_list[0]
    opens, highs = quote.get("open", []), quote.get("high", [])
    lows, closes, vols = quote.get("low", []), quote.get("close", []), quote.get("volume", [])
    adj_list = (res.get("indicators") or {}).get("adjclose") or [{}]
    adjcloses = adj_list[0].get("adjclose", []) if adj_list else []
    gmtoffset = int((res.get("meta") or {}).get("gmtoffset", 0) or 0)

    rows: list[tuple[pd.Timestamp, float, float, float, float, float]] = []
    for i, ts in enumerate(timestamps):
        close = _at(closes, i)
        if close is None:  # 進行中の当日バー等は確定値がないので落とす
            continue
        o, h, l = _at(opens, i), _at(highs, i), _at(lows, i)
        if None in (o, h, l):
            continue
        adj = _at(adjcloses, i)
        factor = (adj / close) if (adj is not None and close) else 1.0
        vol = _at(vols, i)
        date = pd.Timestamp(dt.datetime.utcfromtimestamp(int(ts) + gmtoffset).date())
        rows.append((
            date, o * factor, h * factor, l * factor,
            adj if adj is not None else close, float(vol) if vol is not None else 0.0,
        ))
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["_date", *OHLCV_COLUMNS]).set_index("_date")
    df.index.name = None
    return df[~df.index.duplicated(keep="last")].sort_index()


def _at(seq: list, i: int) -> float | None:
    """リストの i 番目を float で返す（範囲外・None は None）。"""
    if seq is None or i >= len(seq):
        return None
    v = seq[i]
    return None if v is None else float(v)


def _fetch_one_yahoo_lib(ticker: str, period: str, interval: str) -> pd.DataFrame:
    """yfinance ライブラリ経由の取得（requests 直叩きが失敗したときのフォールバック）。"""
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover
        raise DataFetchError(
            "yfinance がインストールされていません。`pip install yfinance` を実行するか、"
            "--synthetic フラグで合成データを使用してください。"
        ) from exc
    try:
        df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
    except Exception as exc:
        raise DataFetchError(
            f"{ticker} の取得に失敗しました（ネットワーク・ティッカー名を確認してください。"
            f"オフライン検証には --synthetic を使用できます）: {exc}"
        ) from exc
    if df is None or df.empty:
        raise DataFetchError(
            f"{ticker} のデータが空でした。銘柄コード（4桁数字 or '^N225' 等）と期間指定を確認してください。"
        )
    df = df[[c for c in OHLCV_COLUMNS if c in df.columns]].copy()
    if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


def _fetch_one_yfinance(ticker: str, period: str, interval: str) -> pd.DataFrame:
    """Yahoo Finance から OHLCV を取得する（requests 直叩きを優先、失敗時にライブラリ）。

    第一手段は :func:`_fetch_one_yahoo_http`（標準 ``requests`` による chart API 直叩き。
    TLS 再終端プロキシ環境でも到達しやすい）。それが失敗した場合のみ
    :func:`_fetch_one_yahoo_lib`（yfinance ライブラリ）にフォールバックする。
    """
    try:
        return _fetch_one_yahoo_http(ticker, period, interval)
    except DataFetchError as http_err:
        try:
            return _fetch_one_yahoo_lib(ticker, period, interval)
        except DataFetchError:
            raise http_err


def _fetch_one_jquants(code: str, ticker: str, period: str, interval: str) -> pd.DataFrame:
    """J-Quants（JPX 公式）から日足 OHLCV を取得する。

    指数（``^N225``）・為替（``USDJPY=X``）など J-Quants が扱わないコードは、
    ベンチマークや為替換算をそのまま使えるよう **yfinance にフォールバック**する。
    株価は分割・併合調整済み系列を優先する（配当落ち調整は含まない。詳細は
    ``knowledge/data-sources/data-apis-and-tools.md`` の J-Quants 節を参照）。
    """
    from stocklib import jquants  # 循環 import 回避のため遅延 import

    try:
        jquants.normalize_jquants_code(code)
    except ValueError:
        # J-Quants 非対応コード（指数・通貨ペア等）は yfinance で取得する
        return _fetch_one_yfinance(ticker, period, interval)
    if interval != "1d":
        raise DataFetchError(
            "J-Quants ソースは日足（interval='1d'）のみ対応です。"
            "分足・週足が必要な場合は yfinance ソース（--source yfinance）を使用してください。"
        )
    return jquants.fetch_daily_quotes(code, period=period)[code]


def fetch_prices(
    codes: str | Sequence[str],
    period: str = "1y",
    interval: str = "1d",
    *,
    synthetic: bool = False,
    use_cache: bool = True,
    source: str | None = None,
) -> dict[str, pd.DataFrame]:
    """1つ以上の銘柄の OHLCV 株価を取得する。

    Args:
        codes: 銘柄コード（4桁数字は内部で ``.T`` を付与）。単一文字列またはリスト。
        period: 取得期間（yfinance 形式: ``"6mo"``, ``"1y"``, ``"2y"``, ``"max"`` 等）。
        interval: 足の間隔（通常 ``"1d"``）。
        synthetic: True なら価格ソースを使わず、シード固定の合成データを返す
            （``source`` より優先）。
        use_cache: True なら ``data/cache/`` の当日キャッシュを利用・更新する。
        source: 価格データソース（``"yfinance"`` / ``"jquants"``）。``None`` の場合は
            環境変数 ``STOCK_HACKER_SOURCE``、それも無ければ ``"yfinance"``。
            ``"jquants"`` は日足のみ対応で、指数・為替コードは yfinance にフォールバックする。

    Returns:
        入力コード（正規化前の文字列）をキー、OHLCV DataFrame を値とする辞書。

    Raises:
        DataFetchError: 取得失敗または空データの場合。
        ValueError: ``source`` に未知の値が指定された場合。
    """
    resolved_source = resolve_source(source)
    code_list: list[str] = [codes] if isinstance(codes, str) else list(codes)
    result: dict[str, pd.DataFrame] = {}
    for code in code_list:
        ticker = normalize_code(code)
        if synthetic:
            result[code] = synthetic_prices(ticker, days=period_to_days(period))
            continue
        cache = _cache_path(ticker, period, interval, resolved_source)
        df = _load_cache(cache) if use_cache else None
        if df is None:
            if resolved_source == "jquants":
                df = _fetch_one_jquants(code, ticker, period, interval)
            else:
                df = _fetch_one_yfinance(ticker, period, interval)
            if use_cache:
                _save_cache(cache, df)
        result[code] = df
    return result


_INFO_KEYS: dict[str, str] = {
    "longName": "名称",
    "sector": "セクター",
    "industry": "業種",
    "marketCap": "時価総額",
    "trailingPE": "PER（実績）",
    "forwardPE": "PER（予想）",
    "priceToBook": "PBR",
    "dividendYield": "配当利回り",
    "returnOnEquity": "ROE",
    "beta": "ベータ",
    "fiftyTwoWeekHigh": "52週高値",
    "fiftyTwoWeekLow": "52週安値",
}


def fetch_info(code: str, *, synthetic: bool = False) -> dict[str, object]:
    """銘柄の基本情報（PER・PBR・時価総額など）を取得する。

    yfinance の ``Ticker.info`` から取得できる範囲の指標を、日本語キーの辞書で返す。
    ``synthetic=True`` の場合はコードから決定論的に導出したダミー値を返す。
    取得できない項目は含まれない（欠損時も例外にはしない）。
    """
    ticker = normalize_code(code)
    if synthetic:
        rng = np.random.default_rng(_seed_from_code(code))
        return {
            "名称": f"合成データ銘柄 {ticker}",
            "セクター": "Synthetic",
            "時価総額": int(rng.uniform(1e11, 5e13)),
            "PER（実績）": round(float(rng.uniform(8, 40)), 2),
            "PBR": round(float(rng.uniform(0.5, 5)), 2),
            "配当利回り": round(float(rng.uniform(0.0, 0.04)), 4),
        }
    try:
        raw = _fetch_info_http(ticker)
    except DataFetchError:
        try:
            import yfinance as yf

            raw = yf.Ticker(ticker).info or {}
        except Exception as exc:
            raise DataFetchError(
                f"{ticker} の基本情報取得に失敗しました（--synthetic でダミー値を利用できます）: {exc}"
            ) from exc
    info: dict[str, object] = {}
    for key, label in _INFO_KEYS.items():
        value = raw.get(key)
        if value is not None:
            info[label] = value
    # 配当利回りの単位を「比率」に統一する。Yahoo chart/quoteSummary の raw は比率
    # （0.034=3.4%）だが、yfinance ライブラリ（フォールバック経路）は版により
    # 百分率（3.4）を返すことがある。取得経路で単位が揺れると screen.py 等の下流
    # （× 100 して%表示）が桁違いになるため、1 超なら百分率とみなして比率へ正規化する
    # （現実の配当利回りが 100% を超えることはなく、閾値 1 で安全に判別できる）。
    dy = info.get("配当利回り")
    if isinstance(dy, (int, float)) and not isinstance(dy, bool) and dy > 1.0:
        info["配当利回り"] = float(dy) / 100.0
    return info


# Yahoo quoteSummary（ファンダ指標）を requests で取得するための crumb 付きセッション。
# crumb はプロセス内で使い回す（多銘柄取得時の往復を減らす）。
_YAHOO_SESSION: object | None = None
_YAHOO_CRUMB: str | None = None
# quoteSummary で要求するモジュール。
_QS_MODULES: str = "price,summaryDetail,defaultKeyStatistics,financialData,assetProfile"


def _yahoo_session_and_crumb() -> tuple[object, str]:
    """crumb 付きの requests セッションを（キャッシュしつつ）返す。

    Yahoo の quoteSummary v10 は cookie + crumb を要求する。``fc.yahoo.com`` で
    cookie を得てから ``/v1/test/getcrumb`` で crumb を取得する。

    Raises:
        DataFetchError: requests 未導入、または crumb を取得できなかった場合。
    """
    global _YAHOO_SESSION, _YAHOO_CRUMB
    if _YAHOO_SESSION is not None and _YAHOO_CRUMB:
        return _YAHOO_SESSION, _YAHOO_CRUMB
    try:
        import requests
    except ImportError as exc:  # pragma: no cover
        raise DataFetchError("requests がインストールされていません。") from exc
    session = requests.Session()
    session.headers.update({"User-Agent": _YAHOO_UA})
    try:
        ensure_yahoo_allowed()
        yahoo_throttle()
        session.get("https://fc.yahoo.com", timeout=15)  # cookie 取得（404 でも Set-Cookie は返る）
    except Exception:  # noqa: BLE001 - cookie 取得失敗でも crumb 取得を試みる
        pass
    for host in _YAHOO_HOSTS:
        try:
            resp = session.get(f"https://{host}/v1/test/getcrumb", timeout=15)
        except Exception:  # noqa: BLE001
            continue
        crumb = resp.text.strip()
        if resp.status_code == 200 and crumb and "<" not in crumb:
            _YAHOO_SESSION, _YAHOO_CRUMB = session, crumb
            return session, crumb
    raise DataFetchError("Yahoo crumb を取得できませんでした。")


def _fetch_info_http(ticker: str) -> dict[str, object]:
    """Yahoo quoteSummary を requests で取得し、yfinance ``.info`` 互換の辞書に整形する。

    Raises:
        DataFetchError: 取得失敗・空データの場合。
    """
    session, crumb = _yahoo_session_and_crumb()
    params = {"modules": _QS_MODULES, "crumb": crumb}
    for host in _YAHOO_HOSTS:
        try:
            resp = session.get(  # type: ignore[attr-defined]
                f"https://{host}/v10/finance/quoteSummary/{ticker}", params=params, timeout=20
            )
        except Exception:  # noqa: BLE001
            continue
        if resp.status_code != 200:
            continue
        try:
            results = (resp.json().get("quoteSummary") or {}).get("result") or []
        except ValueError:
            continue
        if results:
            return _extract_quote_summary(results[0])
    raise DataFetchError(f"{ticker} の quoteSummary を取得できませんでした。")


def _qs_raw(module: dict, key: str) -> object | None:
    """quoteSummary モジュールの ``{raw, fmt}`` 形式の値から raw を取り出す。"""
    val = module.get(key)
    if isinstance(val, dict):
        return val.get("raw")
    return val


def _extract_quote_summary(result: dict) -> dict[str, object]:
    """quoteSummary の1銘柄結果を yfinance ``.info`` 互換のフラット辞書に変換する。"""
    price = result.get("price") or {}
    detail = result.get("summaryDetail") or {}
    key_stats = result.get("defaultKeyStatistics") or {}
    financial = result.get("financialData") or {}
    profile = result.get("assetProfile") or {}

    raw: dict[str, object] = {}
    raw["longName"] = price.get("longName") or price.get("shortName")
    raw["sector"] = profile.get("sector")
    raw["industry"] = profile.get("industry")
    raw["marketCap"] = _qs_raw(price, "marketCap")
    for key in ("trailingPE", "forwardPE", "dividendYield", "beta",
                "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "priceToBook"):
        raw[key] = _qs_raw(detail, key)
    # summaryDetail に無い指標は他モジュールで補完する。
    if raw.get("priceToBook") is None:
        raw["priceToBook"] = _qs_raw(key_stats, "priceToBook")
    if raw.get("forwardPE") is None:
        raw["forwardPE"] = _qs_raw(key_stats, "forwardPE")
    if raw.get("beta") is None:
        raw["beta"] = _qs_raw(key_stats, "beta")
    raw["returnOnEquity"] = _qs_raw(financial, "returnOnEquity")
    return {k: v for k, v in raw.items() if v is not None}

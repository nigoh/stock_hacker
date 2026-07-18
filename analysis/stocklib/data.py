"""データ取得モジュール。

yfinance による株価取得（4桁コード・2024年以降の英字入りコード → ``.T`` 正規化、
data/cache/ への CSV キャッシュ）と、
ネットワーク不要の合成 OHLCV データ生成（GBM + ボラティリティクラスタ）を提供する。
"""

from __future__ import annotations

import datetime as dt
import os
import re
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
        help="価格データソース（既定: yfinance。jquants は要 JQUANTS_REFRESH_TOKEN・日足のみ・"
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


def _fetch_one_yfinance(ticker: str, period: str, interval: str) -> pd.DataFrame:
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
    return info

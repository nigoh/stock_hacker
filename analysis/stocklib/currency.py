"""通貨換算モジュール（円建て → 基準通貨建て、海外投資家視点）。

円建て価格系列をクロス円レート（``USDJPY=X`` / ``EURJPY=X`` / ``GBPJPY=X``、
1基準通貨あたり円）の同日終値で除して基準通貨建てに換算する。
リターンの恒等式は基準通貨 $B$ によらず同型で
$(1 + r^{B}) = (1 + r^{JPY}) / (1 + r^{FX})$、対数リターンでは
$\\log(1 + r^{B}) = \\log(1 + r^{JPY}) - \\log(1 + r^{FX})$（$r^{FX}$: クロス円レートの変化率）。

歴史的経緯から ``to_usd*`` / ``fetch_usdjpy`` の旧名も後方互換エイリアスとして残す
（実体は基準通貨を問わない一般形 ``to_base*`` / ``fetch_fx``）。
"""

from __future__ import annotations

import pandas as pd

from stocklib.data import fetch_prices

# 対応する基準通貨 → yfinance のクロス円ティッカー（1基準通貨あたり円）のホワイトリスト。
# 通貨を追加する場合はここに "<通貨コード>": "<通貨コード>JPY=X" を登録し、
# CURRENCY_LABELS に日本語表示名を追加する（synthetic モードの水準レンジは
# stocklib.data の _FX_SYNTHETIC_RANGES を参照）。
SUPPORTED_CURRENCIES: dict[str, str] = {
    "USD": "USDJPY=X",
    "EUR": "EURJPY=X",
    "GBP": "GBPJPY=X",
}

# 基準通貨コード → レポート表示用の日本語名（「ドル建て」「円/ユーロ」等の組み立てに使う）
CURRENCY_LABELS: dict[str, str] = {
    "USD": "ドル",
    "EUR": "ユーロ",
    "GBP": "ポンド",
}

# ドル円の yfinance ティッカー（後方互換のため残置。一般形は get_fx_ticker("USD")）
FX_TICKER: str = SUPPORTED_CURRENCIES["USD"]

# 換算対象の価格列（Volume は株数のため換算しない）
_PRICE_COLUMNS: tuple[str, ...] = ("Open", "High", "Low", "Close")


def get_fx_ticker(ccy: str) -> str:
    """基準通貨コードから yfinance のクロス円ティッカーを返す。

    対応通貨（:data:`SUPPORTED_CURRENCIES` のホワイトリスト、2026年時点で
    USD/EUR/GBP）以外は ``ValueError`` を送出する。大文字小文字は区別しない。
    """
    key = ccy.strip().upper()
    if key not in SUPPORTED_CURRENCIES:
        raise ValueError(
            f"未対応の基準通貨です: {ccy!r}（対応: {', '.join(SUPPORTED_CURRENCIES)}）。"
            "追加するには stocklib/currency.py の SUPPORTED_CURRENCIES に "
            "'<通貨コード>': '<通貨コード>JPY=X'（yfinance のクロス円ティッカー）を登録し、"
            "CURRENCY_LABELS に表示名を追加してください。"
        )
    return SUPPORTED_CURRENCIES[key]


def currency_label(ccy: str) -> str:
    """基準通貨コードの日本語表示名（「ドル」「ユーロ」「ポンド」）を返す。

    未対応通貨は :func:`get_fx_ticker` と同じ ``ValueError`` を送出する。
    """
    get_fx_ticker(ccy)  # ホワイトリスト検証（未対応なら ValueError）
    return CURRENCY_LABELS[ccy.strip().upper()]


def fetch_fx(
    ccy: str = "USD", period: str = "1y", *, synthetic: bool = False
) -> pd.DataFrame:
    """基準通貨のクロス円レート（例: ``EURJPY=X``）の OHLCV DataFrame を取得する。

    ``synthetic=True`` の場合は ``stocklib.data.synthetic_prices`` の為替モード
    （ドリフト 0・年率ボラ約10%・通貨ごとに現実的な水準レンジ、シード固定で
    決定論的）を使う。未対応通貨は :func:`get_fx_ticker` が ``ValueError`` を送出する。
    """
    ticker = get_fx_ticker(ccy)
    return fetch_prices(ticker, period=period, synthetic=synthetic)[ticker]


def fetch_usdjpy(period: str = "1y", *, synthetic: bool = False) -> pd.DataFrame:
    """USDJPY=X の OHLCV DataFrame を取得する（``fetch_fx("USD", ...)`` の後方互換ラッパー）。"""
    return fetch_fx("USD", period, synthetic=synthetic)


def align_fx(index: pd.DatetimeIndex, fx_close: pd.Series) -> pd.Series:
    """為替終値系列を株価のインデックスに合わせて返す。

    為替市場の休場等で欠損する日は、為替カレンダー上の直近の値で前方補完し、
    系列先頭の欠損のみ後方補完する。有効な為替値が1つも無い場合は
    ``ValueError`` を送出する。
    """
    union = index.union(fx_close.index)
    aligned = fx_close.reindex(union).ffill().reindex(index).bfill()
    if aligned.isna().any():
        raise ValueError("為替系列と株価系列に共通の日付が無く、基準通貨建てに換算できません。")
    return aligned


def to_base_series(prices_jpy: pd.Series, fx_close: pd.Series) -> pd.Series:
    """円建て価格系列を基準通貨建てに換算する（同日のクロス円終値で除す）。

    ``fx_close`` は「1基準通貨あたり円」の終値系列（:func:`fetch_fx` の ``Close`` 列等）。
    """
    fx = align_fx(prices_jpy.index, fx_close)
    return prices_jpy / fx


def to_base_returns(returns_jpy: pd.Series, fx_close: pd.Series) -> pd.Series:
    """円建て日次リターン系列を基準通貨建て日次リターン系列に換算する。

    恒等式 $(1 + r^{B}_t) = (1 + r^{JPY}_t) / (1 + r^{FX}_t)$ を各日に適用する
    （$r^{FX}_t$: クロス円終値の同区間変化率。基準通貨 $B$ は USD/EUR/GBP いずれでも
    同型）。系列の初日は換算の基準日として為替リターン 0 とみなす（初日の為替水準を
    基準に以降の変化のみを反映）。同日終値換算・為替ヘッジなしの近似であり、
    日中の為替変動は無視する。

    円建てエクイティカーブ $E^{JPY}_t = \\prod_s (1 + r^{JPY}_s)$ に対し、
    返り値の累積積は $E^{JPY}_t \\cdot FX_0 / FX_t$（初日基準で正規化した
    基準通貨建てエクイティカーブ）に厳密に一致する。

    Args:
        returns_jpy: 円建て日次リターン系列（戦略リターン・ポートフォリオ
            リターン等、円建てで複利計算可能な任意のリターン系列）。
        fx_close: クロス円レートの終値系列（:func:`fetch_fx` の ``Close`` 列等、
            1基準通貨あたり円）。欠損日は :func:`align_fx` の規則で補完する。
    """
    fx = align_fx(pd.DatetimeIndex(returns_jpy.index), fx_close)
    r_fx = fx.pct_change().fillna(0.0)
    return (1.0 + returns_jpy) / (1.0 + r_fx) - 1.0


def to_base_currency(df: pd.DataFrame, fx_df: pd.DataFrame) -> pd.DataFrame:
    """円建て OHLCV DataFrame を基準通貨建てに換算した新しい DataFrame を返す。

    Open/High/Low/Close を同日のクロス円終値で除す（日中の為替変動は無視する近似）。
    Volume（株数）はそのまま維持する。為替が欠損する日は :func:`align_fx` の規則で補完する。

    Args:
        df: 円建て OHLCV DataFrame（``fetch_prices`` の返す形式）。
        fx_df: クロス円レートの OHLCV DataFrame（``Close`` 列を使用。
            :func:`fetch_fx` の返り値等、1基準通貨あたり円）。
    """
    fx = align_fx(df.index, fx_df["Close"])
    out = df.copy()
    for col in _PRICE_COLUMNS:
        if col in out.columns:
            out[col] = out[col] / fx
    return out


# --- 後方互換エイリアス（旧 USD 固定 API。実体は基準通貨を問わない一般形） ---
to_usd_series = to_base_series
to_usd_returns = to_base_returns
to_usd = to_base_currency

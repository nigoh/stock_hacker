"""ADRパリティ計算モジュール（東証現地株 × 米国ADR × ドル円）。

ADR の理論価格は為替を介したパリティ
$P_{ADR} = P_{\\text{東証}} \\times n / S_{USD/JPY}$（$n$: 1ADRあたり現地株数）で決まる。
本モジュールは対応表 ``analysis/universe/adr_map.csv`` の読み込み（:func:`load_adr_map`）と、
東証終値・ADR終値・ドル円終値からの理論ADR価格・乖離率・円換算ADR価格の計算
（:func:`compute_parity`）、価格取得込みの評価（:func:`evaluate_mapping`）を提供する。
制度背景は ``knowledge/market-structure/foreign-investor-access-channels.md`` の ADR 節を参照。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from stocklib.currency import fetch_usdjpy
from stocklib.data import REPO_ROOT, fetch_prices

# 対応表 CSV の既定パス（code,adr_ticker,ratio,listing。``#`` 行はコメント）
ADR_MAP_PATH: Path = REPO_ROOT / "analysis" / "universe" / "adr_map.csv"

_REQUIRED_COLUMNS: frozenset[str] = frozenset({"code", "adr_ticker", "ratio", "listing"})


@dataclass(frozen=True)
class AdrMapping:
    """東証現地株と米国ADRの対応1件。

    Attributes:
        code: 東証の銘柄コード（4桁文字列、例: ``"7203"``）。
        adr_ticker: 米国ADRのティッカー（例: ``"TM"``。yfinance にそのまま渡せる）。
        ratio: ADR比率 $n$ = 1ADRあたりの現地株数（例: トヨタは 10 株 = 1ADR）。
        listing: 上場区分（``"NYSE"`` = スポンサード、``"OTC"`` = 店頭）。
    """

    code: str
    adr_ticker: str
    ratio: float
    listing: str


@dataclass(frozen=True)
class ParityResult:
    """パリティ計算の結果（入力値と導出値のセット）。

    Attributes:
        tse_close: 東証終値（円）。
        adr_close: ADR終値（ドル）。
        usdjpy_close: ドル円終値（1ドルあたり円）。
        theoretical_adr_usd: 理論ADR価格（ドル）= 東証終値 × ratio ÷ ドル円。
        premium_pct: 乖離率（比率、0.01 = +1%）= ADR終値 ÷ 理論ADR価格 − 1。
            正なら「ADRが東証終値換算より高い」（NY時間に理論値が切り上がった状態）。
        adr_implied_jpy: 円換算ADR価格（円/現地株1株）= ADR終値 × ドル円 ÷ ratio。
            東証の翌営業日の寄り付き水準の目安になる。
    """

    tse_close: float
    adr_close: float
    usdjpy_close: float
    theoretical_adr_usd: float
    premium_pct: float
    adr_implied_jpy: float


def load_adr_map(path: Path | None = None) -> list[AdrMapping]:
    """ADR対応表 CSV を読み込み、:class:`AdrMapping` のリストを返す。

    Args:
        path: CSV パス。``None`` なら既定の :data:`ADR_MAP_PATH`
            （``analysis/universe/adr_map.csv``）。

    Raises:
        ValueError: 必須列（code,adr_ticker,ratio,listing）の欠落、
            または ratio が正の数でない行がある場合。
    """
    csv_path = ADR_MAP_PATH if path is None else path
    df = pd.read_csv(csv_path, comment="#", dtype={"code": str, "adr_ticker": str, "listing": str})
    if not _REQUIRED_COLUMNS.issubset(df.columns):
        raise ValueError(
            f"ADR対応表 CSV には {sorted(_REQUIRED_COLUMNS)} 列が必要です: {csv_path}"
        )
    mappings: list[AdrMapping] = []
    for row in df.itertuples(index=False):
        ratio = float(row.ratio)
        if not ratio > 0:
            raise ValueError(f"ADR比率は正の数である必要があります: {row.code} → {row.ratio!r}")
        mappings.append(
            AdrMapping(
                code=str(row.code).strip(),
                adr_ticker=str(row.adr_ticker).strip(),
                ratio=ratio,
                listing=str(row.listing).strip(),
            )
        )
    if not mappings:
        raise ValueError(f"ADR対応表 CSV にデータ行がありません: {csv_path}")
    return mappings


def compute_parity(
    tse_close: float, adr_close: float, usdjpy_close: float, ratio: float
) -> ParityResult:
    """東証終値・ADR終値・ドル円終値からADRパリティを計算する（純粋関数）。

    $$P^{理論}_{ADR} = \\frac{P_{\\text{東証}} \\times n}{S_{USD/JPY}},\\quad
      \\text{乖離} = \\frac{P_{ADR}}{P^{理論}_{ADR}} - 1,\\quad
      P^{円換算}_{ADR} = \\frac{P_{ADR} \\times S_{USD/JPY}}{n}$$

    Args:
        tse_close: 東証終値（円）。
        adr_close: ADR終値（ドル）。
        usdjpy_close: ドル円終値（1ドルあたり円）。
        ratio: ADR比率 $n$（1ADRあたり現地株数、正の数）。

    Returns:
        :class:`ParityResult`（理論ADR価格・乖離率・円換算ADR価格を含む）。

    Raises:
        ValueError: いずれかの入力が正の数でない場合。
    """
    for name, value in (
        ("東証終値", tse_close),
        ("ADR終値", adr_close),
        ("ドル円終値", usdjpy_close),
        ("ADR比率", ratio),
    ):
        if not value > 0:
            raise ValueError(f"{name} は正の数である必要があります: {value!r}")
    theoretical = tse_close * ratio / usdjpy_close
    premium = adr_close / theoretical - 1.0
    implied_jpy = adr_close * usdjpy_close / ratio
    return ParityResult(
        tse_close=float(tse_close),
        adr_close=float(adr_close),
        usdjpy_close=float(usdjpy_close),
        theoretical_adr_usd=float(theoretical),
        premium_pct=float(premium),
        adr_implied_jpy=float(implied_jpy),
    )


def _last_close(df: pd.DataFrame) -> tuple[float, dt.date]:
    """OHLCV DataFrame の最終終値とその日付を返す。"""
    close = df["Close"].dropna()
    if close.empty:
        raise ValueError("終値系列が空です")
    ts = close.index[-1]
    return float(close.iloc[-1]), pd.Timestamp(ts).date()


def evaluate_mapping(
    mapping: AdrMapping,
    period: str = "1mo",
    *,
    synthetic: bool = False,
    fx_df: pd.DataFrame | None = None,
) -> tuple[ParityResult, dt.date, dt.date, dt.date]:
    """対応1件について価格を取得し、直近終値ベースのパリティを評価する。

    価格取得は ``stocklib.data.fetch_prices`` を再利用する（東証コードは ``.T`` に
    正規化され、``"TM"`` のような ADR ティッカーはそのまま yfinance に渡る）。
    東証・NY・為替の「直近終値」は時差により同一暦日とは限らない点に注意
    （返り値の日付で確認できる）。

    Args:
        mapping: 評価する対応（:func:`load_adr_map` の要素）。
        period: 取得期間（yfinance 形式）。直近終値のみ使うので短くてよい。
        synthetic: True なら合成データ（ネットワーク不要、ロジック検証用）。
        fx_df: ドル円 OHLCV を外から渡す場合に指定（複数銘柄評価時の再取得回避）。
            ``None`` なら :func:`stocklib.currency.fetch_usdjpy` で取得する。

    Returns:
        ``(ParityResult, 東証終値の日付, ADR終値の日付, ドル円終値の日付)``。
    """
    prices = fetch_prices([mapping.code, mapping.adr_ticker], period=period, synthetic=synthetic)
    if fx_df is None:
        fx_df = fetch_usdjpy(period, synthetic=synthetic)
    tse_close, tse_date = _last_close(prices[mapping.code])
    adr_close, adr_date = _last_close(prices[mapping.adr_ticker])
    fx_close, fx_date = _last_close(fx_df)
    result = compute_parity(tse_close, adr_close, fx_close, mapping.ratio)
    return result, tse_date, adr_date, fx_date

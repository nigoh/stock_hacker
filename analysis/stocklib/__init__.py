"""stocklib — 日本株分析のための軽量ライブラリ。

データ取得（yfinance / 合成データ）、テクニカル指標、リスク・リターン指標、
ベクトル化バックテスト、Markdown レポート生成を提供する。

サブモジュール ``stocklib.jquants`` に J-Quants API（JPX総研）クライアントがある。
環境変数 ``JQUANTS_REFRESH_TOKEN`` を設定すると、``fetch_listed_info()`` で
全上場銘柄のユニバース構築、``fetch_daily_quotes()`` で ``fetch_prices`` 互換の
日足 OHLCV 取得ができる（Free プランは12週間遅延、2025年時点）。
本パッケージの必須依存（pandas/numpy/yfinance）を増やさないよう、ここでは
eager import せず、``stocklib.jquants`` への属性アクセス時に遅延 import する。
詳細は ``knowledge/data-sources/data-apis-and-tools.md`` の J-Quants 節を参照。
"""

from importlib import import_module
from types import ModuleType

from stocklib.data import (
    DataFetchError,
    fetch_info,
    fetch_prices,
    normalize_code,
    synthetic_prices,
)
from stocklib.indicators import atr, bollinger, ema, ichimoku, macd, rsi, sma
from stocklib.metrics import (
    ann_return,
    ann_vol,
    beta,
    correlation_matrix,
    daily_returns,
    max_drawdown,
    sharpe,
    sortino,
    var_historical,
)
from stocklib.backtest import BacktestResult, ma_cross_signal, run_backtest
from stocklib.report import DISCLAIMER, markdown_table, save_report

__version__ = "0.1.0"


def __getattr__(name: str) -> ModuleType:
    """``stocklib.jquants`` を遅延 import で解決する（PEP 562）。

    ``import stocklib; stocklib.jquants.fetch_listed_info()`` のような属性アクセスを、
    パッケージ import 時のコスト・依存を増やさずに成立させる。
    """
    if name == "jquants":
        return import_module("stocklib.jquants")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "jquants",
    "DataFetchError",
    "fetch_prices",
    "fetch_info",
    "normalize_code",
    "synthetic_prices",
    "sma",
    "ema",
    "rsi",
    "macd",
    "bollinger",
    "ichimoku",
    "atr",
    "daily_returns",
    "ann_return",
    "ann_vol",
    "sharpe",
    "sortino",
    "max_drawdown",
    "beta",
    "var_historical",
    "correlation_matrix",
    "BacktestResult",
    "ma_cross_signal",
    "run_backtest",
    "DISCLAIMER",
    "markdown_table",
    "save_report",
    "__version__",
]

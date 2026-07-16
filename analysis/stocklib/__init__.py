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

サブモジュール ``stocklib.charts`` にチャート画像生成（matplotlib / Agg）がある。
import コストを抑えるため同様に遅延 import とし、matplotlib 未導入環境でも
``charts.charts_available()`` で利用可否を判定できる。

決算・開示分析にはサブモジュール ``stocklib.fundamentals``（業績時系列と成長分析。
数値は yfinance を正とする）と ``stocklib.edinet``（EDINET API v2 クライアント。
環境変数 ``EDINET_API_KEY`` が必要、有価証券報告書等の原文取得・確認用）がある。
いずれも同様に遅延 import で解決する。

サブモジュール ``stocklib.journal`` にリサーチジャーナル（分析仮説の記録と
事後検証。``journal/`` 配下の frontmatter 付き Markdown を読み書きし、
記録時スナップショットとベンチマーク調整後リターンで hit/miss/mixed を判定）が
ある。CLI は ``analysis/research_journal.py``。同様に遅延 import で解決する。
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
from stocklib.signals import Signal, detect_signals
from stocklib.portfolio import (
    PortfolioReview,
    PortfolioValidationError,
    Position,
    PositionValuation,
    evaluate_portfolio,
    load_portfolio,
)

__version__ = "0.1.0"


def __getattr__(name: str) -> ModuleType:
    """``stocklib.jquants`` / ``stocklib.charts`` を遅延 import で解決する（PEP 562）。

    ``import stocklib; stocklib.jquants.fetch_listed_info()`` のような属性アクセスを、
    パッケージ import 時のコスト・依存を増やさずに成立させる。
    """
    if name in ("jquants", "charts", "edinet", "fundamentals", "journal"):
        return import_module(f"stocklib.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "jquants",
    "charts",
    "edinet",
    "fundamentals",
    "journal",
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
    "Signal",
    "detect_signals",
    "Position",
    "PositionValuation",
    "PortfolioReview",
    "PortfolioValidationError",
    "load_portfolio",
    "evaluate_portfolio",
    "DISCLAIMER",
    "markdown_table",
    "save_report",
    "__version__",
]

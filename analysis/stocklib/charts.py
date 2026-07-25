"""チャート画像生成モジュール（matplotlib / Agg バックエンド）。

reports/img/ 配下に PNG を出力する。ヘッドレス環境で動作するよう
matplotlib は Agg バックエンドを明示的に使用する。matplotlib 未導入の
環境でも import 自体は成功し、:func:`charts_available` で利用可否を
判定できる（CLI 側は警告を出してチャートなしで続行する）。

日本語フォントの豆腐化（グリフ欠落）を避けるため、軸ラベル・凡例は
英数字表記とし、銘柄の識別はタイトル中のコードで行う。
ローソク足は日本の慣習に合わせ、陽線=赤系・陰線=青系で描画する。
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from stocklib import indicators
from stocklib.data import REPO_ROOT
from stocklib.safepath import contained_path, safe_name

try:  # matplotlib はオプション依存（未導入でも stocklib 本体は動く）
    import matplotlib

    matplotlib.use("Agg")  # ヘッドレス環境用（GUI 不要）
    import matplotlib.pyplot as plt
except ImportError:  # pragma: no cover - matplotlib 導入済み環境では通らない
    plt = None  # type: ignore[assignment]

# チャート画像の出力先（reports/* は .gitignore 済みのため img/ も管理外）
IMG_DIR: Path = REPO_ROOT / "reports" / "img"

DPI: int = 110  # ファイルサイズ抑制のため控えめに固定

# 日本の慣習: 陽線（Close >= Open）= 赤系、陰線 = 青系
UP_COLOR: str = "#d64541"
DOWN_COLOR: str = "#2a6fdb"

# SMA・比較線グラフ用の固定順カテゴリカル配色（ローソクの赤/青と衝突しにくい色から）
LINE_COLORS: tuple[str, ...] = (
    "#e8a33d",  # orange
    "#7b52ab",  # purple
    "#3f8f5f",  # green
    "#2a6fdb",  # blue
    "#d64541",  # red
    "#6b7280",  # gray
)


def charts_available() -> bool:
    """matplotlib が利用可能かどうかを返す。

    False の場合、各 plot 関数は :class:`RuntimeError` を送出する。
    CLI 側はこの関数で判定し、警告を出してチャートなしで続行すること。
    """
    return plt is not None


def _require_matplotlib() -> None:
    if plt is None:
        raise RuntimeError(
            "matplotlib がインストールされていないためチャートを生成できません。"
            "`pip install matplotlib` を実行するか、--no-charts で無効化してください。"
        )


def img_path(filename: str) -> Path:
    """``reports/img/`` 配下のチャート出力パスを組み立てて返す（絶対パス）。

    **チャートのファイル名にはユーザー入力が混ざる**（``analyze_stock.py`` の
    ``--code`` から作られる ``img_stem`` がそのまま連結される）。``IMG_DIR / name``
    と素朴に join すると ``--code ../../..`` や ``--code /tmp/x`` で
    ``reports/img/`` の外に PNG を書けてしまうため、
    :func:`stocklib.safepath.contained_path` でディレクトリ成分を捨てて封じ込める。
    **チャートを出力する CLI は ``IMG_DIR / ...`` ではなく必ずこの関数を使うこと。**

    Raises:
        ValueError: ファイル名が空・``.``・``..``・先頭ドット・NUL 文字を含む場合。
    """
    return contained_path(
        IMG_DIR, filename, what="チャートファイル名", where="reports/img/"
    )


def save_figure(fig: "plt.Figure", out_path: Path | str) -> Path:
    """Figure を PNG として保存し、絶対パスを返す（親ディレクトリは自動作成）。

    出力先ディレクトリは呼び出し側の指定に従う（テストは tmp_path を渡す）が、
    ``..`` による上位ディレクトリ参照と不正なファイル名は拒否する——
    ``IMG_DIR / f"{img_stem}-price.png"`` のように**ユーザー入力由来の文字列を
    連結したパス**が渡る経路があり、``..`` が混ざると意図した出力先の外に
    書き込まれるため（:mod:`stocklib.safepath` の説明を参照）。
    ``reports/img/`` への出力は :func:`img_path` で組み立てること。
    """
    path = Path(out_path)
    if ".." in path.parts:
        raise ValueError(f"チャート出力先に上位ディレクトリ参照は使えません: {out_path!r}")
    safe_name(path.name, what="チャートファイル名")
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    return path


def _style_axes(ax: "plt.Axes") -> None:
    """グリッドを控えめにし、データより目立たない軸装飾に統一する。"""
    ax.grid(True, alpha=0.3, linewidth=0.5)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)


def _draw_candles(ax: "plt.Axes", df: pd.DataFrame) -> None:
    """ローソク足を自前描画する（ヒゲ=vlines、実体=bar、陽線=赤系/陰線=青系）。"""
    up = (df["Close"] >= df["Open"]).to_numpy()
    colors = np.where(up, UP_COLOR, DOWN_COLOR)
    ax.vlines(df.index, df["Low"], df["High"], color=colors, linewidth=0.7)
    bottoms = np.minimum(df["Open"].to_numpy(), df["Close"].to_numpy())
    heights = np.abs(df["Close"].to_numpy() - df["Open"].to_numpy())
    ax.bar(df.index, heights, bottom=bottoms, width=0.8, color=colors, linewidth=0)


def plot_price_chart(
    df: pd.DataFrame,
    code: str,
    out_path: Path | str,
    sma_windows: Sequence[int] = (25, 75),
    with_bollinger: bool = True,
) -> Path:
    """価格チャート（ローソク足 + SMA + ボリンジャーバンド / 出来高 / RSI）を PNG 出力する。

    上段: ローソク足（陽線=赤系・陰線=青系）+ SMA + ボリンジャーバンド(20, 2σ)、
    中段: 出来高（陽線/陰線と同色）、下段: RSI(14)（30/70 の目安線付き）。
    指標計算は :mod:`stocklib.indicators` を再利用する。

    Args:
        df: ``Open/High/Low/Close/Volume`` 列を持つ OHLCV DataFrame。
        code: 銘柄コード（タイトル表示に使用）。
        out_path: 出力 PNG パス。
        sma_windows: 重ねる SMA の期間（データ不足の期間は自動スキップ）。
        with_bollinger: True ならボリンジャーバンド(20, 2σ)を重ねる。

    Returns:
        保存した PNG の絶対パス。

    Raises:
        RuntimeError: matplotlib が利用できない場合。
    """
    _require_matplotlib()
    close = df["Close"]

    fig, (ax_price, ax_vol, ax_rsi) = plt.subplots(
        3,
        1,
        figsize=(10, 8),
        sharex=True,
        gridspec_kw={"height_ratios": [3, 1, 1]},
        constrained_layout=True,
    )

    # --- 上段: ローソク足 + SMA + ボリンジャーバンド ---
    _draw_candles(ax_price, df)
    if with_bollinger:
        bb = indicators.bollinger(close)
        ax_price.fill_between(
            df.index, bb["lower"], bb["upper"], color="#6b7280", alpha=0.12, linewidth=0
        )
        ax_price.plot(df.index, bb["upper"], color="#6b7280", linewidth=0.7, label="BB(20,2s)")
        ax_price.plot(df.index, bb["lower"], color="#6b7280", linewidth=0.7)
    for i, window in enumerate(sma_windows):
        line = indicators.sma(close, window)
        if line.notna().any():
            color = LINE_COLORS[i % len(LINE_COLORS)]
            ax_price.plot(df.index, line, color=color, linewidth=1.4, label=f"SMA{window}")
    ax_price.set_ylabel("Price")
    ax_price.set_title(f"{code} Price / Volume / RSI")
    ax_price.legend(loc="upper left", fontsize=8, frameon=False)
    _style_axes(ax_price)

    # --- 中段: 出来高 ---
    up = (df["Close"] >= df["Open"]).to_numpy()
    vol_colors = np.where(up, UP_COLOR, DOWN_COLOR)
    ax_vol.bar(df.index, df["Volume"], width=0.8, color=vol_colors, alpha=0.6, linewidth=0)
    ax_vol.set_ylabel("Volume")
    _style_axes(ax_vol)

    # --- 下段: RSI(14) ---
    rsi14 = indicators.rsi(close, 14)
    ax_rsi.plot(df.index, rsi14, color="#374151", linewidth=1.2)
    ax_rsi.axhline(70, color=UP_COLOR, linewidth=0.8, linestyle="--", alpha=0.6)
    ax_rsi.axhline(30, color=DOWN_COLOR, linewidth=0.8, linestyle="--", alpha=0.6)
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_ylabel("RSI(14)")
    ax_rsi.set_xlabel("Date")
    _style_axes(ax_rsi)

    return save_figure(fig, out_path)


def plot_relative_performance(
    dfs: Mapping[str, pd.DataFrame],
    out_path: Path | str,
    title: str | None = None,
) -> Path:
    """期首=100 の相対パフォーマンス比較線グラフを PNG 出力する（compare 用）。

    共通の取引日（全銘柄で終値が揃う日）に絞ってから期首=100 に正規化する。

    Args:
        dfs: 銘柄コードをキー、``Close`` 列を持つ DataFrame を値とする辞書。
        out_path: 出力 PNG パス。
        title: チャートタイトル（豆腐化回避のため英数字推奨）。``None`` なら
            銘柄コードから自動生成する。

    Returns:
        保存した PNG の絶対パス。

    Raises:
        RuntimeError: matplotlib が利用できない場合。
        ValueError: 共通の取引日が存在しない場合。
    """
    _require_matplotlib()
    closes = pd.concat({code: df["Close"] for code, df in dfs.items()}, axis=1).dropna()
    if closes.empty:
        raise ValueError("共通の取引日が存在せず、相対パフォーマンスを描画できません。")
    normalized = closes / closes.iloc[0] * 100.0

    fig, ax = plt.subplots(figsize=(10, 5), constrained_layout=True)
    for i, code in enumerate(normalized.columns):
        color = LINE_COLORS[i % len(LINE_COLORS)]
        ax.plot(normalized.index, normalized[code], color=color, linewidth=1.6, label=str(code))
    ax.axhline(100.0, color="#6b7280", linewidth=0.8, linestyle="--", alpha=0.6)
    ax.set_ylabel("Relative Performance (start = 100)")
    ax.set_xlabel("Date")
    if title is None:
        title = f"Relative Performance: {' / '.join(str(c) for c in normalized.columns)}"
    ax.set_title(title)
    ax.legend(loc="upper left", fontsize=9, frameon=False)
    _style_axes(ax)

    return save_figure(fig, out_path)


def plot_drawdown(
    df: pd.DataFrame | pd.Series,
    out_path: Path | str,
    title: str = "Cumulative Return & Drawdown",
) -> Path:
    """累積リターン（期首=1.0）とドローダウンの2段チャートを PNG 出力する。

    上段: 期首=1.0 に正規化した累積価値（エクイティカーブ）、
    下段: 過去最高値からの下落率 $\\mathrm{DD}_t = V_t / \\max_{s \\le t} V_s - 1$。

    Args:
        df: ``Close`` 列を持つ DataFrame、または価格・エクイティカーブの Series
            （例: :class:`stocklib.backtest.BacktestResult` の ``equity_curve``）。
        out_path: 出力 PNG パス。
        title: チャートタイトル（豆腐化回避のため英数字推奨。コードを含めるとよい）。

    Returns:
        保存した PNG の絶対パス。

    Raises:
        RuntimeError: matplotlib が利用できない場合。
    """
    _require_matplotlib()
    series = (df["Close"] if isinstance(df, pd.DataFrame) else df).dropna()
    if series.empty:
        raise ValueError("系列が空のためドローダウンを描画できません。")
    cumulative = series / series.iloc[0]
    drawdown = cumulative / cumulative.cummax() - 1.0

    fig, (ax_cum, ax_dd) = plt.subplots(
        2,
        1,
        figsize=(10, 6),
        sharex=True,
        gridspec_kw={"height_ratios": [2, 1]},
        constrained_layout=True,
    )
    ax_cum.plot(cumulative.index, cumulative, color="#2a6fdb", linewidth=1.6)
    ax_cum.axhline(1.0, color="#6b7280", linewidth=0.8, linestyle="--", alpha=0.6)
    ax_cum.set_ylabel("Growth of 1.0")
    ax_cum.set_title(title)
    _style_axes(ax_cum)

    ax_dd.fill_between(drawdown.index, drawdown, 0.0, color=UP_COLOR, alpha=0.35, linewidth=0)
    ax_dd.plot(drawdown.index, drawdown, color=UP_COLOR, linewidth=0.9)
    ax_dd.set_ylabel("Drawdown")
    ax_dd.set_xlabel("Date")
    _style_axes(ax_dd)

    return save_figure(fig, out_path)

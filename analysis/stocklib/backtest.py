"""ベクトル化バックテストモジュール。

ポジション（0/1 の ``pd.Series``）を受け取り、翌日執行・取引コスト込みで
戦略リターンを計算する。結果は :class:`BacktestResult` に集約する。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from stocklib import metrics
from stocklib.indicators import bollinger, macd, rsi, sma


@dataclass
class BacktestResult:
    """バックテスト結果の統計サマリー。

    Attributes:
        total_return: 期間トータルリターン（コスト控除後）。
        ann_return: 年率リターン（幾何平均ベース）。
        ann_vol: 年率ボラティリティ。
        sharpe: シャープレシオ（無リスク金利 0 と仮定）。
        max_drawdown: 戦略エクイティカーブの最大ドローダウン（負値）。
        n_trades: 新規建て（エントリー）の回数。
        win_rate: 1トレード（エントリー〜手仕舞い）単位の勝率。トレードが無い場合 NaN。
        t_stat: 日次戦略リターン平均のt統計量 $t = \\bar r / (s/\\sqrt{n})$。
        t_stat_interpretation: t統計量の日本語解釈。
        n_days: 対象営業日数。
        equity_curve: 戦略のエクイティカーブ（初期値 1.0）。
    """

    total_return: float
    ann_return: float
    ann_vol: float
    sharpe: float
    max_drawdown: float
    n_trades: int
    win_rate: float
    t_stat: float
    t_stat_interpretation: str
    n_days: int
    equity_curve: pd.Series = field(repr=False)


def _interpret_t_stat(t: float, n: int) -> str:
    if not np.isfinite(t):
        return "計算不能（データ不足またはリターン変動なし）"
    at = abs(t)
    if at >= 2.58:
        sig = "1%水準で統計的に有意"
    elif at >= 1.96:
        sig = "5%水準で統計的に有意"
    elif at >= 1.645:
        sig = "10%水準で弱く有意"
    else:
        sig = "統計的に有意ではない（偶然と区別できない）"
    direction = "プラス" if t > 0 else "マイナス"
    return f"t={t:.2f}（n={n}）: 平均日次リターンは{direction}方向で{sig}"


def run_backtest(
    prices: pd.Series,
    positions: pd.Series,
    cost_bps: float = 0.0,
) -> BacktestResult:
    """0/1 ポジション系列に基づくベクトル化バックテストを実行する。

    シグナルは当日終値で判定し、**翌営業日から** ポジションに反映する
    （``positions.shift(1)`` で執行、先読みバイアスを回避）。
    取引コストはポジション変化量に対して ``cost_bps``（1bp = 0.01%）を片道課金する:

    $$ r^{strat}_t = w_{t-1} \\, r_t - |w_{t-1} - w_{t-2}| \\cdot \\frac{cost}{10^4} $$

    Args:
        prices: 終値系列。
        positions: 同じインデックスの 0/1 ポジション系列（1=買い持ち、0=ノーポジ）。
        cost_bps: 片道取引コスト（ベーシスポイント）。

    Returns:
        :class:`BacktestResult`
    """
    prices = prices.dropna()
    positions = positions.reindex(prices.index).fillna(0.0).astype(float)
    if not positions.isin([0.0, 1.0]).all():
        raise ValueError("positions は 0/1 の系列である必要があります")

    ret = prices / prices.shift(1) - 1.0
    exec_pos = positions.shift(1).fillna(0.0)  # 翌日執行
    turnover = exec_pos.diff().abs().fillna(exec_pos.abs())
    cost = turnover * (cost_bps / 1e4)
    strat_ret = (exec_pos * ret).fillna(0.0) - cost

    equity = (1.0 + strat_ret).cumprod()
    total_return = float(equity.iloc[-1] - 1.0) if len(equity) else float("nan")

    active = strat_ret[exec_pos > 0]
    n = len(active)
    if n >= 2 and float(active.std()) > 0:
        t_stat = float(active.mean() / (active.std() / np.sqrt(n)))
    else:
        t_stat = float("nan")

    entries = ((exec_pos == 1.0) & (exec_pos.shift(1).fillna(0.0) == 0.0))
    n_trades = int(entries.sum())

    # トレード単位の損益: 保有区間ごとに複利リターンを計算
    trade_id = entries.cumsum()
    held = exec_pos == 1.0
    win_rate = float("nan")
    if n_trades > 0:
        trade_rets = (
            (1.0 + strat_ret[held]).groupby(trade_id[held]).prod() - 1.0
        )
        win_rate = float((trade_rets > 0).mean())

    return BacktestResult(
        total_return=total_return,
        ann_return=metrics.ann_return(strat_ret),
        ann_vol=metrics.ann_vol(strat_ret),
        sharpe=metrics.sharpe(strat_ret),
        max_drawdown=metrics.max_drawdown(equity),
        n_trades=n_trades,
        win_rate=win_rate,
        t_stat=t_stat,
        t_stat_interpretation=_interpret_t_stat(t_stat, n),
        n_days=len(prices),
        equity_curve=equity,
    )


@dataclass
class DCAResult:
    """積立（ドルコスト平均法）シミュレーションの結果。

    Attributes:
        monthly_amount: 毎月の買付金額（円）。
        day_of_month: 目標買付日（暦日。非営業日・月に存在しない日は翌営業日へ繰越）。
        cost_bps: 片道取引コスト（ベーシスポイント）。買付金額から控除してから株数換算する。
        n_buys: 実際に約定した買付回数。
        total_invested: 累計投資額（円、コスト込みの拠出総額）。
        total_shares: 最終保有株数（端株許容、金額ベース買付）。
        avg_cost: 平均取得単価（円、コスト込み）$= \\text{累計投資額} / \\text{保有株数}$。
        avg_buy_price: 買付日終値の単純平均（円）。定額買付では安値で多く買うため、
            コストが小さければ ``avg_cost``（調和平均に相当）はこれを下回る。
        final_value: 最終評価額（円）。
        total_return: トータル損益率 $= \\text{最終評価額}/\\text{累計投資額} - 1$。
        min_pnl: 期間中の最低損益率（最悪時点の評価損益率）。
        max_drawdown_pp: 損益率曲線のピークからの最大下落幅（パーセントポイント、負値）。
            追加拠出が損益率を希薄化する効果を含むため保守的な値になる点に注意。
        buy_prices: 買付日終値の系列（インデックス=約定日）。
        invested_curve: 累計投資額の時系列（円）。
        value_curve: 評価額の時系列（円）。
        pnl_curve: 損益率の時系列（初回買付前は NaN）。
        avg_cost_curve: 平均取得単価の時系列（円、初回買付前は NaN）。
    """

    monthly_amount: float
    day_of_month: int
    cost_bps: float
    n_buys: int
    total_invested: float
    total_shares: float
    avg_cost: float
    avg_buy_price: float
    final_value: float
    total_return: float
    min_pnl: float
    max_drawdown_pp: float
    buy_prices: pd.Series = field(repr=False)
    invested_curve: pd.Series = field(repr=False)
    value_curve: pd.Series = field(repr=False)
    pnl_curve: pd.Series = field(repr=False)
    avg_cost_curve: pd.Series = field(repr=False)


@dataclass
class LumpSumResult:
    """期初一括投資シミュレーションの結果（積立との比較用）。

    Attributes:
        amount: 投資額（円、コスト込みの拠出総額）。
        cost_bps: 片道取引コスト（ベーシスポイント）。
        total_shares: 保有株数（端株許容）。
        avg_cost: 取得単価（円、コスト込み）。
        final_value: 最終評価額（円）。
        total_return: トータル損益率。
        min_pnl: 期間中の最低損益率。
        max_drawdown_pp: 損益率曲線のピークからの最大下落幅（パーセントポイント、負値）。
            :class:`DCAResult` と同一定義で、積立との比較を同じ物差しで行うためのもの。
        invested_curve: 累計投資額の時系列（円、期初から一定）。
        value_curve: 評価額の時系列（円）。
        pnl_curve: 損益率の時系列。
    """

    amount: float
    cost_bps: float
    total_shares: float
    avg_cost: float
    final_value: float
    total_return: float
    min_pnl: float
    max_drawdown_pp: float
    invested_curve: pd.Series = field(repr=False)
    value_curve: pd.Series = field(repr=False)
    pnl_curve: pd.Series = field(repr=False)


@dataclass
class DCAComparison:
    """積立と期初一括の比較結果。

    ``lump_sum`` は積立の累計投資額と同額を**期初の終値で一括投資**した場合。
    「期初に全額を用意できた」という後知恵の前提を置いた比較である点に注意
    （毎月の収入から拠出する現実の積立投資家には選べないことが多い）。
    """

    dca: DCAResult
    lump_sum: LumpSumResult


def _to_close(df: pd.DataFrame | pd.Series) -> pd.Series:
    """OHLCV DataFrame または終値 Series から終値系列を取り出す（NaN 除去済み）。"""
    close = df["Close"] if isinstance(df, pd.DataFrame) else df
    close = close.dropna()
    if close.empty:
        raise ValueError("価格系列が空です")
    return close


def _pnl_drawdown_pp(pnl: pd.Series) -> float:
    """損益率曲線のピークからの最大下落幅（パーセントポイント、負値）を計算する。

    $$ \\mathrm{DD} = \\min_t \\left( \\mathrm{pnl}_t - \\max_{s \\le t} \\mathrm{pnl}_s \\right) $$
    """
    pnl = pnl.dropna()
    if len(pnl) == 0:
        return float("nan")
    return float((pnl - pnl.cummax()).min())


def dca_schedule(index: pd.DatetimeIndex, day_of_month: int = 1) -> pd.DatetimeIndex:
    """毎月の目標買付日を営業日インデックス上の約定日に割り当てる。

    各暦月について ``day_of_month`` 日（月にその日が無ければ月末日に丸める）を目標日とし、
    目標日が非営業日（``index`` に無い日）なら **翌営業日に繰越** する。繰越の結果が
    翌月にまたがることも許容する（例: 12月末の目標日が年末休場で1月頭に約定）。
    系列開始前の目標日は系列初日に繰り越される。系列終了後にしか約定できない月はスキップする。

    Args:
        index: 営業日の ``DatetimeIndex``（昇順）。
        day_of_month: 目標買付日（1〜31）。

    Returns:
        約定日の ``DatetimeIndex``（重複なし・昇順）。
    """
    if not 1 <= day_of_month <= 31:
        raise ValueError(f"day_of_month ({day_of_month}) は 1〜31 で指定してください")
    if len(index) == 0:
        return pd.DatetimeIndex([])
    dates: list[pd.Timestamp] = []
    period = index[0].to_period("M")
    last_period = index[-1].to_period("M")
    while period <= last_period:
        target = pd.Timestamp(
            year=period.year, month=period.month,
            day=min(day_of_month, period.days_in_month),
        )
        pos = int(index.searchsorted(target))
        if pos < len(index):
            exec_date = index[pos]
            if not dates or exec_date != dates[-1]:
                dates.append(exec_date)
        period += 1
    return pd.DatetimeIndex(dates)


def dca_backtest(
    df: pd.DataFrame | pd.Series,
    monthly_amount: float,
    day_of_month: int = 1,
    cost_bps: float = 0.0,
) -> DCAResult:
    """毎月定額買付（ドルコスト平均法）のシミュレーションを実行する。

    毎月 ``day_of_month`` 日（非営業日なら翌営業日に繰越）の終値で ``monthly_amount`` 円を
    買い付ける。端株（小数株数）を許容する金額ベースの買付で、取得株数は

    $$ \\Delta q_t = \\frac{A \\cdot (1 - \\mathrm{cost}/10^4)}{P_t} $$

    （$A$: 月額、$P_t$: 約定日終値）。累計投資額はコスト込みの拠出額 $A$ で数える
    ため、平均取得単価 ``avg_cost`` は取引コストを含む。

    Args:
        df: OHLCV DataFrame（``Close`` 列を使用）または終値 Series。
        monthly_amount: 毎月の買付金額（円、正値）。
        day_of_month: 目標買付日（1〜31。月に存在しない日は月末日に丸める）。
        cost_bps: 片道取引コスト（ベーシスポイント）。

    Returns:
        :class:`DCAResult`
    """
    if monthly_amount <= 0:
        raise ValueError(f"monthly_amount ({monthly_amount}) は正の値で指定してください")
    if cost_bps < 0:
        raise ValueError(f"cost_bps ({cost_bps}) は 0 以上で指定してください")
    close = _to_close(df)
    buy_dates = dca_schedule(close.index, day_of_month)
    if len(buy_dates) == 0:
        raise ValueError("買付可能な営業日がありません（期間が短すぎます）")

    cash_flow = pd.Series(0.0, index=close.index)
    cash_flow.loc[buy_dates] = monthly_amount
    net_factor = 1.0 - cost_bps / 1e4
    shares_bought = cash_flow * net_factor / close
    shares_cum = shares_bought.cumsum()
    invested_curve = cash_flow.cumsum()
    value_curve = shares_cum * close
    with np.errstate(invalid="ignore", divide="ignore"):
        pnl_curve = (value_curve / invested_curve - 1.0).where(invested_curve > 0)
        avg_cost_curve = (invested_curve / shares_cum).where(shares_cum > 0)

    buy_prices = close.loc[buy_dates]
    total_invested = float(invested_curve.iloc[-1])
    total_shares = float(shares_cum.iloc[-1])
    final_value = float(value_curve.iloc[-1])
    return DCAResult(
        monthly_amount=monthly_amount,
        day_of_month=day_of_month,
        cost_bps=cost_bps,
        n_buys=len(buy_dates),
        total_invested=total_invested,
        total_shares=total_shares,
        avg_cost=total_invested / total_shares,
        avg_buy_price=float(buy_prices.mean()),
        final_value=final_value,
        total_return=final_value / total_invested - 1.0,
        min_pnl=float(pnl_curve.dropna().min()),
        max_drawdown_pp=_pnl_drawdown_pp(pnl_curve),
        buy_prices=buy_prices,
        invested_curve=invested_curve,
        value_curve=value_curve,
        pnl_curve=pnl_curve,
        avg_cost_curve=avg_cost_curve,
    )


def lump_sum_backtest(
    df: pd.DataFrame | pd.Series,
    amount: float,
    cost_bps: float = 0.0,
) -> LumpSumResult:
    """期初一括投資のシミュレーションを実行する（積立との比較用）。

    系列初日の終値で ``amount`` 円（コスト込み）を全額投資し、以後保有し続ける。
    端株を許容する金額ベースの買付（:func:`dca_backtest` と同じ約定・コストの扱い）。

    Args:
        df: OHLCV DataFrame（``Close`` 列を使用）または終値 Series。
        amount: 投資額（円、正値）。
        cost_bps: 片道取引コスト（ベーシスポイント）。

    Returns:
        :class:`LumpSumResult`
    """
    if amount <= 0:
        raise ValueError(f"amount ({amount}) は正の値で指定してください")
    if cost_bps < 0:
        raise ValueError(f"cost_bps ({cost_bps}) は 0 以上で指定してください")
    close = _to_close(df)
    net_factor = 1.0 - cost_bps / 1e4
    shares = amount * net_factor / float(close.iloc[0])
    invested_curve = pd.Series(amount, index=close.index)
    value_curve = shares * close
    pnl_curve = value_curve / amount - 1.0
    final_value = float(value_curve.iloc[-1])
    return LumpSumResult(
        amount=amount,
        cost_bps=cost_bps,
        total_shares=shares,
        avg_cost=amount / shares,
        final_value=final_value,
        total_return=final_value / amount - 1.0,
        min_pnl=float(pnl_curve.min()),
        max_drawdown_pp=_pnl_drawdown_pp(pnl_curve),
        invested_curve=invested_curve,
        value_curve=value_curve,
        pnl_curve=pnl_curve,
    )


def compare_dca_lump_sum(
    df: pd.DataFrame | pd.Series,
    monthly_amount: float,
    day_of_month: int = 1,
    cost_bps: float = 0.0,
) -> DCAComparison:
    """積立と期初一括を同一総投資額で比較する。

    まず :func:`dca_backtest` を実行し、その累計投資額と**同額**を期初の終値で
    一括投資した :func:`lump_sum_backtest` と並べて返す。一括側は「期初に全額を
    用意できた」という後知恵の前提を置いた参照値であることに注意。

    Args:
        df: OHLCV DataFrame（``Close`` 列を使用）または終値 Series。
        monthly_amount: 毎月の買付金額（円、正値）。
        day_of_month: 目標買付日（1〜31）。
        cost_bps: 片道取引コスト（ベーシスポイント、両手法共通）。

    Returns:
        :class:`DCAComparison`
    """
    dca = dca_backtest(
        df, monthly_amount, day_of_month=day_of_month, cost_bps=cost_bps
    )
    lump = lump_sum_backtest(df, dca.total_invested, cost_bps=cost_bps)
    return DCAComparison(dca=dca, lump_sum=lump)


def ma_cross_signal(prices: pd.Series, fast: int = 25, slow: int = 75) -> pd.Series:
    """移動平均クロス戦略のポジション系列（0/1）を生成する。

    短期 SMA が長期 SMA を上回っている間はロング（1）、それ以外はノーポジ（0）:

    $$ w_t = \\mathbb{1}\\{\\mathrm{SMA}_t(fast) > \\mathrm{SMA}_t(slow)\\} $$

    執行タイミングのシフトは :func:`run_backtest` 側で行うため、ここでは当日判定の値を返す。
    """
    if fast >= slow:
        raise ValueError(f"fast ({fast}) は slow ({slow}) より小さくしてください")
    signal = (sma(prices, fast) > sma(prices, slow)).astype(float)
    signal.name = f"ma_cross_{fast}_{slow}"
    return signal


def rsi_reversal_signal(
    prices: pd.Series,
    window: int = 14,
    lower: float = 30.0,
    upper: float = 50.0,
) -> pd.Series:
    """RSI 逆張り戦略のポジション系列（0/1）を生成する。

    RSI が ``lower`` を下回ったら「売られすぎ」としてロング（1）でエントリーし、
    RSI が ``upper`` を上回るまで保有を継続、上回ったら手仕舞い（0）とする:

    $$ w_t = \\begin{cases}
        1 & (\\mathrm{RSI}_t < \\text{lower}) \\\\
        0 & (\\mathrm{RSI}_t > \\text{upper}) \\\\
        w_{t-1} & (\\text{それ以外、直前状態を維持})
    \\end{cases} $$

    執行タイミングのシフトは :func:`run_backtest` 側で行うため、ここでは当日判定の値を返す。

    Args:
        prices: 終値系列。
        window: RSI の計算期間（Wilder 平滑化）。
        lower: エントリー閾値（この値未満で買い）。
        upper: イグジット閾値（この値超で手仕舞い）。``lower < upper`` が必須。

    Returns:
        0/1 のポジション系列（``prices`` と同じインデックス）。
    """
    if window < 2:
        raise ValueError(f"window ({window}) は 2 以上にしてください")
    if not (0.0 < lower < upper < 100.0):
        raise ValueError(
            f"閾値は 0 < lower ({lower}) < upper ({upper}) < 100 を満たす必要があります"
        )
    r = rsi(prices, window)
    raw = pd.Series(np.nan, index=prices.index, dtype=float)
    raw[r < lower] = 1.0
    raw[r > upper] = 0.0
    signal = raw.ffill().fillna(0.0)
    signal.name = f"rsi_reversal_{window}_{lower:g}_{upper:g}"
    return signal


def macd_signal(
    prices: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.Series:
    """MACD トレンドフォロー戦略のポジション系列（0/1）を生成する。

    MACD ライン（$\\mathrm{EMA}_{fast} - \\mathrm{EMA}_{slow}$）がシグナル線
    （MACD の $\\mathrm{EMA}_{signal}$）を上回っている間はロング（1）、それ以外は 0:

    $$ w_t = \\mathbb{1}\\{\\mathrm{MACD}_t > \\mathrm{Signal}_t\\} $$

    執行タイミングのシフトは :func:`run_backtest` 側で行うため、当日判定の値を返す。
    """
    if not (0 < fast < slow):
        raise ValueError(f"0 < fast ({fast}) < slow ({slow}) を満たしてください")
    if signal < 1:
        raise ValueError(f"signal ({signal}) は 1 以上にしてください")
    lines = macd(prices, fast, slow, signal)
    pos = (lines["macd"] > lines["signal"]).astype(float)
    pos.name = f"macd_{fast}_{slow}_{signal}"
    return pos


def bollinger_reversal_signal(
    prices: pd.Series, window: int = 20, num_std: float = 2.0
) -> pd.Series:
    """ボリンジャーバンド逆張り（平均回帰）戦略のポジション系列（0/1）を生成する。

    終値が下限バンド（$-k\\sigma$）を割り込んだら「売られすぎ」としてロング（1）、
    中心線（SMA）へ回帰したら手仕舞い（0）。それ以外は直前状態を維持する:

    $$ w_t = \\begin{cases}
        1 & (C_t < \\text{lower}_t) \\\\
        0 & (C_t > \\text{middle}_t) \\\\
        w_{t-1} & (\\text{それ以外})
    \\end{cases} $$

    執行タイミングのシフトは :func:`run_backtest` 側で行うため、当日判定の値を返す。

    Args:
        prices: 終値系列。
        window: 移動平均・標準偏差の期間。
        num_std: バンド幅の標準偏差倍率（$k$）。
    """
    if window < 2:
        raise ValueError(f"window ({window}) は 2 以上にしてください")
    if num_std <= 0:
        raise ValueError(f"num_std ({num_std}) は正の値にしてください")
    band = bollinger(prices, window, num_std)
    raw = pd.Series(np.nan, index=prices.index, dtype=float)
    raw[prices < band["lower"]] = 1.0
    raw[prices > band["middle"]] = 0.0
    pos = raw.ffill().fillna(0.0)
    pos.name = f"bollinger_reversal_{window}_{num_std:g}"
    return pos


def split_series(prices: pd.Series, ratio: float = 0.7) -> tuple[pd.Series, pd.Series]:
    """価格系列を時間順にインサンプル（IS）/ アウトオブサンプル（OOS）へ分割する。

    先頭から ``ratio`` の割合を IS、残りを OOS とする。時間順序を保った分割であり、
    ランダム分割は行わない（系列相関を壊すため）。パラメータ調整は IS のみで行い、
    OOS は最終確定した戦略に対して一度だけ評価するのが原則。

    Args:
        prices: 終値系列（NaN は除去される）。
        ratio: IS の割合（0 < ratio < 1）。

    Returns:
        ``(is_prices, oos_prices)`` のタプル。

    Raises:
        ValueError: ratio が範囲外、または分割後のいずれかの区間が短すぎる場合。
    """
    prices = prices.dropna()
    if not 0.0 < ratio < 1.0:
        raise ValueError(f"ratio ({ratio}) は 0 と 1 の間で指定してください")
    n_is = int(round(len(prices) * ratio))
    if n_is < 2 or len(prices) - n_is < 2:
        raise ValueError(
            f"分割後の区間が短すぎます（全体 {len(prices)} 営業日、IS {n_is} 営業日）"
        )
    return prices.iloc[:n_is], prices.iloc[n_is:]


def parameter_sweep(
    prices: pd.Series,
    signal_fn: Callable[..., pd.Series],
    param_grid: Sequence[Mapping[str, float | int]],
    cost_bps: float = 0.0,
) -> list[tuple[dict[str, float | int], BacktestResult]]:
    """パラメータグリッドの各組み合わせでバックテストを実行する（頑健性確認用）。

    最良パラメータの近傍でも成績が維持されるかを確認するためのスイープ。
    返り値の件数が多重検定の試行回数 $N$ に相当する——$N$ 個の無価値な戦略でも
    少なくとも1つが5%有意になる確率は $1-(1-0.05)^N$ に達するため、
    最良の組み合わせの成績を額面どおり解釈してはならない。

    Args:
        prices: 終値系列。
        signal_fn: パラメータをキーワード引数に取りポジション系列を返す関数
            （例: :func:`ma_cross_signal`、:func:`rsi_reversal_signal`）。
        param_grid: パラメータ辞書のリスト（例: ``[{"fast": 20, "slow": 75}, ...]``）。
        cost_bps: 片道取引コスト（ベーシスポイント）。

    Returns:
        ``(パラメータ辞書, BacktestResult)`` のリスト（グリッドと同順）。
    """
    results: list[tuple[dict[str, float | int], BacktestResult]] = []
    for params in param_grid:
        signal = signal_fn(prices, **params)
        results.append((dict(params), run_backtest(prices, signal, cost_bps=cost_bps)))
    return results

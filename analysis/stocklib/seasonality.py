"""季節性・カレンダーアノマリーの集計モジュール（純関数・ネットワーク不使用）。

長期の価格系列（``DatetimeIndex`` を持つ ``Close`` 系列）から、暦（月・曜日・月内の
位置・半期）に紐づくリターンの規則性を機械的に集計する。集計する指標と定義は以下。
解釈と統計的注意の枠組みは :file:`knowledge/strategies/market-anomalies-and-seasonality.md`。

- **月別効果（Month-of-the-Year）**: 月末終値どうしの月次リターン

  $$ r^{(m)}_{y} = \\frac{P_{\\text{末}(y,\\,m)}}{P_{\\text{末}(y,\\,m-1)}} - 1 $$

  を暦月 $m \\in \\{1,\\dots,12\\}$ ごとに集めた、平均リターン・勝率（正の月の割合）・標本数 $n$。

- **曜日効果（Day-of-the-Week）**: 日次リターン $r_t = P_t / P_{t-1} - 1$ を曜日
  （月〜金）ごとに集めた、平均リターン・勝率・標本数 $n$。

- **月内（月初/月末）効果（Turn-of-the-Month）**: 各暦月の**最初の** ``first_days`` 立会日と
  **最後の** ``last_days`` 立会日を「月替わり窓（TOM 窓）」とし、窓内の日次リターン平均と
  窓外（月中）の日次リターン平均を対比する。標本数 $n$ を各群に併記。

- **半期効果（Sell in May / ハロウィン効果）**: 月次リターンを **11〜4月**（$m\\in\\{11,12,1,2,3,4\\}$）と
  **5〜10月**（$m\\in\\{5,\\dots,10\\}$）の2群に分け、各群の平均月次リターンと標本数 $n$ を対比する。

いずれも**過去に観測された標本統計であり、将来の再現を保証するものではない**。月×曜日×
期間の探索は本質的に多重検定であり、真の効果がなくても偶然「有意に見える」組み合わせが
生じうる（データスヌーピング）。少数標本では各集計の平均は不安定なので、必ず標本数 $n$ を
併記できる形にしている。
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# 月替わり窓（Turn-of-the-Month）の既定: 月初・月末それぞれの立会日数。
DEFAULT_FIRST_DAYS: int = 3
DEFAULT_LAST_DAYS: int = 3

# 曜日ラベル（0=月曜 .. 4=金曜）。
WEEKDAY_LABELS: tuple[str, ...] = ("月", "火", "水", "木", "金")

# 半期効果（Sell in May）の「勝ち」とされる側の暦月。
WINTER_MONTHS: frozenset[int] = frozenset({11, 12, 1, 2, 3, 4})  # 11〜4月


@dataclass
class GroupStat:
    """1グループ（ある月・ある曜日など）のリターン集計。

    Attributes:
        label: 表示ラベル（例: ``"1月"``、``"月"``）。
        mean_return: グループ内リターンの単純平均。標本ゼロなら NaN。
        win_rate: 正のリターンの割合（0..1）。標本ゼロなら NaN。
        std: グループ内リターンの標本標準偏差（``ddof=1``）。標本1以下は NaN。
        n: 標本数。
    """

    label: str
    mean_return: float
    win_rate: float
    std: float
    n: int


@dataclass
class TurnOfMonthStat:
    """月内（月初/月末）効果の集計。

    Attributes:
        first_days: 月初の立会日数（窓に含める）。
        last_days: 月末の立会日数（窓に含める）。
        tom: 月替わり窓内の日次リターン集計（:class:`GroupStat`）。
        rest: 窓外（月中）の日次リターン集計。
    """

    first_days: int
    last_days: int
    tom: GroupStat
    rest: GroupStat

    @property
    def edge(self) -> float:
        """窓内平均 − 窓外平均（正なら月替わり窓の方が高リターン）。どちらか空なら NaN。"""
        if np.isnan(self.tom.mean_return) or np.isnan(self.rest.mean_return):
            return float("nan")
        return self.tom.mean_return - self.rest.mean_return


@dataclass
class SeasonalityResult:
    """季節性・カレンダーアノマリーの集計結果一式。

    Attributes:
        monthly: 暦月（1..12）ごとの月次リターン集計。
        weekday: 曜日（月..金）ごとの日次リターン集計。
        turn_of_month: 月内（月初/月末）効果。
        winter: 11〜4月の月次リターン集計（Sell in May の「勝ち」側）。
        summer: 5〜10月の月次リターン集計。
        n_months: 使用した月次リターンの標本数。
        n_days: 使用した日次リターンの標本数。
        years: データが跨る暦年の数（distinct な年数）。
        start: 価格系列の最初の日付。
        end: 価格系列の最後の日付。
    """

    monthly: list[GroupStat] = field(default_factory=list)
    weekday: list[GroupStat] = field(default_factory=list)
    turn_of_month: TurnOfMonthStat | None = None
    winter: GroupStat | None = None
    summer: GroupStat | None = None
    n_months: int = 0
    n_days: int = 0
    years: int = 0
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None

    @property
    def sell_in_may_edge(self) -> float:
        """11〜4月平均 − 5〜10月平均（正ならハロウィン効果と整合）。どちらか空なら NaN。"""
        if self.winter is None or self.summer is None:
            return float("nan")
        if np.isnan(self.winter.mean_return) or np.isnan(self.summer.mean_return):
            return float("nan")
        return self.winter.mean_return - self.summer.mean_return


def _as_close(prices: pd.DataFrame | pd.Series) -> pd.Series:
    """入力（OHLCV DataFrame か Close 系列）から昇順・欠損除去済みの Close 系列を得る。"""
    if isinstance(prices, pd.DataFrame):
        if "Close" not in prices.columns:
            raise ValueError("価格 DataFrame には 'Close' 列が必要です。")
        close = prices["Close"]
    else:
        close = prices
    if not isinstance(close.index, pd.DatetimeIndex):
        raise ValueError("価格系列は DatetimeIndex を持つ必要があります。")
    return close.astype(float).dropna().sort_index()


def _group_stat(label: str, returns: pd.Series) -> GroupStat:
    """リターン系列から平均・勝率・標準偏差・標本数を計算する。"""
    vals = returns.dropna()
    n = int(len(vals))
    if n == 0:
        return GroupStat(label=label, mean_return=float("nan"),
                         win_rate=float("nan"), std=float("nan"), n=0)
    mean = float(vals.mean())
    win_rate = float((vals > 0).sum()) / n
    std = float(vals.std(ddof=1)) if n > 1 else float("nan")
    return GroupStat(label=label, mean_return=mean, win_rate=win_rate, std=std, n=n)


def monthly_returns(prices: pd.DataFrame | pd.Series) -> pd.Series:
    """月末終値どうしの月次リターン系列を返す（index は各月末）。

    $r^{(m)}_y = P_{\\text{末}(y,m)} / P_{\\text{末}(y,m-1)} - 1$。各暦月の最終立会日の終値を
    使う（``resample('ME').last()``）。先頭月は前月末が無いため NaN として除外される。
    """
    close = _as_close(prices)
    month_end = close.resample("ME").last().dropna()
    return month_end.pct_change().dropna()


def daily_returns(prices: pd.DataFrame | pd.Series) -> pd.Series:
    """日次リターン系列 $r_t = P_t / P_{t-1} - 1$ を返す。"""
    close = _as_close(prices)
    return close.pct_change().dropna()


def monthly_effect(prices: pd.DataFrame | pd.Series) -> list[GroupStat]:
    """暦月（1..12）ごとの月次リターン集計を返す（1月→12月の順、標本ゼロ月も含む）。"""
    mret = monthly_returns(prices)
    out: list[GroupStat] = []
    for m in range(1, 13):
        label = f"{m}月"
        subset = mret[mret.index.month == m] if len(mret) else mret
        out.append(_group_stat(label, subset))
    return out


def weekday_effect(prices: pd.DataFrame | pd.Series) -> list[GroupStat]:
    """曜日（月..金）ごとの日次リターン集計を返す（土日は立会がないため対象外）。"""
    dret = daily_returns(prices)
    out: list[GroupStat] = []
    for wd in range(5):  # 0=月曜 .. 4=金曜
        subset = dret[dret.index.dayofweek == wd] if len(dret) else dret
        out.append(_group_stat(WEEKDAY_LABELS[wd], subset))
    return out


def turn_of_month_effect(
    prices: pd.DataFrame | pd.Series,
    first_days: int = DEFAULT_FIRST_DAYS,
    last_days: int = DEFAULT_LAST_DAYS,
) -> TurnOfMonthStat:
    """月内（月初/月末）効果を集計する。

    各暦月（年×月）の立会日を日付順に並べ、先頭 ``first_days`` 日と末尾 ``last_days`` 日を
    「月替わり窓（TOM 窓）」として印を付ける。窓が重なる短い月（立会日数
    ``<= first_days + last_days``）はその月の全日が窓になる。窓内・窓外それぞれの
    日次リターン平均を :class:`GroupStat` で返す。

    Raises:
        ValueError: ``first_days`` または ``last_days`` が負の場合。
    """
    if first_days < 0 or last_days < 0:
        raise ValueError("first_days / last_days は 0 以上を指定してください。")
    dret = daily_returns(prices)
    if len(dret) == 0:
        empty = _group_stat("月替わり窓", dret)
        return TurnOfMonthStat(first_days, last_days, empty, _group_stat("月中", dret))

    idx = dret.index
    period = idx.to_period("M")
    # 月内での立会日順の連番（0 始まり）と、月内の総立会日数。
    order = pd.Series(np.arange(len(dret)), index=idx).groupby(period).cumcount()
    size = pd.Series(1, index=idx).groupby(period).transform("size")
    order_arr = order.to_numpy()
    size_arr = size.to_numpy()
    is_tom = (order_arr < first_days) | (order_arr >= size_arr - last_days)

    tom = _group_stat("月替わり窓", dret[is_tom])
    rest = _group_stat("月中", dret[~is_tom])
    return TurnOfMonthStat(first_days, last_days, tom, rest)


def sell_in_may(prices: pd.DataFrame | pd.Series) -> tuple[GroupStat, GroupStat]:
    """半期効果（Sell in May / ハロウィン効果）の集計を ``(winter, summer)`` で返す。

    ``winter`` は 11〜4月、``summer`` は 5〜10月の月次リターンの集計。
    """
    mret = monthly_returns(prices)
    if len(mret):
        winter_mask = mret.index.month.isin(sorted(WINTER_MONTHS))
        winter = _group_stat("11〜4月", mret[winter_mask])
        summer = _group_stat("5〜10月", mret[~winter_mask])
    else:
        winter = _group_stat("11〜4月", mret)
        summer = _group_stat("5〜10月", mret)
    return winter, summer


def compute_seasonality(
    prices: pd.DataFrame | pd.Series,
    first_days: int = DEFAULT_FIRST_DAYS,
    last_days: int = DEFAULT_LAST_DAYS,
) -> SeasonalityResult:
    """価格系列から季節性・カレンダーアノマリー集計一式を計算する。

    Args:
        prices: OHLCV DataFrame（``Close`` 列必須）または Close 系列。
            :func:`stocklib.data.fetch_prices` の 1 銘柄分をそのまま渡せる。
        first_days: 月替わり窓に含める月初の立会日数。
        last_days: 月替わり窓に含める月末の立会日数。

    Returns:
        :class:`SeasonalityResult`。
    """
    close = _as_close(prices)
    mret = monthly_returns(close)
    dret = daily_returns(close)
    years = int(pd.Index(close.index.year).nunique()) if len(close) else 0
    winter, summer = sell_in_may(close)
    return SeasonalityResult(
        monthly=monthly_effect(close),
        weekday=weekday_effect(close),
        turn_of_month=turn_of_month_effect(close, first_days, last_days),
        winter=winter,
        summer=summer,
        n_months=int(len(mret)),
        n_days=int(len(dret)),
        years=years,
        start=close.index[0] if len(close) else None,
        end=close.index[-1] if len(close) else None,
    )


def month_name(m: int) -> str:
    """英語の月名（デバッグ・補助用）。"""
    return calendar.month_name[m]

"""セクターローテーション（セクター別相対強度）の集計モジュール。

「いま資金がどのセクターに向かっているか（相対的に強いセクターはどこか）」を、
ユニバースの複数銘柄の OHLCV から機械的に集計する。個別銘柄のモメンタムを
セクター単位に束ね、セクター間で順位づけ（リーダー / ラガード）する。

セクターローテーションは、景気サイクルや金利局面に応じて主導セクターが循環する
という経験則に基づく見方だが、ここでの集計はあくまで**過去リターンのクロスセクション
比較**であり、将来のセクターの騰落を予測するものでも売買助言でもない。

集計する指標:

- **複数期間モメンタム**: 各銘柄の 1 / 3 / 6 / 12 ヶ月（21 / 63 / 126 / 252 営業日）
  リターン $r_w = C_t / C_{t-w} - 1$。
- **セクター・モメンタム**: セクター内銘柄のモメンタムの**中央値**
  （外れ値に頑健。少数銘柄では不安定）。直近（既定 63 営業日 ≈ 3 ヶ月）の
  セクター・モメンタムでセクターを降順ランキングする。
- **セクター内ブレッシュ**: 各セクターで終値 > SMA50 の銘柄割合
  $\\text{breadth}_s = \\#\\{i \\in s : C_t^{(i)} \\ge \\mathrm{SMA}_{50}^{(i)}\\} / n_s$。
  セクター内の値上がりの広がりを測る（母数はその窓を満たす銘柄）。

いずれもクロスセクションの機械的な相対比較であり、将来予測でも投資助言でもない。
セクター分類は入力の ``{code: sector}``（ユニバース CSV の sector 列）に依存する。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

import pandas as pd

# セクター・モメンタムに使う (窓（営業日）, ラベル)。
# 21≈1ヶ月 / 63≈3ヶ月 / 126≈6ヶ月 / 252≈12ヶ月。
MOMENTUM_WINDOWS: tuple[tuple[int, str], ...] = (
    (21, "1ヶ月"), (63, "3ヶ月"), (126, "6ヶ月"), (252, "12ヶ月"),
)
# セクターのランキングに使う代表窓（既定 63 営業日 ≈ 3 ヶ月）。
RANK_WINDOW: int = 63
# セクター内ブレッシュの SMA 窓（営業日）。
BREADTH_SMA_WINDOW: int = 50

# 「不明」セクターの表示名（sector 未指定の銘柄をまとめる）。
UNKNOWN_SECTOR: str = "（不明）"


@dataclass
class SectorRow:
    """1セクターのモメンタム集約とセクター内ブレッシュ。"""

    sector: str
    n: int                                         # このセクターで集計に使えた銘柄数
    momentum: dict[int, float] = field(default_factory=dict)  # 窓 → セクター内中央値リターン
    breadth_above_sma: float = float("nan")        # 終値 > SMA50 の割合（0..1、NaN=母数0）
    breadth_base: int = 0                          # ブレッシュ判定に使えた銘柄数
    rank: int = 0                                  # 代表窓モメンタム降順の順位（1=リーダー）

    @property
    def rank_momentum(self) -> float | None:
        """ランキングに使う代表窓（:data:`RANK_WINDOW`）のセクター・モメンタム。"""
        return self.momentum.get(RANK_WINDOW)


def _return_over(close: pd.Series, window: int) -> float | None:
    """window 営業日前比リターン $C_t / C_{t-w} - 1$。

    データ不足（``len <= window``）または始点が非正なら None。
    """
    if len(close) <= window:
        return None
    start = float(close.iloc[-1 - window])
    if start <= 0:
        return None
    return float(close.iloc[-1]) / start - 1.0


def _above_sma(close: pd.Series, window: int) -> bool | None:
    """終値が直近 window 本の単純移動平均以上か。データ不足なら None。"""
    if len(close) < window:
        return None
    return float(close.iloc[-1]) >= float(close.iloc[-window:].mean())


def _median_or_nan(values: list[float]) -> float:
    """有限値の中央値。空なら NaN。"""
    clean = [v for v in values if v is not None and math.isfinite(v)]
    return statistics.median(clean) if clean else float("nan")


def compute_sector_rotation(
    prices: dict[str, pd.DataFrame],
    sectors: dict[str, str],
) -> list[SectorRow]:
    """ユニバースの OHLCV とセクター辞書からセクター別相対強度を集計する。

    各銘柄の複数期間モメンタム（:data:`MOMENTUM_WINDOWS`）を計算し、セクター単位に
    **中央値**で集約する。セクター内ブレッシュ（終値 > SMA50 の割合）も算出し、
    代表窓（:data:`RANK_WINDOW`）のセクター・モメンタム降順で順位づけする。

    Args:
        prices: ``{code: OHLCV DataFrame}``（``Close`` 列必須）。
            :func:`stocklib.data.fetch_prices` の戻り値をそのまま渡せる。
        sectors: ``{code: セクター名}``（ユニバース CSV の sector 列など）。
            空文字・未登録のコードは :data:`UNKNOWN_SECTOR` に束ねる。

    Returns:
        :class:`SectorRow` のリスト。代表窓モメンタムの降順（リーダー→ラガード）で、
        代表窓を算出できないセクター（データ期間不足）は末尾に回す（社名順で安定化）。
        銘柄が1つも集計できないセクターは含めない。``rank`` は 1 起点で付与済み。
    """
    # セクター → そのセクターに属する各銘柄の Close（欠損除去済み）。
    by_sector: dict[str, list[pd.Series]] = {}
    for code, df in prices.items():
        if "Close" not in df.columns:
            continue
        close = df["Close"].dropna()
        if len(close) < 2:
            continue
        sector = (sectors.get(code) or "").strip() or UNKNOWN_SECTOR
        by_sector.setdefault(sector, []).append(close)

    rows: list[SectorRow] = []
    for sector, closes in by_sector.items():
        # 窓ごとに、その窓を満たす銘柄のリターンを集めて中央値をとる。
        momentum: dict[int, float] = {}
        for window, _label in MOMENTUM_WINDOWS:
            rets = [r for c in closes if (r := _return_over(c, window)) is not None]
            if rets:
                momentum[window] = _median_or_nan(rets)

        flags = [f for c in closes if (f := _above_sma(c, BREADTH_SMA_WINDOW)) is not None]
        breadth_base = len(flags)
        breadth = (sum(1 for f in flags if f) / breadth_base) if breadth_base else float("nan")

        rows.append(SectorRow(
            sector=sector,
            n=len(closes),
            momentum=momentum,
            breadth_above_sma=breadth,
            breadth_base=breadth_base,
        ))

    # 代表窓モメンタム降順。算出不可（None）は末尾、同点はセクター名で安定化。
    def _sort_key(r: SectorRow) -> tuple[int, float, str]:
        m = r.rank_momentum
        has = 0 if m is not None else 1
        return (has, -(m if m is not None else 0.0), r.sector)

    rows.sort(key=_sort_key)
    for i, row in enumerate(rows, start=1):
        row.rank = i
    return rows

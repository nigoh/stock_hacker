"""相対強度（RS）とセクター相対バリュエーションの集計モジュール。

「その銘柄が市場（ユニバース）の中で相対的に強いか・割安か」を機械的に測る。
個別の絶対水準（RSI が何％、PER が何倍）だけでは「他と比べてどうか」が分からないため、
ユニバース横断の順位づけ（クロスセクション）で文脈を与える。

- **相対強度（RS）ランク**: 複数期間のトレンド（モメンタム）を加重合成したスコアを
  ユニバース内でパーセンタイル（1〜99）に変換する。IBD の RS Rating に着想を得た素朴版。
- **セクター相対バリュエーション**: 各銘柄の PER / PBR を、同セクターの中央値と比較して
  ディスカウント / プレミアムを出す（割安・割高の相対評価）。

いずれもクロスセクションの相対比較であり、将来の騰落の予測でも売買助言でもない。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

import pandas as pd

# モメンタム合成に使う (窓（営業日）, 重み)。直近を重めにする素朴な配分。
# 63≈3ヶ月 / 126≈6ヶ月 / 189≈9ヶ月 / 252≈12ヶ月。
MOMENTUM_WINDOWS: tuple[tuple[int, float], ...] = (
    (63, 0.40), (126, 0.20), (189, 0.20), (252, 0.20),
)


@dataclass
class RSRow:
    """1銘柄の相対強度。"""

    code: str
    name: str
    blended_return: float           # 加重合成モメンタム（比率、例 0.15=+15%）
    components: dict[int, float] = field(default_factory=dict)  # 窓 → その期間リターン
    rs_rank: float = float("nan")   # ユニバース内パーセンタイル（1..99）


@dataclass
class ValationRow:
    """1銘柄のセクター相対バリュエーション。"""

    code: str
    name: str
    sector: str
    per: float | None
    pbr: float | None
    sector_per_median: float | None
    sector_pbr_median: float | None
    per_premium: float | None       # per/median - 1（+ が割高、− が割安）
    pbr_premium: float | None


def _return_over(close: pd.Series, window: int) -> float | None:
    """window 営業日前比リターン。データ不足・非正の始点なら None。"""
    if len(close) <= window:
        return None
    start = float(close.iloc[-1 - window])
    if start <= 0:
        return None
    return float(close.iloc[-1]) / start - 1.0


def blended_momentum(close: pd.Series) -> tuple[float, dict[int, float]] | None:
    """複数期間リターンの加重合成モメンタムと内訳を返す。

    利用可能な窓だけで重みを正規化して合成する（全窓欠損なら None）。
    """
    total_w = 0.0
    acc = 0.0
    components: dict[int, float] = {}
    for window, weight in MOMENTUM_WINDOWS:
        r = _return_over(close, window)
        if r is None:
            continue
        components[window] = r
        acc += weight * r
        total_w += weight
    if total_w <= 0:
        return None
    return acc / total_w, components


def _percentile_ranks(scores: list[float]) -> list[float]:
    """スコア列を 1..99 のパーセンタイル順位に変換する（大きいほど高順位）。

    同点は同じ平均順位を与える。要素1つなら 50 を返す。
    """
    n = len(scores)
    if n == 0:
        return []
    if n == 1:
        return [50.0]
    order = sorted(range(n), key=lambda i: scores[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        # i..j は同点。平均の 0-based 位置を使う。
        avg_pos = (i + j) / 2.0
        pct = 1.0 + 98.0 * (avg_pos / (n - 1))
        for k in range(i, j + 1):
            ranks[order[k]] = pct
        i = j + 1
    return ranks


def compute_relative_strength(
    prices: dict[str, pd.DataFrame], names: dict[str, str] | None = None
) -> list[RSRow]:
    """ユニバースの相対強度ランキングを計算する（RSランク降順で返す）。

    Args:
        prices: ``{code: OHLCV DataFrame}``（``Close`` 列必須）。
        names: ``{code: 表示名}``（任意）。

    Returns:
        :class:`RSRow` のリスト（``rs_rank`` 降順）。モメンタムを計算できない銘柄は除外。
    """
    names = names or {}
    rows: list[RSRow] = []
    for code, df in prices.items():
        if "Close" not in df.columns:
            continue
        close = df["Close"].dropna()
        result = blended_momentum(close)
        if result is None:
            continue
        blended, components = result
        rows.append(RSRow(code=code, name=names.get(code, ""),
                          blended_return=blended, components=components))
    ranks = _percentile_ranks([r.blended_return for r in rows])
    for row, rank in zip(rows, ranks):
        row.rs_rank = rank
    rows.sort(key=lambda r: r.rs_rank, reverse=True)
    return rows


def _median_or_none(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None and math.isfinite(v) and v > 0]
    return statistics.median(clean) if clean else None


def sector_relative_valuation(
    infos: dict[str, dict[str, object]],
    sectors: dict[str, str],
    names: dict[str, str] | None = None,
) -> list[ValationRow]:
    """各銘柄の PER / PBR をセクター中央値と比較する。

    Args:
        infos: ``{code: fetch_info の戻り値}``。``"PER（実績）"`` / ``"PBR"`` キーを見る。
        sectors: ``{code: セクター名}``（ユニバース CSV の sector 列など）。
        names: ``{code: 表示名}``（任意）。

    Returns:
        :class:`ValationRow` のリスト（セクター→コード順）。
    """
    names = names or {}

    def _num(info: dict[str, object], key: str) -> float | None:
        v = info.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)):
            return float(v)
        return None

    per: dict[str, float | None] = {}
    pbr: dict[str, float | None] = {}
    for code, info in infos.items():
        per[code] = _num(info, "PER（実績）")
        pbr[code] = _num(info, "PBR")

    # セクターごとの中央値
    sector_codes: dict[str, list[str]] = {}
    for code, sector in sectors.items():
        sector_codes.setdefault(sector or "（不明）", []).append(code)
    sector_per_med: dict[str, float | None] = {}
    sector_pbr_med: dict[str, float | None] = {}
    for sector, codes in sector_codes.items():
        sector_per_med[sector] = _median_or_none([per.get(c) for c in codes])  # type: ignore[misc]
        sector_pbr_med[sector] = _median_or_none([pbr.get(c) for c in codes])  # type: ignore[misc]

    rows: list[ValationRow] = []
    for code in infos:
        sector = sectors.get(code) or "（不明）"
        p, b = per.get(code), pbr.get(code)
        pmed, bmed = sector_per_med.get(sector), sector_pbr_med.get(sector)
        rows.append(ValationRow(
            code=code, name=names.get(code, ""), sector=sector,
            per=p, pbr=b, sector_per_median=pmed, sector_pbr_median=bmed,
            per_premium=(p / pmed - 1.0) if (p and pmed) else None,
            pbr_premium=(b / bmed - 1.0) if (b and bmed) else None,
        ))
    rows.sort(key=lambda r: (r.sector, r.code))
    return rows

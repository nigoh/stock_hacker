"""ペアトレード（統計的裁定）の候補スクリーニング。

2銘柄の対数価格が長期的に連動（共和分）していれば、その差（スプレッド）は平均回帰し、
乖離が開いたときに「割高側を売り・割安側を買い」で鞘取りを狙える。本モジュールは
ユニバースの全ペアについて、連動性と平均回帰の強さを機械的に測って候補を並べる。

指標（numpy のみで実装。scipy/statsmodels 非依存）:

- **ヘッジ比 β**: $\\log P^a_t = \\alpha + \\beta \\log P^b_t + \\varepsilon_t$ の OLS 傾き。
  スプレッド $s_t = \\log P^a_t - (\\alpha + \\beta \\log P^b_t)$。
- **Dickey-Fuller 統計量**: $\\Delta s_t = a + b\\, s_{t-1} + u_t$ の傾き $b$ の t 値。
  値が小さい（負に大きい）ほどスプレッドは定常＝平均回帰的（単位根仮説を棄却しやすい）。
  目安の臨界値（定数項ありのケース、MacKinnon 近似）: 5% ≈ −2.86、1% ≈ −3.43。
- **半減期**: OU 過程近似で $\\tau_{1/2} = -\\ln 2 / b$（$b<0$ のときのみ有限。乖離が半分に
  戻るまでの平均営業日数）。
- **現在の z スコア**: $(s_T - \\bar s)/\\sigma_s$。|z| が大きいほど直近の乖離が大きい。

**注意**: これはインサンプルの統計的スクリーニングであり、将来の平均回帰を保証しない。
$N$ 銘柄なら $N(N-1)/2$ ペアを検定する多重比較で偽陽性が出やすい。売買助言ではない。
背景は :file:`knowledge/strategies/pairs-trading-and-arbitrage.md` を参照。
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

# Dickey-Fuller の目安臨界値（定数項あり、MacKinnon 近似、大標本）。
DF_CRIT_5PCT: float = -2.86
DF_CRIT_1PCT: float = -3.43
# スクリーニングの既定: この営業日数以上の重なりが無いペアは評価しない。
DEFAULT_MIN_OVERLAP: int = 250


@dataclass
class PairResult:
    """1ペアの共和分スクリーニング結果。"""

    code_a: str
    code_b: str
    name_a: str
    name_b: str
    sector_a: str
    sector_b: str
    n: int              # 評価に使った営業日数（重なり）
    corr: float         # 日次リターンの相関
    beta: float         # ヘッジ比（log_a ~ log_b の傾き）
    df_stat: float      # Dickey-Fuller 統計量（小さいほど平均回帰的）
    half_life: float    # 半減期（営業日。非平均回帰なら nan）
    zscore: float       # 直近スプレッドの z スコア

    @property
    def same_sector(self) -> bool:
        return bool(self.sector_a) and self.sector_a == self.sector_b

    def is_mean_reverting(self, crit: float = DF_CRIT_5PCT) -> bool:
        """DF 統計量が臨界値未満（＝平均回帰的と判定できる）か。"""
        return math.isfinite(self.df_stat) and self.df_stat < crit


def _ols(y: np.ndarray, x: np.ndarray) -> tuple[float, float, np.ndarray]:
    """単回帰 y = intercept + slope*x を OLS で解く。(intercept, slope, residuals)。"""
    X = np.column_stack([np.ones_like(x), x])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    intercept, slope = float(coef[0]), float(coef[1])
    resid = y - (intercept + slope * x)
    return intercept, slope, resid


def _df_stat_and_half_life(spread: np.ndarray) -> tuple[float, float]:
    """スプレッドの Dickey-Fuller 統計量と OU 半減期を返す。

    $\\Delta s_t = a + b\\, s_{t-1} + u_t$ を OLS で解き、$b$ の t 値（DF 統計量）と
    $\\tau_{1/2} = -\\ln 2 / b$（$b<0$ のときのみ有限）を計算する。
    """
    s = spread[np.isfinite(spread)]
    if len(s) < 30:
        return float("nan"), float("nan")
    lag = s[:-1]
    delta = np.diff(s)
    n = len(delta)
    X = np.column_stack([np.ones(n), lag])
    coef, *_ = np.linalg.lstsq(X, delta, rcond=None)
    b = float(coef[1])
    resid = delta - X @ coef
    dof = n - 2
    if dof <= 0:
        return float("nan"), float("nan")
    sigma2 = float(resid @ resid) / dof
    # slope の標準誤差 = sqrt(sigma2 / Σ(x-x̄)^2)
    ssx = float(np.sum((lag - lag.mean()) ** 2))
    if ssx <= 0 or sigma2 <= 0:
        return float("nan"), float("nan")
    se_b = math.sqrt(sigma2 / ssx)
    df_stat = b / se_b if se_b > 0 else float("nan")
    half_life = (-math.log(2.0) / b) if b < 0 else float("nan")
    return df_stat, half_life


def analyze_pair(
    close_a: pd.Series, close_b: pd.Series,
    *, code_a: str = "", code_b: str = "",
    name_a: str = "", name_b: str = "", sector_a: str = "", sector_b: str = "",
) -> PairResult | None:
    """2銘柄の終値系列からペア統計を計算する。重なりが不足なら None。"""
    joined = pd.concat([close_a.rename("a"), close_b.rename("b")], axis=1, join="inner").dropna()
    joined = joined[(joined["a"] > 0) & (joined["b"] > 0)]
    if len(joined) < 30:
        return None
    log_a = np.log(joined["a"].to_numpy())
    log_b = np.log(joined["b"].to_numpy())
    intercept, beta, resid = _ols(log_a, log_b)
    spread = log_a - (intercept + beta * log_b)
    df_stat, half_life = _df_stat_and_half_life(spread)

    sd = float(np.std(spread))
    zscore = float((spread[-1] - float(np.mean(spread))) / sd) if sd > 0 else float("nan")

    ret_a = np.diff(log_a)
    ret_b = np.diff(log_b)
    if len(ret_a) > 1 and np.std(ret_a) > 0 and np.std(ret_b) > 0:
        corr = float(np.corrcoef(ret_a, ret_b)[0, 1])
    else:
        corr = float("nan")  # 無変動系列は相関を定義しない

    return PairResult(
        code_a=code_a, code_b=code_b, name_a=name_a, name_b=name_b,
        sector_a=sector_a, sector_b=sector_b, n=len(joined), corr=corr,
        beta=beta, df_stat=df_stat, half_life=half_life, zscore=zscore,
    )


def find_pairs(
    prices: dict[str, pd.DataFrame],
    names: dict[str, str] | None = None,
    sectors: dict[str, str] | None = None,
    *,
    min_overlap: int = DEFAULT_MIN_OVERLAP,
    same_sector_only: bool = False,
) -> list[PairResult]:
    """ユニバースの全ペアを評価し、平均回帰の強い順（DF 統計量の昇順）に返す。

    Args:
        prices: ``{code: OHLCV DataFrame}``（``Close`` 列必須）。
        names / sectors: ``{code: 表示名/セクター}``（任意）。
        min_overlap: 評価に必要な最小の重なり営業日数。
        same_sector_only: True なら同セクターのペアのみ評価する。

    Returns:
        :class:`PairResult` のリスト（DF 統計量の昇順＝平均回帰的な順）。
    """
    names = names or {}
    sectors = sectors or {}
    closes = {c: df["Close"].dropna() for c, df in prices.items() if "Close" in df.columns}
    codes = sorted(closes)
    out: list[PairResult] = []
    for code_a, code_b in itertools.combinations(codes, 2):
        sec_a, sec_b = sectors.get(code_a, ""), sectors.get(code_b, "")
        if same_sector_only and (not sec_a or sec_a != sec_b):
            continue
        result = analyze_pair(
            closes[code_a], closes[code_b],
            code_a=code_a, code_b=code_b,
            name_a=names.get(code_a, ""), name_b=names.get(code_b, ""),
            sector_a=sec_a, sector_b=sec_b,
        )
        if result is None or result.n < min_overlap:
            continue
        out.append(result)
    out.sort(key=lambda r: (r.df_stat if math.isfinite(r.df_stat) else float("inf")))
    return out

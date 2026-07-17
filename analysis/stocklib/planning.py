"""資産形成プランニング（積立シミュレーション・目標逆算・取り崩し）のロジック。

価格データを一切使わない、ネットワーク不要の純計算モジュール。

- :func:`compound_projection` — 決定論的複利 + シード固定モンテカルロ（対数正規の
  月次リターン）による積立予測。ファンチャート用のパーセンタイル系列
  （5/25/50/75/95）を返す。
- :func:`required_monthly_saving` — 目標額から毎月の必要積立額を逆算する
  （導出式は docstring 参照）。
- :func:`required_annual_return` — 現在資産と積立ペースを所与として、目標額の
  達成に必要な年率リターンを数値解（二分法）で逆算する（progress サブコマンド用）。
- :func:`net_of_cost_return` — 信託報酬等の年率コストを想定リターンから控除した
  実効リターン $(1+R)(1-c) - 1$ を返す（全サブコマンド共通の ``--cost``）。
- :func:`decumulation_simulation` — 取り崩し（定額 / 定率）のモンテカルロ。
  枯渇確率と、シークエンス・オブ・リターンズ（リターン順序）の影響を定量化する。
- :func:`nisa_tax_benefit` — 課税口座（税率 20.315%、2025年時点）との比較で
  NISA の非課税メリットを定量化する。

全関数の出力は「ユーザーが入力した想定リターン・想定ボラティリティに基づく試算」で
あり、将来の運用成果の予測でも保証でもない。レポートには必ず
:data:`ASSUMPTION_NOTE` を含めること。

数式の要点:

- 年率リターン $R$ の月利換算（幾何）: $r = (1+R)^{1/12} - 1$
- 月末拠出 $P$・初期資産 $V_0$・月数 $n$ の決定論的将来価値:
  $$FV = V_0 (1+r)^n + P \\frac{(1+r)^n - 1}{r} \\quad (r \\ne 0)$$
- モンテカルロの月次グロスリターン: $G_t = e^{X_t},\\ X_t \\sim
  \\mathcal{N}(\\mu_m, \\sigma_m^2)$、$\\sigma_m = \\sigma / \\sqrt{12}$、
  $\\mu_m = \\ln(1+R)/12 - \\sigma_m^2/2$。この設定で
  $\\mathbb{E}[G_t] = (1+R)^{1/12}$（期待値が決定論的複利と一致）となり、
  中央値パスは $e^{\\mu_m} < (1+R)^{1/12}$ で決定論的複利を下回る
  （ボラティリティ・ドラッグ）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np

MONTHS_PER_YEAR: int = 12

#: ファンチャートに用いるパーセンタイル（下位5% 〜 上位95%）
PERCENTILES: tuple[int, ...] = (5, 25, 50, 75, 95)

#: 上場株式等の譲渡益・配当への課税率（所得税15% + 復興特別所得税0.315% + 住民税5%。
#: 2025年時点。復興特別所得税は2037年まで）
TAX_RATE_TAXABLE: float = 0.20315

# 新NISA（2024年開始）の非課税枠。いずれも2025年時点の制度値。
NISA_TSUMITATE_ANNUAL_LIMIT: int = 1_200_000  #: つみたて投資枠の年間上限（円）
NISA_GROWTH_ANNUAL_LIMIT: int = 2_400_000  #: 成長投資枠の年間上限（円）
NISA_ANNUAL_LIMIT_TOTAL: int = 3_600_000  #: 両枠併用時の年間上限（円）
NISA_LIFETIME_LIMIT: int = 18_000_000  #: 生涯非課税保有限度額（簿価ベース、円）

#: 歴史的な株式リターンの参考値（年率・幾何平均・実質）。Dimson–Marsh–Staunton の
#: 長期データ（1900年以降、2024年時点のイヤーブック）で世界株の実質リターンは
#: 年率約5%、日本株は約4%、短期債に対するリスクプレミアムは年4%前後。
#: 出典と限界（事後平均であり事前の約束ではない）の整理は
#: ``knowledge/strategies/long-term-wealth-building.md`` を参照。
HISTORICAL_EQUITY_REAL_RETURN: float = 0.05

#: :func:`required_annual_return` の結果がこの水準（歴史的参考値 + 2pt）を超えたら、
#: レポートで「積立額または期間の見直しの検討材料」と中立的に注記する閾値。
#: 達成可否の断定には使わない。
REQUIRED_RETURN_CAUTION_THRESHOLD: float = HISTORICAL_EQUITY_REAL_RETURN + 0.02

#: すべてのプランニングレポートに必ず含める前提注意文
ASSUMPTION_NOTE: str = (
    "**想定リターン・想定ボラティリティはユーザーが入力した仮定であり、"
    "将来の運用成果を保証するものではありません。**"
    "実際の市場リターンは年ごとに大きく変動し、想定を下回る期間が長期に及ぶことも"
    "あります。前提（リターン・ボラ・インフレ率）を変えた複数のシナリオで、"
    "幅を持って解釈してください。"
)


# --------------------------------------------------------------------------
# 共通ヘルパー
# --------------------------------------------------------------------------


def monthly_rate(annual_return: float) -> float:
    """年率リターンを幾何換算の月利に変換する: $r = (1+R)^{1/12} - 1$。"""
    if annual_return <= -1.0:
        raise ValueError(f"annual_return は -1（-100%）より大きい必要があります: {annual_return}")
    return (1.0 + annual_return) ** (1.0 / MONTHS_PER_YEAR) - 1.0


def real_value(nominal: float, years: float, annual_inflation: float) -> float:
    """名目値を実質値（現在の購買力換算）に割り引く: $V_{real} = V / (1+\\pi)^{y}$。"""
    if annual_inflation <= -1.0:
        raise ValueError(f"annual_inflation は -1 より大きい必要があります: {annual_inflation}")
    return nominal / (1.0 + annual_inflation) ** years


def _n_months(years: float) -> int:
    """年数を月数に変換する（最低1ヶ月）。"""
    if years <= 0:
        raise ValueError(f"years は正の値が必要です: {years}")
    n = int(round(years * MONTHS_PER_YEAR))
    if n < 1:
        raise ValueError(f"期間が短すぎます（最低1ヶ月）: years={years}")
    return n


def _lognormal_params(annual_return: float, annual_vol: float) -> tuple[float, float]:
    """月次対数リターンの $(\\mu_m, \\sigma_m)$ を返す。

    $\\sigma_m = \\sigma/\\sqrt{12}$、$\\mu_m = \\ln(1+R)/12 - \\sigma_m^2/2$。
    この設定で月次グロスリターンの期待値が $(1+R)^{1/12}$ に一致する
    （モジュール docstring 参照）。
    """
    if annual_vol < 0:
        raise ValueError(f"annual_vol は非負が必要です: {annual_vol}")
    if annual_return <= -1.0:
        raise ValueError(f"annual_return は -1 より大きい必要があります: {annual_return}")
    sigma_m = annual_vol / math.sqrt(MONTHS_PER_YEAR)
    mu_m = math.log1p(annual_return) / MONTHS_PER_YEAR - 0.5 * sigma_m**2
    return mu_m, sigma_m


def _simulate_gross_returns(
    annual_return: float, annual_vol: float, n_months: int, n_paths: int, seed: int
) -> np.ndarray:
    """シード固定の月次グロスリターン行列 (n_paths, n_months) を生成する（対数正規）。"""
    if n_paths < 1:
        raise ValueError(f"n_paths は 1 以上が必要です: {n_paths}")
    mu_m, sigma_m = _lognormal_params(annual_return, annual_vol)
    rng = np.random.default_rng(seed)
    return np.exp(rng.normal(mu_m, sigma_m, size=(n_paths, n_months)))


def net_of_cost_return(annual_return: float, annual_cost: float) -> float:
    """信託報酬等の年率コストを控除した実効年率リターンを返す。

    信託報酬は純資産に対して年率で日割り控除されるため、グロスリターン $R$ ・
    コスト率 $c$ のとき資産の年間成長率は $(1+R)(1-c)$ となり、実効リターンは

    $$R_{net} = (1+R)(1-c) - 1 = R - c - Rc \\approx R - c$$

    コスト差は複利で効く: 差 $\\Delta c$ は $T$ 年で最終資産を
    $1 - (1-\\Delta c)^T$ だけ押し下げる（例: 1.4% 差は30年で約34.5%。
    ``knowledge/market-structure/investment-trusts-and-asset-management.md`` の
    定量化と整合）。

    Args:
        annual_return: 想定年率リターン（コスト控除前。0.05 = 5%）。
        annual_cost: 年率コスト（信託報酬・実質コスト。0.005 = 0.5%。0 以上 1 未満）。

    Returns:
        コスト控除後の実効年率リターン。

    Raises:
        ValueError: 引数が定義域外の場合。
    """
    if not 0.0 <= annual_cost < 1.0:
        raise ValueError(f"annual_cost は 0 以上 1 未満が必要です: {annual_cost}")
    if annual_return <= -1.0:
        raise ValueError(f"annual_return は -1 より大きい必要があります: {annual_return}")
    return (1.0 + annual_return) * (1.0 - annual_cost) - 1.0


def _deterministic_final_value(
    current: float, monthly_amount: float, annual_return: float, n_months: int
) -> float:
    """決定論的複利（月末拠出）の最終資産の閉形式。

    $$FV = V_0 (1+r)^n + P \\frac{(1+r)^n - 1}{r} \\quad (r \\ne 0)$$

    :func:`compound_projection` の ``deterministic`` 系列の最終値と一致する。
    """
    r = monthly_rate(annual_return)
    growth = (1.0 + r) ** n_months
    if r == 0.0:
        return current + monthly_amount * n_months
    return current * growth + monthly_amount * (growth - 1.0) / r


def required_annual_return(
    target_amount: float,
    years: float,
    *,
    current: float = 0.0,
    monthly_amount: float = 0.0,
    tol: float = 1e-12,
) -> float:
    """現在資産と積立ペースを所与として、目標達成に必要な年率リターンを逆算する。

    決定論的複利（月末拠出、:func:`compound_projection` と同一の漸化式）の最終資産

    $$FV(R) = V_0 (1+r)^n + P \\frac{(1+r)^n - 1}{r}, \\quad r = (1+R)^{1/12} - 1$$

    が ``target_amount`` に一致する年率リターン $R$ を二分法で解く。$FV(R)$ は
    $R$ について単調増加（$V_0 > 0$ または $P > 0$ かつ $n \\ge 2$）なので解は一意。
    閉形式では $R$ について解けないため数値解とする。

    結果が負になることもある（元本合計だけで目標を上回る場合。「運用リターンが
    なくても届く」ことを意味する）。結果の解釈はコスト控除後のリターンとして
    扱うこと（信託報酬 $c$ がかかる場合、控除前に必要なリターンは
    $(1+R_{req})/(1-c) - 1$ とさらに高くなる）。

    Args:
        target_amount: 目標額（円、正）。
        years: 残り年数（正）。
        current: 現在の資産評価額（円、非負）。
        monthly_amount: 毎月の積立額（円、非負）。``current`` と少なくとも
            一方は正であること。
        tol: 二分法の収束許容誤差（リターンの絶対誤差）。

    Returns:
        必要な年率リターン（0.05 = 5%）。探索範囲は年率 -99.99% 〜 +1,000,000%。

    Raises:
        ValueError: 引数が定義域外、またはどんな年率リターンでも目標に到達
            できない場合（例: 現在資産0で期間1ヶ月のみ）。
    """
    if target_amount <= 0:
        raise ValueError(f"target_amount は正の値が必要です: {target_amount}")
    if current < 0:
        raise ValueError(f"current は非負が必要です: {current}")
    if monthly_amount < 0:
        raise ValueError(f"monthly_amount は非負が必要です: {monthly_amount}")
    if current == 0 and monthly_amount == 0:
        raise ValueError("current と monthly_amount の少なくとも一方は正が必要です")
    n = _n_months(years)

    def fv(annual: float) -> float:
        return _deterministic_final_value(current, monthly_amount, annual, n)

    lo, hi = -0.9999, 1.0
    if fv(lo) >= target_amount:
        return lo  # 年率 -99.99% でも到達する（実質、運用リターン不要の領域）
    while fv(hi) < target_amount:
        hi *= 2.0
        if hi > 1e4:  # 年率 +1,000,000% でも届かない → 実質的に到達不能
            raise ValueError(
                "現実的な年率リターンでは目標に到達できません"
                f"（target={target_amount:,.0f}, current={current:,.0f}, "
                f"monthly={monthly_amount:,.0f}, years={years:g}）。"
                "積立額・期間・目標額の見直しが必要です"
            )
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if fv(mid) < target_amount:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2.0


def _deflator(n_months: int, annual_inflation: float) -> np.ndarray:
    """月次の物価デフレーター系列 $(1+\\pi)^{t/12}$（長さ n_months+1）を返す。"""
    if annual_inflation <= -1.0:
        raise ValueError(f"inflation は -1 より大きい必要があります: {annual_inflation}")
    t_years = np.arange(n_months + 1, dtype=float) / MONTHS_PER_YEAR
    return (1.0 + annual_inflation) ** t_years


# --------------------------------------------------------------------------
# 積立シミュレーション
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectionResult:
    """積立シミュレーションの結果。

    系列はすべて長さ ``n_months + 1``（月0 = 開始時点）。名目値。
    実質値（インフレ調整後）は ``系列 / deflator`` で得られる（:meth:`real`）。
    """

    monthly_amount: float
    years: float
    annual_return: float
    annual_vol: float
    initial: float
    inflation: float
    n_paths: int
    seed: int
    months: np.ndarray  #: 経過月数 0..n
    contributions: np.ndarray  #: 累計元本（初期資産 + 累計拠出）
    deterministic: np.ndarray  #: 決定論的複利の資産推移
    percentiles: dict[int, np.ndarray]  #: ファンチャート用（キー: 5/25/50/75/95）
    final_values: np.ndarray  #: 各モンテカルロパスの最終資産 (n_paths,)
    deflator: np.ndarray  #: 物価デフレーター $(1+\pi)^{t/12}$

    @property
    def total_contribution(self) -> float:
        """総元本（初期資産 + 全拠出額、名目）。"""
        return float(self.contributions[-1])

    @property
    def shortfall_prob(self) -> float:
        """名目の元本割れ確率 $P(\\text{最終資産} < \\text{総元本})$（モンテカルロ推定）。"""
        return float(np.mean(self.final_values < self.total_contribution))

    def real(self, series: np.ndarray) -> np.ndarray:
        """名目系列を実質値（現在の購買力換算）に割り引く。"""
        return series / self.deflator


def compound_projection(
    monthly_amount: float,
    years: float,
    annual_return: float,
    annual_vol: float,
    *,
    initial: float = 0.0,
    inflation: float = 0.0,
    n_paths: int = 2000,
    seed: int = 42,
) -> ProjectionResult:
    """毎月定額積立の資産推移を、決定論的複利とモンテカルロの両方で試算する。

    拠出は**月末払い**（その月の運用リターンが付いた後に拠出）とする:
    $V_{t+1} = V_t \\cdot G_t + P$。決定論では $G_t = (1+R)^{1/12}$（一定）、
    モンテカルロでは対数正規（:func:`_lognormal_params` のパラメータ化。
    期待値が決定論と一致し、中央値はボラティリティ・ドラッグの分だけ下回る）。

    Args:
        monthly_amount: 毎月の積立額（円、非負）。
        years: 積立年数（正。月数 = round(years × 12)）。
        annual_return: 想定年率リターン（0.05 = 5%）。
        annual_vol: 想定年率ボラティリティ（0.15 = 15%、非負。0 なら全パスが決定論と一致）。
        initial: 初期資産（円、非負）。
        inflation: 年率インフレ率（実質価値表示用。0 なら deflator は全て1）。
        n_paths: モンテカルロのパス数。
        seed: 乱数シード（固定すれば結果は完全に再現される）。

    Returns:
        :class:`ProjectionResult`。

    Raises:
        ValueError: 引数が定義域外の場合。
    """
    if monthly_amount < 0:
        raise ValueError(f"monthly_amount は非負が必要です: {monthly_amount}")
    if initial < 0:
        raise ValueError(f"initial は非負が必要です: {initial}")
    n = _n_months(years)
    r_m = monthly_rate(annual_return)

    # 決定論的複利（月末拠出の漸化式）
    det = np.empty(n + 1, dtype=float)
    det[0] = initial
    for t in range(n):
        det[t + 1] = det[t] * (1.0 + r_m) + monthly_amount

    # モンテカルロ（パス方向はベクトル化、月方向のみループ）
    gross = _simulate_gross_returns(annual_return, annual_vol, n, n_paths, seed)
    balances = np.empty((n_paths, n + 1), dtype=float)
    balances[:, 0] = initial
    for t in range(n):
        balances[:, t + 1] = balances[:, t] * gross[:, t] + monthly_amount

    months = np.arange(n + 1)
    return ProjectionResult(
        monthly_amount=monthly_amount,
        years=years,
        annual_return=annual_return,
        annual_vol=annual_vol,
        initial=initial,
        inflation=inflation,
        n_paths=n_paths,
        seed=seed,
        months=months,
        contributions=initial + monthly_amount * months.astype(float),
        deterministic=det,
        percentiles={p: np.percentile(balances, p, axis=0) for p in PERCENTILES},
        final_values=balances[:, -1].copy(),
        deflator=_deflator(n, inflation),
    )


def required_monthly_saving(
    target_amount: float,
    years: float,
    annual_return: float,
    *,
    initial: float = 0.0,
) -> float:
    """目標額から毎月の必要積立額を逆算する（決定論的複利、月末拠出）。

    導出: 月利 $r = (1+R)^{1/12} - 1$、月数 $n = 12y$ とすると、月末拠出 $P$ の
    将来価値は等比数列の和で

    $$FV = V_0 (1+r)^n + P \\sum_{k=0}^{n-1} (1+r)^k
         = V_0 (1+r)^n + P \\frac{(1+r)^n - 1}{r}$$

    これを $FV = \\text{target}$ について $P$ に関して解くと

    $$P = \\bigl(\\text{target} - V_0 (1+r)^n\\bigr) \\cdot
          \\frac{r}{(1+r)^n - 1}$$

    $r = 0$（リターン0）の極限では $P = (\\text{target} - V_0) / n$。
    初期資産の複利成長だけで目標に到達する場合は 0 を返す。

    Args:
        target_amount: 目標額（円、正）。
        years: 積立年数（正）。
        annual_return: 想定年率リターン（0.05 = 5%）。
        initial: 初期資産（円、非負）。

    Returns:
        毎月の必要積立額（円）。:func:`compound_projection` の決定論系列と
        往復整合する（同じ入力で最終額が target_amount に一致する）。

    Raises:
        ValueError: 引数が定義域外の場合。
    """
    if target_amount <= 0:
        raise ValueError(f"target_amount は正の値が必要です: {target_amount}")
    if initial < 0:
        raise ValueError(f"initial は非負が必要です: {initial}")
    n = _n_months(years)
    r = monthly_rate(annual_return)
    growth = (1.0 + r) ** n
    remaining = target_amount - initial * growth
    if remaining <= 0:
        return 0.0
    if r == 0.0:
        return remaining / n
    return remaining * r / (growth - 1.0)


# --------------------------------------------------------------------------
# 取り崩しシミュレーション
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DecumulationResult:
    """取り崩しシミュレーションの結果。

    系列はすべて長さ ``n_months + 1``（月0 = 取り崩し開始時点）。名目値。
    残高が一度 0 になったパスは以後 0 のまま（吸収状態）なので、
    ``depletion_prob_by_month`` は累積の枯渇確率になる。
    """

    initial: float
    years: float
    annual_return: float
    annual_vol: float
    inflation: float
    n_paths: int
    seed: int
    mode: Literal["fixed_amount", "fixed_rate"]
    monthly_withdrawal: float | None  #: 定額モードの月次引出額（円）
    annual_withdrawal_rate: float | None  #: 定率モードの年率引出率（0.04 = 4%）
    inflation_linked: bool  #: 定額モードで引出額をインフレ連動増額するか
    months: np.ndarray
    deterministic: np.ndarray  #: 一定リターン（決定論）での残高推移
    percentiles: dict[int, np.ndarray]  #: 残高のファンチャート用系列
    final_values: np.ndarray  #: 各パスの最終残高 (n_paths,)
    depletion_prob: float  #: 期間内に残高が枯渇する確率（モンテカルロ推定）
    depletion_prob_by_month: np.ndarray  #: 月次の累積枯渇確率（長さ n+1）
    depletion_month_median: float | None  #: 枯渇したパスの枯渇月の中央値（枯渇パスが無ければ None）
    worst_first_final: float  #: 同一リターン集合を「悪い順」に並べた場合の最終残高
    best_first_final: float  #: 同一リターン集合を「良い順」に並べた場合の最終残高
    withdrawal_median: np.ndarray | None  #: 定率モードの月次引出額の中央値系列（定額では None）
    deflator: np.ndarray

    def real(self, series: np.ndarray) -> np.ndarray:
        """名目系列を実質値（取り崩し開始時点の購買力換算）に割り引く。"""
        return series / self.deflator


def _run_decumulation_path(
    initial: float,
    gross: np.ndarray,
    mode: Literal["fixed_amount", "fixed_rate"],
    withdrawal_schedule: np.ndarray | None,
    rate_m: float | None,
) -> float:
    """単一のリターン系列に対する取り崩しの最終残高（シークエンス検証用）。"""
    balance = initial
    for t in range(gross.shape[0]):
        if mode == "fixed_amount":
            assert withdrawal_schedule is not None
            balance = max(balance * gross[t] - withdrawal_schedule[t], 0.0)
        else:
            assert rate_m is not None
            balance = balance * gross[t] * (1.0 - rate_m)
        if balance <= 0.0:
            return 0.0
    return balance


def decumulation_simulation(
    initial: float,
    years: float,
    annual_return: float,
    annual_vol: float,
    *,
    monthly_withdrawal: float | None = None,
    annual_withdrawal_rate: float | None = None,
    inflation: float = 0.0,
    inflation_linked: bool = False,
    n_paths: int = 2000,
    seed: int = 42,
) -> DecumulationResult:
    """資産の取り崩し（定額 / 定率）をモンテカルロで試算する。

    ``monthly_withdrawal``（定額）と ``annual_withdrawal_rate``（定率）は
    **どちらか一方だけ**を指定する。

    - **定額**: $V_{t+1} = \\max(V_t \\cdot G_t - W_t,\\ 0)$。
      ``inflation_linked=True`` なら $W_t = W (1+\\pi)^{t/12}$（実質一定の引出）。
      残高が 0 になったパスは枯渇（以後 0 のまま）。
    - **定率**: $V_{t+1} = V_t \\cdot G_t (1 - q/12)$（$q$ = 年率引出率）。
      残高に比例して引出額が減るため数学的に枯渇しないが、
      引出額（受取額）自体が減っていく点に注意（``withdrawal_median`` 参照）。

    シークエンス・オブ・リターンズ（リターンの順序）の影響: シードから生成した
    1本の月次リターン系列を「悪い順（worst-first）」と「良い順（best-first）」に
    並べ替えて同じ取り崩しを適用した最終残高を ``worst_first_final`` /
    ``best_first_final`` に返す。**両者のリターン集合（積 = 累積リターン）は同一**
    なのに、引出がある場合は序盤に下落が来るほど最終残高が小さくなる
    （積立とは逆に、取り崩し期は初期の下落が致命的になりうる）。

    Args:
        initial: 取り崩し開始時の資産（円、正）。
        years: 取り崩し期間（年、正）。
        annual_return: 想定年率リターン。
        annual_vol: 想定年率ボラティリティ（非負）。
        monthly_withdrawal: 定額モードの月次引出額（円、正）。
        annual_withdrawal_rate: 定率モードの年率引出率（0 < q < 12、通常 0.03〜0.05）。
        inflation: 年率インフレ率（実質価値表示・インフレ連動引出に使用）。
        inflation_linked: True かつ定額モードなら引出額をインフレ連動で増額する。
        n_paths: モンテカルロのパス数。
        seed: 乱数シード。

    Returns:
        :class:`DecumulationResult`。

    Raises:
        ValueError: 引数が定義域外、または定額・定率の指定が両方/どちらも無い場合。
    """
    if initial <= 0:
        raise ValueError(f"initial は正の値が必要です: {initial}")
    if (monthly_withdrawal is None) == (annual_withdrawal_rate is None):
        raise ValueError(
            "monthly_withdrawal（定額）と annual_withdrawal_rate（定率）は"
            "どちらか一方だけを指定してください"
        )
    n = _n_months(years)

    mode: Literal["fixed_amount", "fixed_rate"]
    withdrawal_schedule: np.ndarray | None = None
    rate_m: float | None = None
    if monthly_withdrawal is not None:
        if monthly_withdrawal <= 0:
            raise ValueError(f"monthly_withdrawal は正の値が必要です: {monthly_withdrawal}")
        mode = "fixed_amount"
        if inflation_linked:
            t_years = np.arange(n, dtype=float) / MONTHS_PER_YEAR
            withdrawal_schedule = monthly_withdrawal * (1.0 + inflation) ** t_years
        else:
            withdrawal_schedule = np.full(n, float(monthly_withdrawal))
    else:
        assert annual_withdrawal_rate is not None
        rate_m = annual_withdrawal_rate / MONTHS_PER_YEAR
        if not 0.0 < rate_m < 1.0:
            raise ValueError(
                f"annual_withdrawal_rate は 0 < q/12 < 1 の範囲が必要です: {annual_withdrawal_rate}"
            )
        mode = "fixed_rate"

    gross = _simulate_gross_returns(annual_return, annual_vol, n, n_paths, seed)
    balances = np.empty((n_paths, n + 1), dtype=float)
    balances[:, 0] = initial
    withdrawals: np.ndarray | None = None
    if mode == "fixed_rate":
        withdrawals = np.empty((n_paths, n), dtype=float)
    for t in range(n):
        if mode == "fixed_amount":
            assert withdrawal_schedule is not None
            balances[:, t + 1] = np.maximum(
                balances[:, t] * gross[:, t] - withdrawal_schedule[t], 0.0
            )
        else:
            assert rate_m is not None and withdrawals is not None
            grown = balances[:, t] * gross[:, t]
            withdrawals[:, t] = grown * rate_m
            balances[:, t + 1] = grown * (1.0 - rate_m)

    # 決定論（一定リターン）での残高推移
    r_m = monthly_rate(annual_return)
    det_gross = np.full(n, 1.0 + r_m)
    det = np.empty(n + 1, dtype=float)
    det[0] = initial
    for t in range(n):
        if mode == "fixed_amount":
            assert withdrawal_schedule is not None
            det[t + 1] = max(det[t] * det_gross[t] - withdrawal_schedule[t], 0.0)
        else:
            assert rate_m is not None
            det[t + 1] = det[t] * det_gross[t] * (1.0 - rate_m)

    # 枯渇統計（残高0 は吸収状態なので月次系列は累積確率になる）
    depleted_matrix = balances <= 0.0
    depleted_paths = depleted_matrix[:, -1]
    depletion_prob = float(np.mean(depleted_paths))
    depletion_prob_by_month = depleted_matrix.mean(axis=0)
    depletion_month_median: float | None = None
    if depleted_paths.any():
        first_zero = np.argmax(depleted_matrix[depleted_paths], axis=1)
        depletion_month_median = float(np.median(first_zero))

    # シークエンス・オブ・リターンズ: 同一リターン集合の並べ替え比較
    seq_gross = _simulate_gross_returns(annual_return, annual_vol, n, 1, seed + 1)[0]
    worst_first = np.sort(seq_gross)
    best_first = worst_first[::-1]
    worst_first_final = _run_decumulation_path(
        initial, worst_first, mode, withdrawal_schedule, rate_m
    )
    best_first_final = _run_decumulation_path(
        initial, best_first, mode, withdrawal_schedule, rate_m
    )

    return DecumulationResult(
        initial=initial,
        years=years,
        annual_return=annual_return,
        annual_vol=annual_vol,
        inflation=inflation,
        n_paths=n_paths,
        seed=seed,
        mode=mode,
        monthly_withdrawal=monthly_withdrawal,
        annual_withdrawal_rate=annual_withdrawal_rate,
        inflation_linked=bool(inflation_linked and mode == "fixed_amount"),
        months=np.arange(n + 1),
        deterministic=det,
        percentiles={p: np.percentile(balances, p, axis=0) for p in PERCENTILES},
        final_values=balances[:, -1].copy(),
        depletion_prob=depletion_prob,
        depletion_prob_by_month=depletion_prob_by_month,
        depletion_month_median=depletion_month_median,
        worst_first_final=worst_first_final,
        best_first_final=best_first_final,
        withdrawal_median=(
            np.median(withdrawals, axis=0) if withdrawals is not None else None
        ),
        deflator=_deflator(n, inflation),
    )


# --------------------------------------------------------------------------
# NISA 非課税メリット
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NisaBenefit:
    """NISA（非課税口座）と課税口座の税引後比較。金額はすべて円。"""

    gain: float  #: 運用益（元本を超えた部分。負なら損失）
    tax_rate: float  #: 課税口座の税率（既定 20.315%、2025年時点）
    tax_in_taxable: float  #: 課税口座で売却した場合の税額（損失なら 0）
    after_tax_gain_taxable: float  #: 課税口座の税引後運用益
    after_tax_gain_nisa: float  #: NISA の税引後運用益（= gain、非課税）
    benefit: float  #: 非課税メリット（= 課税口座での税額）


def nisa_tax_benefit(gain: float, *, tax_rate: float = TAX_RATE_TAXABLE) -> NisaBenefit:
    """運用益 ``gain`` に対する NISA の非課税メリットを課税口座と比較して定量化する。

    課税口座では売却時に運用益へ ``tax_rate``（既定 20.315% = 所得税15% +
    復興特別所得税0.315% + 住民税5%、2025年時点）が課される。NISA では非課税なので、
    メリット = 課税口座で払うはずだった税額 = $\\max(\\text{gain}, 0) \\times \\text{税率}$。

    注意（docstring のみ、計算には含めない）:

    - 損失（gain < 0）の場合は課税口座でも税額 0 でメリットは 0。さらに NISA の
      損失は課税口座との**損益通算・繰越控除ができない**（2025年時点）ため、
      損失時は NISA が不利になる非対称性がある。
    - 実際の税額は取得価額の計算方法・配当の課税方式等で変わりうる。

    Args:
        gain: 運用益（円。最終評価額 − 総元本）。
        tax_rate: 課税口座の税率（0 以上 1 未満）。

    Returns:
        :class:`NisaBenefit`。
    """
    if not 0.0 <= tax_rate < 1.0:
        raise ValueError(f"tax_rate は 0 以上 1 未満が必要です: {tax_rate}")
    tax = max(gain, 0.0) * tax_rate
    return NisaBenefit(
        gain=gain,
        tax_rate=tax_rate,
        tax_in_taxable=tax,
        after_tax_gain_taxable=gain - tax,
        after_tax_gain_nisa=gain,
        benefit=tax,
    )

"""ポートフォリオ管理モジュール。

保有銘柄 CSV（``code,shares,avg_cost,acquired_date,memo,fx_at_cost,account,
target_weight,manual_price,proxy_ticker``。memo・fx_at_cost・account・target_weight・
manual_price・proxy_ticker は任意）の読み込み・バリデーションと、ポートフォリオ評価
（現在値・損益・ウエイト・セクター配分・加重ベータ・相関行列・年率ボラティリティ・
ヒストリカル VaR・HHI 集中度・目標配分ドリフト・下落ストレス感応度）を提供する。

``fx_at_cost`` は取得時のクロス円レート（円/基準通貨、正の数）。入力した銘柄は
``--in-currency`` 指定時に損益も基準通貨建てで算出し、株価要因と為替要因に分解する
（:class:`BaseCurrencyPnl` 参照）。未入力の銘柄は損益を円建てのみとする現行設計を維持する。

``account`` は口座区分（``nisa_tsumitate`` / ``nisa_growth`` / ``taxable``。空欄・
列なしは taxable 扱い）。列がある場合のみ、レポートに「NISA口座状況」節
（口座区分別の内訳・NISA枠の使用状況・非課税メリット推計、:class:`NisaSummary`）を追加する。

``target_weight`` は目標ウエイト（%、0〜100）。入力する場合は**全行に入力し合計を
ほぼ100%にする**必要があり、列がある場合のみレポートに「目標配分とのドリフト」節
（銘柄別・セクター別の乖離%pt、目標回帰の機械的な調整額試算、閾値バンド判定、
:class:`DriftSummary`）を追加する。売買の推奨はしない（乖離の測定と機械的試算のみ）。

``manual_price`` は手入力の現在値（投資信託の基準価額や現金 ``1`` 円などを想定）。
入力した行は yfinance を引かず手入力値で評価に組み入れる（現金・国内投信を含む
家計の全体資産ビュー用）。手入力行に限り ``code`` は4桁銘柄コード以外の任意の識別子
（例: ``emaxis-slim-allcountry``、``cash``）を許容する。価格系列が無いため、
既定では β・年率ボラ・VaR・相関の計算対象外（手入力評価・リスク指標対象外）で、
手入力値の取得日・鮮度の管理はユーザーの責任となる。

``proxy_ticker`` は手入力行（``manual_price`` 入力行）専用の任意列で、連動対象とみなす
上場プロキシのティッカー（例: 全世界株投信 → ``2559.T``、TOPIX投信 → ``1306.T``。
どの銘柄を連動対象とみなすかは**ユーザーの判断**）を指定する。指定した行は、その
プロキシの価格系列で β・年率ボラ・VaR・相関・下落ストレス感応度に組み込まれる
（評価額は従来どおり ``manual_price`` で計算する）。これは**プロキシによる近似**であり、
信託報酬差・為替ヘッジ差・投信の基準価額とプロキシ終値の1営業日ズレは反映されない
（レポートに自動で注記される）。``proxy_ticker`` 未指定の手入力行は従来どおり
リスク指標対象外。

保有情報 CSV は ``data/portfolio.csv`` に置く想定（``data/`` は gitignore 対象のため、
個人の保有情報が誤ってコミットされない）。テンプレートは
``analysis/templates/portfolio-example.csv`` にある。
"""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from stocklib import currency, metrics, report
from stocklib.data import (
    REPO_ROOT,
    DataFetchError,
    fetch_info,
    fetch_prices,
    normalize_code,
)

REQUIRED_COLUMNS: tuple[str, ...] = ("code", "shares", "avg_cost", "acquired_date")
OPTIONAL_COLUMNS: tuple[str, ...] = (
    "memo", "fx_at_cost", "account", "target_weight", "manual_price", "proxy_ticker",
)

DEFAULT_UNIVERSE_CSV: Path = REPO_ROOT / "analysis" / "universe" / "liquid30.csv"
UNKNOWN_SECTOR: str = "不明"
#: 手入力評価行（``manual_price`` 入力行）のセクター表示名。
MANUAL_ASSET_SECTOR: str = "手入力資産（投信・現金等）"

#: 目標配分ドリフトの既定閾値バンド（絶対乖離、0.05 = ±5%pt）。
DEFAULT_DRIFT_BAND: float = 0.05
#: 下落ストレス感応度（β近似）の既定シナリオ（ベンチマーク騰落率）。
STRESS_SCENARIOS: tuple[float, ...] = (-0.10, -0.20, -0.30)
#: ``target_weight`` 合計の許容誤差（%pt）。合計が 100 ± この値に収まらなければエラー。
TARGET_WEIGHT_SUM_TOLERANCE_PT: float = 0.5

# --- 口座区分（CSV 任意列 account）と新NISA制度の定数 ---------------------------

ACCOUNT_NISA_TSUMITATE: str = "nisa_tsumitate"
ACCOUNT_NISA_GROWTH: str = "nisa_growth"
ACCOUNT_TAXABLE: str = "taxable"
VALID_ACCOUNTS: tuple[str, ...] = (
    ACCOUNT_NISA_TSUMITATE,
    ACCOUNT_NISA_GROWTH,
    ACCOUNT_TAXABLE,
)
NISA_ACCOUNTS: tuple[str, ...] = (ACCOUNT_NISA_TSUMITATE, ACCOUNT_NISA_GROWTH)
ACCOUNT_LABELS: dict[str, str] = {
    ACCOUNT_NISA_TSUMITATE: "NISAつみたて投資枠",
    ACCOUNT_NISA_GROWTH: "NISA成長投資枠",
    ACCOUNT_TAXABLE: "課税口座（特定・一般）",
}

#: 新NISA（2024年開始）の年間投資枠・生涯投資枠（簿価ベース、2024年制度）。
NISA_ANNUAL_LIMITS: dict[str, float] = {
    ACCOUNT_NISA_TSUMITATE: 1_200_000.0,  # つみたて投資枠 120万円/年
    ACCOUNT_NISA_GROWTH: 2_400_000.0,     # 成長投資枠 240万円/年
}
NISA_LIFETIME_LIMIT: float = 18_000_000.0        # 生涯投資枠 1,800万円（簿価残高方式）
NISA_LIFETIME_GROWTH_LIMIT: float = 12_000_000.0  # うち成長投資枠の上限 1,200万円

#: 上場株式の譲渡益・配当への申告分離課税率（所得税15% + 復興特別所得税0.315% +
#: 住民税5%、2025年時点）。非課税メリット推計に使う。
CAPITAL_GAINS_TAX_RATE: float = 0.20315


class PortfolioValidationError(ValueError):
    """ポートフォリオ CSV のバリデーション失敗を示す例外。

    メッセージに行番号付きのエラー一覧（``errors`` 属性と同内容）を含む。
    """

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors: list[str] = list(errors)
        super().__init__(
            "ポートフォリオ CSV のバリデーションに失敗しました:\n"
            + "\n".join(f"  - {e}" for e in self.errors)
        )


@dataclass(frozen=True)
class Position:
    """保有ポジション1件（CSV の1行に対応）。"""

    code: str
    shares: float
    avg_cost: float
    acquired_date: dt.date
    memo: str = ""
    #: 取得時のクロス円レート（円/基準通貨、正の数）。CSV の任意列 ``fx_at_cost``。
    #: None（列なし・空欄）なら基準通貨建て損益は計算しない（円建てのみ）。
    fx_at_cost: float | None = None
    #: 口座区分（CSV の任意列 ``account``）。``nisa_tsumitate`` / ``nisa_growth`` /
    #: ``taxable`` のいずれか。None（列なし・空欄）は taxable 扱い
    #: （:attr:`account_type` 参照）で、全銘柄 None ならレポートに NISA 節を出さない。
    account: str | None = None
    #: 目標ウエイト（CSV の任意列 ``target_weight``、% 入力を**割合（0〜1）に変換して**
    #: 保持する）。None（列なし・空欄）ならドリフト節は出さない。CSV 経由では
    #: 「全行入力 or 全行空欄」「合計 ≈ 100%」がバリデーションされる。
    target_weight: float | None = None
    #: 手入力の現在値（CSV の任意列 ``manual_price``。投信の基準価額・現金 1 円等）。
    #: 入力した行は yfinance を引かず、この値で評価する（リスク指標対象外）。
    manual_price: float | None = None
    #: 連動対象とみなす上場プロキシのティッカー（CSV の任意列 ``proxy_ticker``。
    #: ``manual_price`` 入力行専用。例: 全世界株投信 → ``2559.T``）。指定した行は
    #: プロキシの価格系列で β・年率ボラ・VaR・相関・下落ストレス感応度に組み込む
    #: （評価額は ``manual_price`` のまま）。None なら従来どおりリスク指標対象外。
    proxy_ticker: str | None = None

    @property
    def cost_value(self) -> float:
        """取得原価（``shares * avg_cost``）。"""
        return self.shares * self.avg_cost

    @property
    def account_type(self) -> str:
        """口座区分（未指定は ``taxable`` 扱い）。"""
        return self.account if self.account is not None else ACCOUNT_TAXABLE


@dataclass
class PositionValuation:
    """評価済みポジション（銘柄ごとの現在値・損益・ウエイト等）。"""

    code: str
    name: str
    sector: str
    shares: float
    avg_cost: float
    price: float
    cost_value: float
    market_value: float
    pnl: float
    pnl_pct: float
    weight: float
    beta: float
    memo: str = ""
    #: True なら ``manual_price`` による手入力評価（``proxy_ticker`` 指定が無い限り
    #: β・ボラ・VaR・相関の対象外）。
    manual: bool = False
    #: 目標ウエイト（割合、0〜1）。ドリフト節の計算に使う。None なら目標未設定。
    target_weight: float | None = None
    #: 手入力行のプロキシティッカー（β・リスク指標をプロキシ系列で近似した場合のみ）。
    proxy_ticker: str | None = None


@dataclass(frozen=True)
class BaseCurrencyPnl:
    """基準通貨建て損益の内訳（``fx_at_cost`` を入力した銘柄のみ計算する）。

    基準通貨建て取得原価 = 円建て取得原価 ÷ ``fx_at_cost``、
    基準通貨建て評価額 = 円建て評価額 ÷ 直近為替。損益率の恒等式
    $(1 + r^{B}) = (1 + r^{JPY}) / (1 + r^{FX})$（$r^{FX}$: 取得時→直近の為替変化率）
    に基づき、損益を

    - 株価要因 ``pnl_price`` = 円建て損益 ÷ 直近為替
    - 為替要因 ``pnl_fx`` = 残差 = 円建て取得原価 × (fx_at_cost/直近為替 − 1) ÷ fx_at_cost

    に分解する。``pnl == pnl_price + pnl_fx`` が（丸め誤差を除き）恒等的に成り立つ。

    Attributes:
        code: 銘柄コード。
        fx_at_cost: 取得時のクロス円レート（円/基準通貨、CSV 入力値）。
        cost_value: 基準通貨建て取得原価。
        market_value: 基準通貨建て評価額。
        pnl: 基準通貨建て損益（``market_value - cost_value``）。
        pnl_pct: 基準通貨建て損益率。
        pnl_price: うち株価要因。
        pnl_fx: うち為替要因。
    """

    code: str
    fx_at_cost: float
    cost_value: float
    market_value: float
    pnl: float
    pnl_pct: float
    pnl_price: float
    pnl_fx: float


@dataclass
class BaseCurrencyValuation:
    """ポートフォリオの基準通貨建て評価（海外投資家視点、``--in-currency``）。

    基準通貨建ての**評価額とリスク指標（年率ボラ・VaR）**は全銘柄で計算する。
    損益の基準通貨建て換算は、取得時のクロス円レート（CSV の任意列 ``fx_at_cost``）を
    入力した銘柄に限り行い、株価要因と為替要因に分解する（:class:`BaseCurrencyPnl`）。
    ``fx_at_cost`` の無い銘柄は損益を換算しない——現在為替での換算は購入時からの
    為替損益を無視した近似にしかならないため、円建てのみを正とする。

    Attributes:
        ccy: 基準通貨コード（``"USD"`` / ``"EUR"`` / ``"GBP"``）。
        fx_ticker: 換算に使ったクロス円ティッカー（``USDJPY=X`` / ``EURJPY=X`` 等）。
        fx_rate: 換算に使った直近のクロス円終値（円/基準通貨）。
        fx_change: 価格期間におけるクロス円レートの変化率（正=円安方向）。
        market_values: 銘柄コード → 基準通貨建て評価額。
        total_market_value: 基準通貨建て評価額合計。
        ann_vol: 基準通貨建てポートフォリオ日次リターンの年率ボラティリティ。
        var_95: 基準通貨建てヒストリカル VaR（95%、日次）。
        pnl_breakdown: 銘柄コード → :class:`BaseCurrencyPnl`（``fx_at_cost`` 入力銘柄のみ）。
        no_fx_at_cost: ``fx_at_cost`` 未入力のため損益を円建てのみとした銘柄コード。
    """

    fx_ticker: str
    fx_rate: float
    fx_change: float
    market_values: dict[str, float]
    total_market_value: float
    ann_vol: float
    var_95: float
    ccy: str = "USD"
    pnl_breakdown: dict[str, BaseCurrencyPnl] = field(default_factory=dict)
    no_fx_at_cost: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """基準通貨の日本語表示名（「ドル」「ユーロ」「ポンド」）。"""
        return currency.CURRENCY_LABELS.get(self.ccy.upper(), self.ccy.upper())


# 後方互換エイリアス（旧 USD 固定の名称）
UsdValuation = BaseCurrencyValuation


@dataclass(frozen=True)
class NisaAccountBreakdown:
    """口座区分1つ分の内訳（評価額・損益）。"""

    account: str
    n_positions: int
    cost_value: float
    market_value: float
    pnl: float

    @property
    def label(self) -> str:
        """口座区分の日本語表示名。"""
        return ACCOUNT_LABELS.get(self.account, self.account)

    @property
    def pnl_pct(self) -> float:
        """損益率（取得原価ゼロなら NaN）。"""
        return self.market_value / self.cost_value - 1.0 if self.cost_value > 0 else float("nan")


@dataclass
class NisaSummary:
    """NISA口座状況のサマリー（CSV に ``account`` 列がある場合のみ計算する）。

    使用率はいずれも**取得額（簿価 = ``shares × avg_cost``）ベース**。年間投資枠は
    ``acquired_date`` の暦年で集計し、つみたて投資枠120万円・成長投資枠240万円
    （2024年開始の新NISA制度）に対する使用率を出す。生涯投資枠は NISA 簿価合計を
    1,800万円（うち成長投資枠は1,200万円上限、簿価残高方式）と比べる。

    保有中の銘柄のみからの集計であり、売却済み分・投資信託等の買付は含まないため、
    実際の枠残高（金融機関側の管理値）とはずれうる点に注意。

    Attributes:
        breakdown: 口座区分別の内訳（保有のある区分のみ、tsumitate → growth → taxable の順）。
        annual_usage: 取得年 → {口座区分: 取得額（簿価）}。NISA 口座区分のみ。
        lifetime_used: NISA口座（つみたて+成長）の簿価合計。
        lifetime_growth_used: うち成長投資枠の簿価合計。
        nisa_pnl: NISA口座の含み損益合計（円）。
        tax_benefit_estimate: 非課税メリットの推計（円）。
            ``max(nisa_pnl, 0) × 20.315%``（2025年時点税率）——「課税口座で同じ含み益
            なら将来売却時に課税されうる額」の目安。含み損なら 0。
    """

    breakdown: list[NisaAccountBreakdown]
    annual_usage: dict[int, dict[str, float]]
    lifetime_used: float
    lifetime_growth_used: float
    nisa_pnl: float
    tax_benefit_estimate: float


def build_nisa_summary(
    positions: Sequence[Position],
    market_values: dict[str, float],
) -> NisaSummary | None:
    """``account`` 列に基づく NISA サマリーを計算する。

    全銘柄で ``account`` が未指定（None、列なし・空欄）なら None を返し、
    レポートに NISA 節を出さない（後方互換）。

    Args:
        positions: :func:`load_portfolio` が返すポジションのリスト。
        market_values: 銘柄コード → 円建て評価額。
    """
    if all(p.account is None for p in positions):
        return None

    # 口座区分別の内訳（保有のある区分のみ、定義順）
    breakdown: list[NisaAccountBreakdown] = []
    for account in (*NISA_ACCOUNTS, ACCOUNT_TAXABLE):
        in_account = [p for p in positions if p.account_type == account]
        if not in_account:
            continue
        cost = float(sum(p.cost_value for p in in_account))
        mv = float(sum(market_values[p.code] for p in in_account))
        breakdown.append(NisaAccountBreakdown(
            account=account,
            n_positions=len(in_account),
            cost_value=cost,
            market_value=mv,
            pnl=mv - cost,
        ))

    # 年間投資枠: 同一取得年の NISA 買付（簿価）を年間枠に対して集計
    annual_usage: dict[int, dict[str, float]] = {}
    for p in positions:
        if p.account_type not in NISA_ACCOUNTS:
            continue
        year_usage = annual_usage.setdefault(p.acquired_date.year, {})
        year_usage[p.account_type] = year_usage.get(p.account_type, 0.0) + p.cost_value
    annual_usage = dict(sorted(annual_usage.items()))

    nisa_positions = [p for p in positions if p.account_type in NISA_ACCOUNTS]
    lifetime_used = float(sum(p.cost_value for p in nisa_positions))
    lifetime_growth_used = float(sum(
        p.cost_value for p in nisa_positions if p.account_type == ACCOUNT_NISA_GROWTH
    ))
    nisa_pnl = float(sum(market_values[p.code] - p.cost_value for p in nisa_positions))
    return NisaSummary(
        breakdown=breakdown,
        annual_usage=annual_usage,
        lifetime_used=lifetime_used,
        lifetime_growth_used=lifetime_growth_used,
        nisa_pnl=nisa_pnl,
        tax_benefit_estimate=max(nisa_pnl, 0.0) * CAPITAL_GAINS_TAX_RATE,
    )


@dataclass(frozen=True)
class DriftEntry:
    """銘柄1つ分の目標配分ドリフト。

    Attributes:
        code: 銘柄コード（手入力行は任意の識別子）。
        name: 銘柄名。
        sector: セクター（手入力行は :data:`MANUAL_ASSET_SECTOR`）。
        account: 口座区分（``taxable`` 等。摩擦の注記に使う）。
        current_weight: 現状ウエイト（割合、0〜1）。
        target_weight: 目標ウエイト（割合、0〜1）。
        drift: 乖離 = 現状 − 目標（割合。正 = 目標比で過大）。
        trade_amount: 目標に戻すための機械的な調整額（円）
            = (目標 − 現状) × 評価額合計。正 = 買付相当・負 = 売却相当の**試算値**で、
            単元株制約・手数料・税・スリッページは考慮しない。
    """

    code: str
    name: str
    sector: str
    account: str
    current_weight: float
    target_weight: float
    drift: float
    trade_amount: float


@dataclass
class DriftSummary:
    """目標配分ドリフトのサマリー（CSV に ``target_weight`` 列がある場合のみ計算する）。

    Attributes:
        entries: 銘柄別ドリフト（保有明細と同じ順）。
        sector_drift: セクター別の ``(セクター, 現状ウエイト, 目標ウエイト, 乖離)``
            を乖離の絶対値の降順に並べたリスト。
        total_market_value: 調整額試算の基準にした円建て評価額合計。
        band: 閾値バンド（絶対乖離。0.05 = ±5%pt）。バンド内の乖離は放置し、
            超えたときだけ目標に戻す「バンド方式」の判定に使う
            （knowledge/math/portfolio-construction-in-practice.md のリバランス設計参照）。
    """

    entries: list[DriftEntry]
    sector_drift: list[tuple[str, float, float, float]]
    total_market_value: float
    band: float = DEFAULT_DRIFT_BAND

    @property
    def outside_band(self) -> list[DriftEntry]:
        """乖離の絶対値がバンドを超えている銘柄。"""
        return [e for e in self.entries if abs(e.drift) > self.band]

    @property
    def max_abs_drift(self) -> float:
        """乖離の絶対値の最大（割合）。"""
        return max((abs(e.drift) for e in self.entries), default=0.0)


def build_drift_summary(
    positions: Sequence[Position],
    valuations: Sequence[PositionValuation],
    *,
    band: float = DEFAULT_DRIFT_BAND,
) -> DriftSummary | None:
    """``target_weight`` 列に基づく目標配分ドリフトを計算する。

    全銘柄で ``target_weight`` が未指定（None、列なし）なら None を返し、
    レポートにドリフト節を出さない（後方互換）。

    Note:
        :func:`load_portfolio` が「全行入力 or 全行空欄」を強制するため、CSV 経由では
        一部入力は起きない。:class:`Position` を直接構築して一部の銘柄だけ
        ``target_weight`` を与えた場合、未指定の銘柄は目標 0% として機械計算される。

    Args:
        positions: :func:`load_portfolio` が返すポジションのリスト。
        valuations: :func:`evaluate_portfolio` 内で計算した評価済みポジション。
        band: 閾値バンド（絶対乖離。既定 :data:`DEFAULT_DRIFT_BAND` = ±5%pt）。
    """
    if all(p.target_weight is None for p in positions):
        return None

    total_mv = float(sum(v.market_value for v in valuations))
    by_code: dict[str, Position] = {p.code: p for p in positions}
    entries: list[DriftEntry] = []
    for v in valuations:
        pos = by_code[v.code]
        target = pos.target_weight if pos.target_weight is not None else 0.0
        current = v.weight if np.isfinite(v.weight) else 0.0
        entries.append(DriftEntry(
            code=v.code,
            name=v.name,
            sector=v.sector,
            account=pos.account_type,
            current_weight=current,
            target_weight=target,
            drift=current - target,
            trade_amount=(target - current) * total_mv,
        ))

    sector_current: dict[str, float] = {}
    sector_target: dict[str, float] = {}
    for e in entries:
        sector_current[e.sector] = sector_current.get(e.sector, 0.0) + e.current_weight
        sector_target[e.sector] = sector_target.get(e.sector, 0.0) + e.target_weight
    sector_drift = [
        (sec, sector_current[sec], sector_target[sec],
         sector_current[sec] - sector_target[sec])
        for sec in sorted(
            sector_current, key=lambda s: -abs(sector_current[s] - sector_target[s])
        )
    ]
    return DriftSummary(
        entries=entries,
        sector_drift=sector_drift,
        total_market_value=total_mv,
        band=band,
    )


@dataclass(frozen=True)
class StressScenarioResult:
    """下落ストレスシナリオ1つ分のβ近似試算。

    Attributes:
        market_drop: ベンチマーク騰落率（例: ``-0.10`` = 10%下落）。
        est_pnl: 推定損益（円）= $\\sum_i MV_i \\cdot \\beta_i \\cdot \\Delta m$
            （β を計算できる銘柄のみ。通常は負 = 推定損失）。
        est_value: 推定評価額（円）= 評価額合計 + ``est_pnl``
            （β不明の行は変動ゼロと仮定した近似）。
        est_pnl_pct: ``est_pnl`` ÷ 評価額合計。
    """

    market_drop: float
    est_pnl: float
    est_value: float
    est_pnl_pct: float


@dataclass
class StressSummary:
    """下落ストレス感応度（β近似）のサマリー。

    ベンチマークが一律に下落した場合の推定損失を
    $\\Delta V \\approx \\sum_i MV_i \\cdot \\beta_i \\cdot \\Delta m$ で機械的に試算する。
    **予測ではなくβ一定仮定の感応度試算**であり、実際のストレス時にはβ・相関が
    上昇しがちで損失を過小推定しうる（レポートに限界を自動で注記する）。

    Attributes:
        benchmark: β計算のベンチマーク（下落を仮定する対象）。
        scenarios: シナリオ別の試算結果（:class:`StressScenarioResult`）。
        total_market_value: 評価額合計（β不明の行を含む全体、円）。
        covered_market_value: β を計算できた銘柄の評価額合計（円）。
        covered_beta: β を計算できた銘柄の加重β（対象銘柄のウエイトで正規化）。
        excluded_codes: β不明のため対象外とした行（``proxy_ticker`` 未指定の
            手入力行等）。試算上は変動ゼロと仮定して据え置く。
        proxied: プロキシ系列でβを近似した手入力行の ``(code, proxy_ticker)``。
    """

    benchmark: str
    scenarios: list[StressScenarioResult]
    total_market_value: float
    covered_market_value: float
    covered_beta: float
    excluded_codes: list[str] = field(default_factory=list)
    proxied: list[tuple[str, str]] = field(default_factory=list)
    #: β を計算できた銘柄数。
    n_covered: int = 0
    #: 全銘柄数（β不明の行を含む）。
    n_total: int = 0


def build_stress_summary(
    valuations: Sequence[PositionValuation],
    *,
    benchmark: str,
    scenarios: Sequence[float] = STRESS_SCENARIOS,
) -> StressSummary:
    """評価済みポジションから下落ストレス感応度（β近似）を計算する。

    β が有限な銘柄（上場銘柄 + ``proxy_ticker`` 指定の手入力行）のみを
    $\\Delta V \\approx \\sum_i MV_i \\cdot \\beta_i \\cdot \\Delta m$ に組み込み、
    β不明の行（``proxy_ticker`` 未指定の手入力行等）は変動ゼロと仮定して据え置く。

    Args:
        valuations: :func:`evaluate_portfolio` 内で計算した評価済みポジション。
        benchmark: β計算に使ったベンチマーク。
        scenarios: ベンチマーク騰落率のリスト（既定 :data:`STRESS_SCENARIOS`）。
    """
    total_mv = float(sum(v.market_value for v in valuations))
    covered = [v for v in valuations if np.isfinite(v.beta)]
    excluded_codes = [v.code for v in valuations if not np.isfinite(v.beta)]
    covered_mv = float(sum(v.market_value for v in covered))
    beta_exposure = float(sum(v.market_value * v.beta for v in covered))
    results: list[StressScenarioResult] = []
    for drop in scenarios:
        est_pnl = beta_exposure * drop
        results.append(StressScenarioResult(
            market_drop=drop,
            est_pnl=est_pnl,
            est_value=total_mv + est_pnl,
            est_pnl_pct=est_pnl / total_mv if total_mv > 0 else float("nan"),
        ))
    return StressSummary(
        benchmark=benchmark,
        scenarios=results,
        total_market_value=total_mv,
        covered_market_value=covered_mv,
        covered_beta=beta_exposure / covered_mv if covered_mv > 0 else float("nan"),
        excluded_codes=excluded_codes,
        proxied=[(v.code, v.proxy_ticker) for v in valuations if v.proxy_ticker is not None],
        n_covered=len(covered),
        n_total=len(valuations),
    )


def build_input_warnings(positions: Sequence[Position]) -> list[str]:
    """入力内容の警告（エラーにしない注意喚起）を組み立てる。

    現在の検出項目:

    - ``account=nisa_tsumitate`` の行に上場銘柄コード（``manual_price`` 無し）がある —
      つみたて投資枠では個別株は購入できない（2024年制度）ため、口座区分の
      入力ミスの可能性としてレポートに警告を出す（集計はそのまま行う）。

    Args:
        positions: :func:`load_portfolio` が返すポジションのリスト。
    """
    warnings: list[str] = []
    stocks_in_tsumitate = [
        p.code for p in positions
        if p.account_type == ACCOUNT_NISA_TSUMITATE and p.manual_price is None
    ]
    if stocks_in_tsumitate:
        warnings.append(
            "`account=nisa_tsumitate` に上場銘柄コード（"
            + ", ".join(stocks_in_tsumitate)
            + "）が入力されている。**つみたて投資枠では個別株は購入できない**"
            "（対象は要件を満たす投資信託・一部ETFに限られる、2024年制度）ため、"
            "口座区分の入力ミス（`nisa_growth` / `taxable` の誤記）の可能性がある。"
            "エラーにはせず集計はそのまま行うが、実際の口座区分を確認すること。"
        )
    return warnings


@dataclass
class PortfolioReview:
    """ポートフォリオ全体の評価結果。:meth:`to_markdown` で Markdown 化できる。"""

    as_of: dt.date
    period: str
    benchmark: str
    synthetic: bool
    positions: list[PositionValuation]
    total_cost: float
    total_market_value: float
    total_pnl: float
    total_pnl_pct: float
    sector_weights: dict[str, float]
    portfolio_beta: float
    ann_vol: float
    var_95: float
    hhi: float
    hhi_interpretation: str
    correlation: pd.DataFrame = field(repr=False, default_factory=pd.DataFrame)
    # 基準通貨建て評価（USD/EUR/GBP）。属性名は後方互換のため ``usd`` のまま維持する。
    usd: BaseCurrencyValuation | None = None
    # NISA口座状況（CSV に account 列がある場合のみ。無ければ None で節ごと省略）。
    nisa: NisaSummary | None = None
    # 目標配分ドリフト（CSV に target_weight 列がある場合のみ。無ければ None で節ごと省略）。
    drift: DriftSummary | None = None
    # 下落ストレス感応度（β近似）。evaluate_portfolio が常に計算する。
    stress: StressSummary | None = None
    # 入力チェックの警告（つみたて投資枠に個別株コード等。エラーにしない注意喚起）。
    input_warnings: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        """レポート本文（Markdown、見出し ``##`` 以下）を生成する。

        タイトル・生成日時は含まないので、呼び出し側で
        :func:`stocklib.report.report_header` と組み合わせる。
        """
        lines: list[str] = []

        lines.append("## 保有明細")
        lines.append("")
        rows: list[list[object]] = []
        for p in self.positions:
            price_cell = report.fmt_num(p.price)
            if p.manual:
                price_cell += "※"
            rows.append([
                p.code,
                p.name,
                p.sector,
                report.fmt_num(p.shares, 0),
                report.fmt_num(p.avg_cost),
                price_cell,
                report.fmt_num(p.market_value, 0),
                report.fmt_num(p.pnl, 0),
                report.fmt_pct(p.pnl_pct),
                report.fmt_pct(p.weight),
                report.fmt_num(p.beta),
                p.memo or "-",
            ])
        lines.append(report.markdown_table(
            ["コード", "銘柄名", "セクター", "株数", "平均取得単価", "現在値",
             "評価額", "損益", "損益率", "ウエイト", f"β（vs {self.benchmark}）", "メモ"],
            rows,
        ))
        lines.append("")
        manual_codes = [p.code for p in self.positions if p.manual]
        proxied_rows = [
            (p.code, p.proxy_ticker) for p in self.positions
            if p.manual and p.proxy_ticker is not None
        ]
        unproxied_codes = [
            p.code for p in self.positions if p.manual and p.proxy_ticker is None
        ]
        if manual_codes:
            lines.append(
                "- ※ の現在値は CSV の任意列 `manual_price` による**手入力評価**"
                "（投信の基準価額・現金 1 円などを想定。対象: "
                + ", ".join(manual_codes)
                + "）。手入力値の取得日・鮮度の管理はユーザーの責任であり、"
                "他銘柄の直近終値と評価時点がずれうる。"
            )
            if proxied_rows:
                lines.append(
                    "- `proxy_ticker` 指定の手入力行（"
                    + ", ".join(f"{c} → {t}" for c, t in proxied_rows)
                    + "）は、β・年率ボラ・VaR・相関・下落ストレス感応度に"
                    "**プロキシの価格系列**で組み込む（評価額は従来どおり "
                    "`manual_price` で計算）。これは**プロキシによる近似"
                    "（信託報酬差・為替ヘッジ差・基準価額の1営業日ズレは反映されない）**"
                    "であり、連動対象の指定はユーザーの判断による。"
                )
            if unproxied_codes:
                lines.append(
                    "- 手入力行のうち `proxy_ticker` 未指定の行（対象: "
                    + ", ".join(unproxied_codes)
                    + "）は価格系列が無いため **β・年率ボラ・VaR・相関の計算対象外**"
                    "（手入力評価・リスク指標対象外）。全体サマリーのリスク指標は対象外の"
                    "行を除く銘柄のウエイトを再正規化して計算している"
                    "（ウエイト・HHI・セクター配分は手入力行を含む全体ベース）。"
                )
            lines.append("")

        if self.input_warnings:
            lines.append("### 入力チェックの警告")
            lines.append("")
            for w in self.input_warnings:
                lines.append(f"- **警告**: {w}")
            lines.append("")

        lines.append("## 全体サマリー")
        lines.append("")
        lines.append(report.markdown_table(
            ["項目", "値"],
            [
                ["取得原価合計", report.fmt_num(self.total_cost, 0)],
                ["評価額合計", report.fmt_num(self.total_market_value, 0)],
                ["評価損益合計", report.fmt_num(self.total_pnl, 0)],
                ["損益率", report.fmt_pct(self.total_pnl_pct)],
                [f"ポートフォリオβ（加重、vs {self.benchmark}）", report.fmt_num(self.portfolio_beta)],
                ["ポートフォリオ年率ボラティリティ", report.fmt_pct(self.ann_vol)],
                ["ヒストリカルVaR（95%、日次）", report.fmt_pct(self.var_95)],
                ["HHI 集中度（ウエイト二乗和）", report.fmt_num(self.hhi, 3)],
            ],
        ))
        lines.append("")
        lines.append(f"- HHI の解釈: {self.hhi_interpretation}")
        lines.append(
            "- 年率ボラ・VaR は「現在ウエイト固定」の日次リターン近似で計算しており、"
            "実際の取得タイミング・売買履歴は反映していない。"
        )
        lines.append("")

        if self.stress is not None:
            lines.append(self._stress_markdown())

        if self.drift is not None:
            lines.append(self._drift_markdown())

        if self.nisa is not None:
            lines.append(self._nisa_markdown())

        if self.usd is not None:
            u = self.usd
            label = u.label
            lines.append(f"## {label}建て評価（海外投資家視点）")
            lines.append("")
            lines.append(
                f"- 換算: {u.fx_ticker} の**同日終値・ヘッジなしの近似**"
                f"（直近 {u.fx_rate:.2f} 円/{label}、期間変動 {report.fmt_pct(u.fx_change)}）。"
                "リスク指標は円建て日次リターンを恒等式 "
                f"$(1 + r^{{{u.ccy}}}) = (1 + r^{{JPY}}) / (1 + r^{{FX}})$ で換算した系列から計算。"
            )
            if u.pnl_breakdown:
                lines.append(
                    f"- 損益（{u.ccy}）は CSV の任意列 `fx_at_cost`（取得時のクロス円レート、"
                    f"円/{u.ccy}）を入力した銘柄（{len(u.pnl_breakdown)}/{len(self.positions)} 銘柄）"
                    f"について算出: {label}建て取得原価 = 円建て取得原価 ÷ fx_at_cost、"
                    f"{label}建て評価額 = 円建て評価額 ÷ 直近為替。恒等式 "
                    f"$(1 + r^{{{u.ccy}}}) = (1 + r^{{JPY}}) / (1 + r^{{FX}})$ に基づき、"
                    "**うち株価要因 = 円建て損益 ÷ 直近為替**、**うち為替要因 = 残差**"
                    "（円建て取得原価 × (fx_at_cost/直近為替 − 1) ÷ fx_at_cost）に分解する"
                    "（両者の合計 = 損益）。"
                )
                if u.no_fx_at_cost:
                    lines.append(
                        "- **取得時為替未入力のため円建てのみ**（`fx_at_cost` が空欄で、"
                        f"損益の{label}建て換算をしない銘柄）: "
                        + ", ".join(u.no_fx_at_cost)
                        + "。現在為替での換算は購入時からの為替損益を無視した近似になるため行わない。"
                    )
            else:
                lines.append(
                    f"- **損益（P&L）の{label}建て換算は行わない**: 購入時点の為替レートが保有 CSV に"
                    "無いため、現在為替での損益換算は購入時からの為替損益を無視した近似にしか"
                    f"ならない。損益は円建て（上表）を正とし、{label}建ては評価額とリスク指標に限定する。"
                    "取得時のクロス円レートを CSV の任意列 `fx_at_cost` に入力すると、"
                    f"{label}建て損益と株価/為替要因の分解を併記できる。"
                )
            lines.append(
                "- ウエイト・セクター配分・HHI は同一為替レートで全銘柄を除すため"
                "円建てと同一（上表参照）。"
            )
            lines.append("")
            if u.pnl_breakdown:
                fx_rows: list[list[object]] = []
                for c, mv in u.market_values.items():
                    b = u.pnl_breakdown.get(c)
                    if b is None:
                        fx_rows.append([c, report.fmt_num(mv, 0), "-", "-", "-", "-", "-"])
                    else:
                        fx_rows.append([
                            c,
                            report.fmt_num(mv, 0),
                            report.fmt_num(b.fx_at_cost, 2),
                            report.fmt_num(b.cost_value, 0),
                            report.fmt_num(b.pnl, 0),
                            report.fmt_num(b.pnl_price, 0),
                            report.fmt_num(b.pnl_fx, 0),
                        ])
                lines.append(report.markdown_table(
                    ["コード", f"評価額（{u.ccy}）", "取得時為替", f"取得原価（{u.ccy}）",
                     f"損益（{u.ccy}）", "うち株価要因", "うち為替要因"],
                    fx_rows,
                ))
            else:
                fx_rows = [
                    [c, report.fmt_num(mv, 0)] for c, mv in u.market_values.items()
                ]
                lines.append(report.markdown_table(["コード", f"評価額（{u.ccy}）"], fx_rows))
            lines.append("")
            summary_rows: list[list[object]] = [
                [f"評価額合計（{u.ccy}）", report.fmt_num(u.total_market_value, 0)],
            ]
            if u.pnl_breakdown:
                summary_rows.extend([
                    [f"損益合計（{u.ccy}、fx_at_cost 入力 {len(u.pnl_breakdown)} 銘柄）",
                     report.fmt_num(sum(b.pnl for b in u.pnl_breakdown.values()), 0)],
                    ["うち株価要因合計",
                     report.fmt_num(sum(b.pnl_price for b in u.pnl_breakdown.values()), 0)],
                    ["うち為替要因合計",
                     report.fmt_num(sum(b.pnl_fx for b in u.pnl_breakdown.values()), 0)],
                ])
            summary_rows.extend([
                [f"ポートフォリオ年率ボラティリティ（{label}建て）", report.fmt_pct(u.ann_vol)],
                [f"ヒストリカルVaR（95%、日次、{label}建て）", report.fmt_pct(u.var_95)],
            ])
            lines.append(report.markdown_table(["項目", "値"], summary_rows))
            lines.append("")

        lines.append("## セクター配分")
        lines.append("")
        lines.append(report.markdown_table(
            ["セクター", "ウエイト"],
            [[sec, report.fmt_pct(w)] for sec, w in self.sector_weights.items()],
        ))
        lines.append("")

        lines.append("## 日次リターン相関行列")
        lines.append("")
        if len(self.correlation) >= 2:
            lines.append(report.df_to_markdown(self.correlation, digits=3, index_name="銘柄"))
            if proxied_rows:
                lines.append("")
                lines.append(
                    "- `proxy_ticker` 指定の手入力行（"
                    + ", ".join(f"{c} → {t}" for c, t in proxied_rows)
                    + "）の相関はプロキシの価格系列による近似。"
                )
        elif manual_codes:
            lines.append(
                "対象銘柄（`proxy_ticker` 無しの手入力行を除く）が1銘柄以下のため相関行列は省略。"
            )
        else:
            lines.append("保有が1銘柄のため相関行列は省略。")
        lines.append("")
        lines.append(
            "- 相関は期間依存・レジーム依存であり、市場ストレス時には上昇しがちである点に注意。"
        )
        lines.append("")
        return "\n".join(lines)

    def _stress_markdown(self) -> str:
        """「下落ストレス感応度（β近似）」節（Markdown）を生成する。``self.stress`` が前提。"""
        assert self.stress is not None
        s = self.stress
        lines: list[str] = []
        lines.append("## 下落ストレス感応度（β近似）")
        lines.append("")
        lines.append(
            f"ベンチマーク（{s.benchmark}）が一律に下落した場合の評価額の変化を、"
            "銘柄βの加重 $\\Delta V \\approx \\sum_i MV_i \\cdot \\beta_i \\cdot \\Delta m$"
            "（$MV_i$: 評価額、$\\beta_i$: β、$\\Delta m$: ベンチマーク騰落率）で"
            "機械的に試算する。**これは将来の予測ではなくβ一定仮定の感応度試算**であり、"
            "下落の発生確率・時期・幅について何も述べない。"
        )
        lines.append("")
        if s.n_covered == 0:
            lines.append(
                "β を計算できる銘柄が無い（全行が `proxy_ticker` 未指定の手入力評価）ため、"
                "本節の試算は省略する。投信等の手入力行に連動対象の上場プロキシ"
                "（CSV の任意列 `proxy_ticker`、例: 全世界株投信 → 2559.T）を指定すると"
                "組み込める（指定はユーザーの判断）。"
            )
            lines.append("")
            return "\n".join(lines)

        lines.append(report.markdown_table(
            ["シナリオ（ベンチマーク騰落）", "推定損失額 ΔV（円、負=損失）",
             "推定評価額（円）", "対評価額合計"],
            [
                [
                    f"{r.market_drop * 100:+.0f}%",
                    f"{r.est_pnl:+,.0f}",
                    report.fmt_num(r.est_value, 0),
                    report.fmt_pct(r.est_pnl_pct),
                ]
                for r in s.scenarios
            ],
        ))
        lines.append("")
        coverage = (
            s.covered_market_value / s.total_market_value
            if s.total_market_value > 0 else float("nan")
        )
        lines.append(
            f"- 対象: β を計算できる {s.n_covered}/{s.n_total} 銘柄"
            f"（評価額ベースで {report.fmt_pct(coverage, 1)}、"
            f"対象分の加重β {report.fmt_num(s.covered_beta)}）。"
        )
        if s.excluded_codes:
            lines.append(
                "- **β不明の手入力行は対象外**（`proxy_ticker` 未指定: "
                + ", ".join(s.excluded_codes)
                + "）。試算上は**変動ゼロと仮定して据え置く**近似であり、"
                "実際にはこれらの資産（投信・現金等）の価値も変動しうる。"
            )
        if s.proxied:
            lines.append(
                "- プロキシ近似の行（"
                + ", ".join(f"{c} → {t}" for c, t in s.proxied)
                + "）のβはプロキシの価格系列に基づく（信託報酬差・為替ヘッジ差・"
                "基準価額の1営業日ズレは反映されない）。"
            )
        lines.append(
            "- **参考（過去実績）**: 2008年のリーマン危機では TOPIX が年間で約4割下落"
            "（2008年時点の過去実績）、2020年のコロナショックでは約1ヶ月で約3割の急落"
            "（2020年時点の過去実績）となった。将来の下落幅の目安を保証するものではなく、"
            "値は各自検証のこと。"
        )
        lines.append(
            "- **限界**: 実際のストレス時にはβ・相関が上昇しがちで、β一定仮定の本試算は"
            "損失を**過小推定しうる**。また個別銘柄固有のリスク（決算・不祥事等）や"
            "為替・金利の変動はβに現れない。"
        )
        lines.append(
            "- **読み方**: 推定損失額を「この損失が起きても生活設計・積立の継続が"
            "壊れないか」（生活防衛資金・リスク受容力との突き合わせ）という観点で読む"
            "枠組みは knowledge/strategies/household-risk-capacity-and-allocation.md を"
            "参照。本節は安全性の判定（「この下落なら問題ない」等の宣言）はしない——"
            "判断材料の提示にとどめ、判断はユーザー自身が行う。"
        )
        lines.append("")
        return "\n".join(lines)

    def _drift_markdown(self) -> str:
        """「目標配分とのドリフト」節（Markdown）を生成する。``self.drift`` が前提。"""
        assert self.drift is not None
        d = self.drift
        band_pt = d.band * 100
        lines: list[str] = []
        lines.append("## 目標配分とのドリフト（リバランス支援）")
        lines.append("")
        lines.append(
            "CSV の任意列 `target_weight`（目標ウエイト%）に基づき、現状配分と目標配分の"
            "乖離を測定する。長期運用の中核規律は「リバランスと継続」であり"
            "（knowledge/strategies/investment-horizons-framework.md）、本節が行うのは"
            "**乖離の測定と機械的試算のみ**——どの銘柄を売買すべきかの判断や、"
            "目標配分そのものの妥当性評価はしない。"
        )
        lines.append("")

        lines.append("### 銘柄別ドリフト")
        lines.append("")
        drift_rows: list[list[object]] = []
        for e in d.entries:
            drift_rows.append([
                e.code,
                e.name,
                report.fmt_pct(e.current_weight, 1),
                report.fmt_pct(e.target_weight, 1),
                f"{e.drift * 100:+.2f}",
                "超過" if abs(e.drift) > d.band else "圏内",
                f"{e.trade_amount:+,.0f}",
            ])
        lines.append(report.markdown_table(
            ["コード", "銘柄名", "現状ウエイト", "目標ウエイト", "乖離（%pt）",
             f"バンド判定（±{band_pt:.1f}%pt）", "目標回帰の調整額（円・試算）"],
            drift_rows,
        ))
        lines.append("")
        n_out = len(d.outside_band)
        lines.append(
            f"- 最大乖離 {d.max_abs_drift * 100:.2f}%pt、バンド ±{band_pt:.1f}%pt 超過は "
            f"{n_out}/{len(d.entries)} 銘柄。"
        )
        lines.append(
            "- **調整額** = (目標ウエイト − 現状ウエイト) × 評価額合計"
            f"（{report.fmt_num(d.total_market_value, 0)} 円）。正 = 買付相当・"
            "負 = 売却相当の機械的試算であり、単元株制約（多くの東証銘柄は100株単位）・"
            "手数料・税・スリッページは考慮していない。目標合計が100%なら調整額の合計は"
            "ほぼゼロ（追加資金なしの入れ替え）になる。"
        )
        lines.append(
            f"- **閾値バンドの考え方**: カレンダー方式（月次・四半期）より、乖離が閾値を"
            "超えたときだけ目標に戻す「バンド方式」の方がコスト効率が良いという実証が多い"
            "（knowledge/math/portfolio-construction-in-practice.md のリバランス設計）。"
            f"±{band_pt:.1f}%pt は一例（絶対乖離バンド）であり、目標×±20% のような相対乖離"
            "バンドも使われる。最適なバンド幅は取引コスト・ボラティリティ・税に依存する。"
            "バンド内の乖離は「放置する」のが方式の前提。"
        )
        lines.append("")

        lines.append("### セクター別ドリフト")
        lines.append("")
        lines.append(report.markdown_table(
            ["セクター", "現状ウエイト", "目標ウエイト", "乖離（%pt）"],
            [
                [sec, report.fmt_pct(cur, 1), report.fmt_pct(tgt, 1), f"{dr * 100:+.2f}"]
                for sec, cur, tgt, dr in d.sector_drift
            ],
        ))
        lines.append("")

        lines.append("### リバランスの摩擦（実行前に確認するコスト）")
        lines.append("")
        lines.append(
            "- **課税口座（taxable）の売却相当額**: 含み益部分に 20.315%"
            "（申告分離課税率、2025年時点）の課税が発生しうる。乖離を戻すコストとして"
            "調整額とあわせて考慮する。"
        )
        lines.append(
            "- **NISA口座の売却相当額**: 年間投資枠（つみたて120万円・成長240万円）は"
            "売却しても**当年中は復活しない**。生涯投資枠（1,800万円、簿価残高方式）は"
            "売却の**翌年**に簿価分が復活する（2024年制度）。NISA枠内での入れ替えは"
            "枠の消費を伴う点に注意。"
        )
        lines.append(
            "- 新規拠出資金の買付配分を目標比で過小な資産に寄せる方法（売却を伴わない"
            "リバランス）は、上記の税・枠の摩擦を避けられることが知られている。"
            "どの方法を採るか・そもそも戻すかはユーザー自身の判断。"
        )
        lines.append("")
        return "\n".join(lines)

    def _nisa_markdown(self) -> str:
        """「NISA口座状況」節（Markdown）を生成する。``self.nisa`` が前提。"""
        assert self.nisa is not None
        n = self.nisa
        lines: list[str] = []
        lines.append("## NISA口座状況")
        lines.append("")
        lines.append(
            "CSV の `account` 列（`nisa_tsumitate` / `nisa_growth` / `taxable`、"
            "空欄は taxable 扱い）に基づく集計。枠の金額は2024年開始の新NISA制度。"
        )
        lines.append("")

        lines.append("### 口座区分別の内訳")
        lines.append("")
        lines.append(report.markdown_table(
            ["口座区分", "銘柄数", "取得額（簿価）", "評価額", "評価損益", "損益率"],
            [
                [
                    b.label,
                    b.n_positions,
                    report.fmt_num(b.cost_value, 0),
                    report.fmt_num(b.market_value, 0),
                    report.fmt_num(b.pnl, 0),
                    report.fmt_pct(b.pnl_pct),
                ]
                for b in n.breakdown
            ],
        ))
        lines.append("")

        lines.append("### NISA枠の使用状況（取得額＝簿価ベース）")
        lines.append("")
        if n.annual_usage:
            annual_rows: list[list[object]] = []
            for year, usage in n.annual_usage.items():
                row: list[object] = [year]
                for account in NISA_ACCOUNTS:
                    used = usage.get(account, 0.0)
                    limit = NISA_ANNUAL_LIMITS[account]
                    row.append(report.fmt_num(used, 0))
                    row.append(report.fmt_pct(used / limit, 1))
                annual_rows.append(row)
            lines.append(report.markdown_table(
                ["取得年", "つみたて投資枠 取得額", "対 年間枠120万円",
                 "成長投資枠 取得額", "対 年間枠240万円"],
                annual_rows,
            ))
            lines.append("")
        lines.append(report.markdown_table(
            ["生涯投資枠（簿価残高方式）", "使用額（簿価）", "使用率"],
            [
                [
                    "全体（上限1,800万円）",
                    report.fmt_num(n.lifetime_used, 0),
                    report.fmt_pct(n.lifetime_used / NISA_LIFETIME_LIMIT, 1),
                ],
                [
                    "うち成長投資枠（上限1,200万円）",
                    report.fmt_num(n.lifetime_growth_used, 0),
                    report.fmt_pct(n.lifetime_growth_used / NISA_LIFETIME_GROWTH_LIMIT, 1),
                ],
            ],
        ))
        lines.append("")

        lines.append("### 非課税メリットの推計")
        lines.append("")
        if n.nisa_pnl > 0:
            lines.append(
                f"- NISA口座の含み益 {report.fmt_num(n.nisa_pnl, 0)} 円 × 20.315%"
                "（申告分離課税率、2025年時点）= "
                f"**約 {report.fmt_num(n.tax_benefit_estimate, 0)} 円**。"
                "課税口座で同じ含み益を実現した場合に課税されうる額の目安であり、"
                "確定した節税額ではない（売却時の株価で変動する）。"
            )
        else:
            lines.append(
                f"- NISA口座の含み損益は {report.fmt_num(n.nisa_pnl, 0)} 円（含み益なし）の"
                "ため、非課税メリット（含み益 × 20.315%、2025年時点の申告分離課税率）の"
                "推計は 0 円。含み損の場合、NISA では損失を他口座の利益と損益通算"
                "できない点がむしろデメリットになる（下記注意）。"
            )
        lines.append("")
        lines.append(
            "- **制度上の注意（2024年制度）**: NISA口座の損失は課税口座との損益通算・"
            "繰越控除ができない。年間投資枠は買付額（簿価）ベースで売却しても当年中は"
            "復活せず、生涯投資枠は売却の**翌年**に簿価分が復活する。"
        )
        lines.append(
            "- 本節は保有中の銘柄のみからの集計であり、売却済み分・投資信託等の買付を"
            "含まない。実際の枠残高は金融機関の管理画面で確認すること。"
        )
        lines.append("")
        return "\n".join(lines)


def interpret_hhi(hhi: float) -> str:
    """HHI 集中度（ウエイト二乗和 $\\mathrm{HHI} = \\sum_i w_i^2$）の解釈文を返す。

    均等ウエイト $n$ 銘柄なら $\\mathrm{HHI} = 1/n$ なので、
    $1/\\mathrm{HHI}$ を「実効銘柄数」として併記する。
    """
    if not np.isfinite(hhi) or hhi <= 0.0:
        return "計算不能（評価額が取得できていない可能性）"
    effective_n = 1.0 / hhi
    if hhi < 0.10:
        level = "分散的（均等10銘柄超に相当）"
    elif hhi < 0.18:
        level = "中程度の集中（均等6〜10銘柄に相当）"
    elif hhi < 0.25:
        level = "やや高い集中（均等4〜6銘柄に相当）"
    else:
        level = "高い集中（均等4銘柄未満に相当。個別銘柄リスクが支配的）"
    return f"HHI = {hhi:.3f}、実効銘柄数 ≈ {effective_n:.1f}。{level}"


def _parse_row(row: dict[str, str], lineno: int, errors: list[str]) -> Position | None:
    """CSV の1行を :class:`Position` に変換する。不正なら ``errors`` に追記して None。"""
    row_errors: list[str] = []

    # manual_price（任意列）: 手入力の現在値。code の形式チェックより先に解釈する
    # （手入力行に限り 4桁コード以外の識別子を許容するため）。
    manual_price: float | None = None
    raw_manual = (row.get("manual_price") or "").strip()
    if raw_manual:  # 任意列: 空欄なら None のまま（yfinance で評価する現行動作）
        try:
            manual_value = float(raw_manual)
        except ValueError:
            row_errors.append(f"{lineno}行目: manual_price {raw_manual!r} を数値に変換できません")
        else:
            if not np.isfinite(manual_value) or manual_value <= 0:
                row_errors.append(
                    f"{lineno}行目: manual_price は正の数（手入力の現在値。投信の基準価額や"
                    f"現金 1 など）を指定してください（{raw_manual!r}）"
                )
            else:
                manual_price = manual_value

    code = (row.get("code") or "").strip()
    if not code:
        row_errors.append(f"{lineno}行目: code が空です")
    elif manual_price is None and not normalize_code(code).endswith(".T"):
        row_errors.append(
            f"{lineno}行目: code {code!r} が銘柄コード形式（4桁数字 or 英字入り4文字）では"
            "ありません（投信・現金など非上場資産は manual_price 列に手入力の現在値を"
            "指定すると、任意の識別子を code に使えます）"
        )

    shares = float("nan")
    raw_shares = (row.get("shares") or "").strip()
    try:
        shares = float(raw_shares)
    except ValueError:
        row_errors.append(f"{lineno}行目: shares {raw_shares!r} を数値に変換できません")
    else:
        if not np.isfinite(shares) or shares <= 0:
            row_errors.append(f"{lineno}行目: shares は正の数を指定してください（{raw_shares!r}）")

    avg_cost = float("nan")
    raw_cost = (row.get("avg_cost") or "").strip()
    try:
        avg_cost = float(raw_cost)
    except ValueError:
        row_errors.append(f"{lineno}行目: avg_cost {raw_cost!r} を数値に変換できません")
    else:
        if not np.isfinite(avg_cost) or avg_cost <= 0:
            row_errors.append(f"{lineno}行目: avg_cost は正の数を指定してください（{raw_cost!r}）")

    acquired = dt.date.min
    raw_date = (row.get("acquired_date") or "").strip()
    try:
        acquired = dt.date.fromisoformat(raw_date)
    except ValueError:
        row_errors.append(
            f"{lineno}行目: acquired_date {raw_date!r} を日付（YYYY-MM-DD）として解釈できません"
        )
    else:
        if acquired > dt.date.today():
            row_errors.append(f"{lineno}行目: acquired_date {raw_date!r} が未来の日付です")

    fx_at_cost: float | None = None
    raw_fx = (row.get("fx_at_cost") or "").strip()
    if raw_fx:  # 任意列: 空欄なら None のまま（円建てのみの現行動作）
        try:
            fx_value = float(raw_fx)
        except ValueError:
            row_errors.append(f"{lineno}行目: fx_at_cost {raw_fx!r} を数値に変換できません")
        else:
            if not np.isfinite(fx_value) or fx_value <= 0:
                row_errors.append(
                    f"{lineno}行目: fx_at_cost は正の数（取得時のクロス円レート、"
                    f"円/基準通貨）を指定してください（{raw_fx!r}）"
                )
            else:
                fx_at_cost = fx_value

    account: str | None = None
    raw_account = (row.get("account") or "").strip()
    if raw_account:  # 任意列: 空欄なら None のまま（taxable 扱い）
        normalized_account = raw_account.lower()
        if normalized_account not in VALID_ACCOUNTS:
            row_errors.append(
                f"{lineno}行目: account {raw_account!r} は "
                f"{' / '.join(VALID_ACCOUNTS)} のいずれかを指定してください"
                "（空欄は taxable 扱い）"
            )
        else:
            account = normalized_account

    target_weight: float | None = None
    raw_target = (row.get("target_weight") or "").strip()
    if raw_target:  # 任意列: 空欄なら None のまま（ドリフト節なしの現行動作）
        try:
            target_value = float(raw_target)
        except ValueError:
            row_errors.append(
                f"{lineno}行目: target_weight {raw_target!r} を数値に変換できません"
            )
        else:
            if not np.isfinite(target_value) or target_value < 0 or target_value > 100:
                row_errors.append(
                    f"{lineno}行目: target_weight は 0〜100 の数値（目標ウエイト%）を"
                    f"指定してください（{raw_target!r}）"
                )
            else:
                target_weight = target_value / 100.0  # % → 割合

    proxy_ticker: str | None = None
    raw_proxy = (row.get("proxy_ticker") or "").strip()
    if raw_proxy:  # 任意列: 空欄なら None のまま（リスク指標対象外の現行動作）
        if manual_price is None:
            row_errors.append(
                f"{lineno}行目: proxy_ticker {raw_proxy!r} は manual_price（手入力評価）行"
                "でのみ指定できます（上場銘柄はその銘柄自身の価格系列でリスク指標を"
                "計算するためプロキシ不要）"
            )
        else:
            proxy_ticker = raw_proxy

    if row_errors:
        errors.extend(row_errors)
        return None
    return Position(
        code=code,
        shares=shares,
        avg_cost=avg_cost,
        acquired_date=acquired,
        memo=(row.get("memo") or "").strip(),
        fx_at_cost=fx_at_cost,
        account=account,
        target_weight=target_weight,
        manual_price=manual_price,
        proxy_ticker=proxy_ticker,
    )


def load_portfolio(path: str | Path) -> list[Position]:
    """ポートフォリオ CSV を読み込み、バリデーション済みの :class:`Position` リストを返す。

    CSV の列は ``code,shares,avg_cost,acquired_date,memo,fx_at_cost,account,
    target_weight,manual_price,proxy_ticker``（memo・fx_at_cost・account・
    target_weight・manual_price・proxy_ticker は省略可）。``fx_at_cost`` は取得時のクロス円レート
    （円/基準通貨、正の数）で、列自体が無い・空欄の銘柄は ``None``（円建てのみ）。
    ``account`` は口座区分（``nisa_tsumitate`` / ``nisa_growth`` / ``taxable``、
    大文字小文字不問）で、列自体が無い・空欄の銘柄は ``None``（taxable 扱い）。
    ``target_weight`` は目標ウエイト（%、0〜100）。入力する場合は**全行に入力**し、
    合計が 100% ± :data:`TARGET_WEIGHT_SUM_TOLERANCE_PT` %pt に収まる必要がある
    （現金を残す配分は現金行を ``manual_price=1`` で追加して表現する）。
    ``manual_price`` は手入力の現在値（正の数。投信の基準価額・現金 1 円等）で、
    入力した行に限り ``code`` は4桁銘柄コード以外の任意の識別子を許容する。
    ``proxy_ticker`` は ``manual_price`` 入力行専用（連動対象とみなす上場プロキシの
    ティッカー。指定した行はプロキシの価格系列でリスク指標に組み込まれる）で、
    ``manual_price`` の無い行に指定するとエラー。
    不正な行はすべて集約し、行番号付きで :class:`PortfolioValidationError` として報告する。

    Raises:
        FileNotFoundError: ファイルが存在しない場合。
        PortfolioValidationError: 列不足・値不正・銘柄コード重複・データ行なしの場合。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"ポートフォリオ CSV が見つかりません: {path}")

    errors: list[str] = []
    positions: list[Position] = []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = [c.strip() for c in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
        if missing:
            raise PortfolioValidationError(
                [f"ヘッダー行: 必須列が不足しています: {', '.join(missing)}"
                 f"（必須: {', '.join(REQUIRED_COLUMNS)}、任意: {', '.join(OPTIONAL_COLUMNS)}）"]
            )
        for row in reader:
            row = {(k or "").strip(): (v or "") for k, v in row.items() if k is not None}
            if not any(v.strip() for v in row.values()):
                continue  # 空行はスキップ
            pos = _parse_row(row, reader.line_num, errors)
            if pos is not None:
                positions.append(pos)

    seen: dict[str, str] = {}
    for pos in positions:
        key = normalize_code(pos.code)
        if key in seen:
            errors.append(
                f"銘柄コード {pos.code} が重複しています（1銘柄1行に集約してください）"
            )
        seen[key] = pos.code

    # target_weight の横断チェック（行単位のエラーが無いときのみ。一部の行だけの
    # 入力や合計の過不足は、ドリフト・調整額試算を誤らせるためエラーにする）
    if not errors and positions:
        with_target = [p for p in positions if p.target_weight is not None]
        if with_target and len(with_target) != len(positions):
            missing = [p.code for p in positions if p.target_weight is None]
            errors.append(
                "target_weight は全行に入力するか、全行空欄にしてください"
                f"（未入力: {', '.join(missing)}。現金を残す配分は code=cash 等の行を "
                "manual_price=1 で追加して表現できます）"
            )
        elif with_target:
            total_pt = sum(p.target_weight or 0.0 for p in with_target) * 100.0
            if abs(total_pt - 100.0) > TARGET_WEIGHT_SUM_TOLERANCE_PT:
                errors.append(
                    f"target_weight の合計が {total_pt:.1f}% です"
                    f"（100% ± {TARGET_WEIGHT_SUM_TOLERANCE_PT}%pt に収めてください。"
                    "現金を目標配分に含める場合は code=cash 等の行を manual_price=1 で"
                    "追加してください）"
                )

    if errors:
        raise PortfolioValidationError(errors)
    if not positions:
        raise PortfolioValidationError(["データ行がありません（ヘッダーのみの CSV です）"])
    return positions


def _load_universe_meta(universe_csv: Path = DEFAULT_UNIVERSE_CSV) -> dict[str, tuple[str, str]]:
    """ユニバース CSV（code,name,sector）から ``コード → (銘柄名, セクター)`` を読み込む。"""
    meta: dict[str, tuple[str, str]] = {}
    if not universe_csv.exists():
        return meta
    df = pd.read_csv(universe_csv, comment="#", dtype={"code": str})
    for rec in df.to_dict("records"):
        code = str(rec.get("code", "")).strip()
        if code:
            meta[normalize_code(code)] = (
                str(rec.get("name", "")).strip(),
                str(rec.get("sector", "")).strip(),
            )
    return meta


def _resolve_name_sector(code: str, *, synthetic: bool) -> tuple[str, str]:
    """銘柄名とセクターを解決する。

    ``analysis/universe/liquid30.csv``（33業種・日本語）を優先し、無ければ
    :func:`stocklib.data.fetch_info` のセクターで補完、それでも不明なら「不明」。
    """
    meta = _load_universe_meta()
    name, sector = meta.get(normalize_code(code), ("", ""))
    if name and sector:
        return name, sector
    try:
        info = fetch_info(code, synthetic=synthetic)
    except DataFetchError:
        info = {}
    name = name or str(info.get("名称", "")) or code
    sector = sector or str(info.get("セクター", "")) or UNKNOWN_SECTOR
    return name, sector


def evaluate_portfolio(
    positions: Sequence[Position],
    *,
    period: str = "1y",
    benchmark: str = "^N225",
    synthetic: bool = False,
    in_usd: bool = False,
    in_currency: str | None = None,
    drift_band: float = DEFAULT_DRIFT_BAND,
) -> PortfolioReview:
    """保有ポジションを評価し、:class:`PortfolioReview` を返す。

    銘柄ごとに現在値（直近終値）・評価額・損益・損益率・ウエイト・対ベンチマークβを計算し、
    ポートフォリオ全体では合計損益・セクター配分・加重β・日次リターン相関行列・
    年率ボラティリティ・ヒストリカル VaR(95%)・HHI 集中度を計算する。
    ``account`` 列（口座区分）を持つポジションが1つでもあれば、NISA口座状況
    （:class:`NisaSummary`、:func:`build_nisa_summary`）も計算して ``nisa`` 属性に付与する。
    ``target_weight``（目標ウエイト）を持つポジションが1つでもあれば、目標配分ドリフト
    （:class:`DriftSummary`、:func:`build_drift_summary`）も計算して ``drift`` 属性に付与する。

    ``manual_price``（手入力の現在値）を持つポジションは yfinance を引かず手入力値で
    評価する（投信・現金等の非上場資産を含む全体資産ビュー用）。手入力行のうち
    ``proxy_ticker``（連動対象とみなす上場プロキシ）を持つ行は、プロキシの価格系列で
    β・年率ボラ・VaR・相関・下落ストレス感応度に組み込む（評価額は ``manual_price``
    のまま。信託報酬差・為替ヘッジ差・基準価額の1営業日ズレは反映されない近似）。
    ``proxy_ticker`` の無い手入力行はβを NaN とし、ポートフォリオの加重β・年率ボラ・
    VaR・相関は**対象外の行を除く銘柄のウエイトを再正規化して**計算する（ウエイト・
    HHI・セクター配分は手入力行を含む全体ベース）。全ポジションがプロキシ無しの
    手入力の場合、これらのリスク指標は NaN になる（価格取得を行わないため
    ネットワーク不要で動く）。

    下落ストレス感応度（:class:`StressSummary`、:func:`build_stress_summary`）と
    入力チェックの警告（:func:`build_input_warnings`。``account=nisa_tsumitate`` に
    上場銘柄コードがある場合等）は常に計算し、``stress`` / ``input_warnings`` 属性に
    付与する。

    ボラ・VaR は「現在ウエイト固定」の日次リターン
    $r_{p,t} = \\sum_i w_i r_{i,t}$ による近似（取得タイミングは反映しない）。

    Args:
        positions: :func:`load_portfolio` が返すポジションのリスト。
        period: 価格取得期間（yfinance 形式、既定 ``"1y"``）。
        benchmark: β計算のベンチマーク（既定 ``"^N225"``）。
        synthetic: True なら合成データで評価（ネットワーク不要）。
        drift_band: 目標配分ドリフトの閾値バンド（絶対乖離。既定
            :data:`DEFAULT_DRIFT_BAND` = 0.05 = ±5%pt。``target_weight`` が
            1つも無ければ使われない）。
        in_usd: True なら ``in_currency="USD"`` と同義（後方互換エイリアス。
            ``in_currency`` 指定時はそちらが優先）。
        in_currency: 基準通貨コード（``"USD"`` / ``"EUR"`` / ``"GBP"``）。指定すると
            :class:`BaseCurrencyValuation`（基準通貨建ての評価額・年率ボラ・VaR）を
            ``usd`` 属性に付与する。さらに ``fx_at_cost``（取得時のクロス円レート、
            CSV の任意列）を持つ銘柄については損益も基準通貨建てで算出し、株価要因と
            為替要因に分解する（:class:`BaseCurrencyPnl`）。``fx_at_cost`` の無い銘柄の
            損益は円建てのみ（購入時為替なしでの換算は近似になるため行わない。
            :class:`BaseCurrencyValuation` の docstring 参照）。為替は同日終値・
            ヘッジなしの近似。``fx_at_cost`` は指定した基準通貨のクロス円レートで
            入力されている前提（USD 建て評価に EURJPY の値を混ぜない）。

    Raises:
        ValueError: positions が空の場合。
        DataFetchError: 価格取得に失敗した場合。
    """
    if not positions:
        raise ValueError("positions が空です（load_portfolio の結果を渡してください）")

    codes = [p.code for p in positions]
    market_positions = [p for p in positions if p.manual_price is None]
    manual_positions = [p for p in positions if p.manual_price is not None]
    market_codes = [p.code for p in market_positions]
    proxy_tickers = [
        p.proxy_ticker for p in manual_positions if p.proxy_ticker is not None
    ]

    # 価格取得は上場銘柄 + 手入力行のプロキシ（proxy_ticker）のみ
    # （プロキシ無しの全行手入力ならネットワーク不要）
    fetch_codes = list(dict.fromkeys([*market_codes, *proxy_tickers]))
    prices: dict[str, pd.DataFrame] = {}
    if fetch_codes:
        prices = fetch_prices([*fetch_codes, benchmark], period=period, synthetic=synthetic)
        bench_rets = metrics.daily_returns(prices[benchmark]["Close"])
    else:
        bench_rets = pd.Series(dtype=float)

    # 銘柄ごとの評価（手入力行は manual_price をそのまま現在値とする）
    last_prices: dict[str, float] = {
        c: float(prices[c]["Close"].iloc[-1]) for c in market_codes
    }
    for pos in manual_positions:
        assert pos.manual_price is not None
        last_prices[pos.code] = pos.manual_price
    market_values = {p.code: p.shares * last_prices[p.code] for p in positions}
    total_mv = float(sum(market_values.values()))
    total_cost = float(sum(p.cost_value for p in positions))

    valuations: list[PositionValuation] = []
    for pos in positions:
        mv = market_values[pos.code]
        is_manual = pos.manual_price is not None
        if is_manual:
            # 手入力行: 名称は code、セクターは固定ラベル。proxy_ticker があれば
            # βをプロキシの価格系列で近似し、無ければ NaN（リスク指標対象外）。
            name, sector = pos.code, MANUAL_ASSET_SECTOR
            if pos.proxy_ticker is not None:
                proxy_rets = metrics.daily_returns(prices[pos.proxy_ticker]["Close"])
                beta_value = metrics.beta(proxy_rets, bench_rets)
            else:
                beta_value = float("nan")
        else:
            rets = metrics.daily_returns(prices[pos.code]["Close"])
            name, sector = _resolve_name_sector(pos.code, synthetic=synthetic)
            beta_value = metrics.beta(rets, bench_rets)
        valuations.append(PositionValuation(
            code=pos.code,
            name=name,
            sector=sector,
            shares=pos.shares,
            avg_cost=pos.avg_cost,
            price=last_prices[pos.code],
            cost_value=pos.cost_value,
            market_value=mv,
            pnl=mv - pos.cost_value,
            pnl_pct=mv / pos.cost_value - 1.0,
            weight=mv / total_mv if total_mv > 0 else float("nan"),
            beta=beta_value,
            memo=pos.memo,
            manual=is_manual,
            target_weight=pos.target_weight,
            proxy_ticker=pos.proxy_ticker if is_manual else None,
        ))

    # セクター配分（ウエイト降順）
    sector_weights: dict[str, float] = {}
    for v in valuations:
        sector_weights[v.sector] = sector_weights.get(v.sector, 0.0) + v.weight
    sector_weights = dict(sorted(sector_weights.items(), key=lambda kv: -kv[1]))

    # ポートフォリオ日次リターン（現在ウエイト固定）と相関行列。
    # リスク指標の対象 = 上場銘柄 + proxy_ticker 指定の手入力行（プロキシ系列で近似）。
    # proxy_ticker 未指定の手入力行は対象外とし、対象銘柄のウエイトを再正規化する。
    risk_valuations = [v for v in valuations if not v.manual or v.proxy_ticker is not None]
    excluded_valuations = [v for v in valuations if v.manual and v.proxy_ticker is None]
    risk_weight_arr = np.array([v.weight for v in risk_valuations])
    risk_weight_sum = float(np.nansum(risk_weight_arr))
    if risk_valuations:
        closes = pd.concat(
            [
                prices[v.proxy_ticker if v.proxy_ticker is not None else v.code]["Close"]
                for v in risk_valuations
            ],
            axis=1,
            keys=[v.code for v in risk_valuations],
        ).dropna()
        returns = closes.pct_change().dropna()
        if excluded_valuations and risk_weight_sum > 0:
            risk_weights = risk_weight_arr / risk_weight_sum
        else:
            risk_weights = risk_weight_arr
        port_rets = pd.Series(returns.to_numpy() @ risk_weights, index=returns.index)
        ann_vol_value = metrics.ann_vol(port_rets)
        var_95_value = metrics.var_historical(port_rets, 0.95)
        correlation = metrics.correlation_matrix(returns)
    else:
        closes = pd.DataFrame()
        port_rets = pd.Series(dtype=float)
        ann_vol_value = float("nan")
        var_95_value = float("nan")
        correlation = pd.DataFrame()

    weights_all = np.array([v.weight for v in valuations])
    hhi = float(np.nansum(weights_all ** 2))
    portfolio_beta = float(np.nansum([v.weight * v.beta for v in risk_valuations]))
    if excluded_valuations:
        portfolio_beta = (
            portfolio_beta / risk_weight_sum if risk_weight_sum > 0 else float("nan")
        )

    ccy: str | None = in_currency if in_currency is not None else ("USD" if in_usd else None)
    usd: BaseCurrencyValuation | None = None
    if ccy is not None:
        ccy = ccy.strip().upper()
        fx_df = currency.fetch_fx(ccy, period, synthetic=synthetic)
        if len(closes) > 0:
            fx_aligned = currency.align_fx(closes.index, fx_df["Close"])
        else:
            fx_aligned = fx_df["Close"]  # 全行手入力評価: 株価系列が無いので為替系列そのまま
        fx_last = float(fx_aligned.iloc[-1])
        if len(port_rets) > 0:
            port_rets_base = currency.to_base_returns(port_rets, fx_df["Close"])
            ann_vol_base = metrics.ann_vol(port_rets_base)
            var_95_base = metrics.var_historical(port_rets_base, 0.95)
        else:
            ann_vol_base = float("nan")
            var_95_base = float("nan")
        pnl_breakdown: dict[str, BaseCurrencyPnl] = {}
        no_fx_at_cost: list[str] = []
        for pos in positions:
            if pos.fx_at_cost is None:
                no_fx_at_cost.append(pos.code)
                continue
            cost_base = pos.cost_value / pos.fx_at_cost
            mv_base = market_values[pos.code] / fx_last
            # 恒等式 (1+r_B) = (1+r_JPY)/(1+r_FX) に基づく分解:
            # 株価要因 = 円建て損益 ÷ 直近為替、為替要因 = 残差（合計 = 損益）
            pnl_price = (market_values[pos.code] - pos.cost_value) / fx_last
            pnl_fx = pos.cost_value * (pos.fx_at_cost / fx_last - 1.0) / pos.fx_at_cost
            pnl_breakdown[pos.code] = BaseCurrencyPnl(
                code=pos.code,
                fx_at_cost=pos.fx_at_cost,
                cost_value=cost_base,
                market_value=mv_base,
                pnl=mv_base - cost_base,
                pnl_pct=mv_base / cost_base - 1.0,
                pnl_price=pnl_price,
                pnl_fx=pnl_fx,
            )
        usd = BaseCurrencyValuation(
            fx_ticker=currency.get_fx_ticker(ccy),
            fx_rate=fx_last,
            fx_change=float(fx_aligned.iloc[-1] / fx_aligned.iloc[0] - 1.0),
            market_values={c: market_values[c] / fx_last for c in codes},
            total_market_value=total_mv / fx_last,
            ann_vol=ann_vol_base,
            var_95=var_95_base,
            ccy=ccy,
            pnl_breakdown=pnl_breakdown,
            no_fx_at_cost=no_fx_at_cost,
        )

    return PortfolioReview(
        as_of=dt.date.today(),
        period=period,
        benchmark=benchmark,
        synthetic=synthetic,
        positions=valuations,
        total_cost=total_cost,
        total_market_value=total_mv,
        total_pnl=total_mv - total_cost,
        total_pnl_pct=total_mv / total_cost - 1.0 if total_cost > 0 else float("nan"),
        sector_weights=sector_weights,
        portfolio_beta=portfolio_beta,
        ann_vol=ann_vol_value,
        var_95=var_95_value,
        hhi=hhi,
        hhi_interpretation=interpret_hhi(hhi),
        correlation=correlation,
        usd=usd,
        nisa=build_nisa_summary(positions, market_values),
        drift=build_drift_summary(positions, valuations, band=drift_band),
        stress=build_stress_summary(valuations, benchmark=benchmark),
        input_warnings=build_input_warnings(positions),
    )

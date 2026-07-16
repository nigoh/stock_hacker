"""ポートフォリオ管理モジュール。

保有銘柄 CSV（``code,shares,avg_cost,acquired_date,memo,fx_at_cost``。memo と
fx_at_cost は任意）の読み込み・バリデーションと、ポートフォリオ評価（現在値・損益・
ウエイト・セクター配分・加重ベータ・相関行列・年率ボラティリティ・
ヒストリカル VaR・HHI 集中度）を提供する。

``fx_at_cost`` は取得時のクロス円レート（円/基準通貨、正の数）。入力した銘柄は
``--in-currency`` 指定時に損益も基準通貨建てで算出し、株価要因と為替要因に分解する
（:class:`BaseCurrencyPnl` 参照）。未入力の銘柄は損益を円建てのみとする現行設計を維持する。

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
OPTIONAL_COLUMNS: tuple[str, ...] = ("memo", "fx_at_cost")

DEFAULT_UNIVERSE_CSV: Path = REPO_ROOT / "analysis" / "universe" / "liquid30.csv"
UNKNOWN_SECTOR: str = "不明"


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

    @property
    def cost_value(self) -> float:
        """取得原価（``shares * avg_cost``）。"""
        return self.shares * self.avg_cost


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
            rows.append([
                p.code,
                p.name,
                p.sector,
                report.fmt_num(p.shares, 0),
                report.fmt_num(p.avg_cost),
                report.fmt_num(p.price),
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
        else:
            lines.append("保有が1銘柄のため相関行列は省略。")
        lines.append("")
        lines.append(
            "- 相関は期間依存・レジーム依存であり、市場ストレス時には上昇しがちである点に注意。"
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

    code = (row.get("code") or "").strip()
    if not code:
        row_errors.append(f"{lineno}行目: code が空です")
    elif not normalize_code(code).endswith(".T"):
        row_errors.append(
            f"{lineno}行目: code {code!r} が銘柄コード形式（4桁数字 or 英字入り4文字）ではありません"
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
    )


def load_portfolio(path: str | Path) -> list[Position]:
    """ポートフォリオ CSV を読み込み、バリデーション済みの :class:`Position` リストを返す。

    CSV の列は ``code,shares,avg_cost,acquired_date,memo,fx_at_cost``
    （memo・fx_at_cost は省略可）。``fx_at_cost`` は取得時のクロス円レート
    （円/基準通貨、正の数）で、列自体が無い・空欄の銘柄は ``None``（円建てのみ）。
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
) -> PortfolioReview:
    """保有ポジションを評価し、:class:`PortfolioReview` を返す。

    銘柄ごとに現在値（直近終値）・評価額・損益・損益率・ウエイト・対ベンチマークβを計算し、
    ポートフォリオ全体では合計損益・セクター配分・加重β・日次リターン相関行列・
    年率ボラティリティ・ヒストリカル VaR(95%)・HHI 集中度を計算する。

    ボラ・VaR は「現在ウエイト固定」の日次リターン
    $r_{p,t} = \\sum_i w_i r_{i,t}$ による近似（取得タイミングは反映しない）。

    Args:
        positions: :func:`load_portfolio` が返すポジションのリスト。
        period: 価格取得期間（yfinance 形式、既定 ``"1y"``）。
        benchmark: β計算のベンチマーク（既定 ``"^N225"``）。
        synthetic: True なら合成データで評価（ネットワーク不要）。
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
    prices = fetch_prices([*codes, benchmark], period=period, synthetic=synthetic)
    bench_rets = metrics.daily_returns(prices[benchmark]["Close"])

    # 銘柄ごとの評価
    last_prices = {c: float(prices[c]["Close"].iloc[-1]) for c in codes}
    market_values = {c: p.shares * last_prices[c] for c, p in zip(codes, positions)}
    total_mv = float(sum(market_values.values()))
    total_cost = float(sum(p.cost_value for p in positions))

    valuations: list[PositionValuation] = []
    for pos in positions:
        mv = market_values[pos.code]
        rets = metrics.daily_returns(prices[pos.code]["Close"])
        name, sector = _resolve_name_sector(pos.code, synthetic=synthetic)
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
            beta=metrics.beta(rets, bench_rets),
            memo=pos.memo,
        ))

    # セクター配分（ウエイト降順）
    sector_weights: dict[str, float] = {}
    for v in valuations:
        sector_weights[v.sector] = sector_weights.get(v.sector, 0.0) + v.weight
    sector_weights = dict(sorted(sector_weights.items(), key=lambda kv: -kv[1]))

    # ポートフォリオ日次リターン（現在ウエイト固定）と相関行列
    closes = pd.concat({c: prices[c]["Close"] for c in codes}, axis=1).dropna()
    returns = closes.pct_change().dropna()
    weights = np.array([v.weight for v in valuations])
    port_rets = pd.Series(returns.to_numpy() @ weights, index=returns.index)

    hhi = float(np.nansum(weights ** 2))
    portfolio_beta = float(np.nansum([v.weight * v.beta for v in valuations]))

    ccy: str | None = in_currency if in_currency is not None else ("USD" if in_usd else None)
    usd: BaseCurrencyValuation | None = None
    if ccy is not None:
        ccy = ccy.strip().upper()
        fx_df = currency.fetch_fx(ccy, period, synthetic=synthetic)
        fx_aligned = currency.align_fx(closes.index, fx_df["Close"])
        fx_last = float(fx_aligned.iloc[-1])
        port_rets_base = currency.to_base_returns(port_rets, fx_df["Close"])
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
            ann_vol=metrics.ann_vol(port_rets_base),
            var_95=metrics.var_historical(port_rets_base, 0.95),
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
        ann_vol=metrics.ann_vol(port_rets),
        var_95=metrics.var_historical(port_rets, 0.95),
        hhi=hhi,
        hhi_interpretation=interpret_hhi(hhi),
        correlation=metrics.correlation_matrix(returns),
        usd=usd,
    )

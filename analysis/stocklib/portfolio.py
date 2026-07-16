"""ポートフォリオ管理モジュール。

保有銘柄 CSV（``code,shares,avg_cost,acquired_date,memo``）の読み込み・バリデーションと、
ポートフォリオ評価（現在値・損益・ウエイト・セクター配分・加重ベータ・相関行列・
年率ボラティリティ・ヒストリカル VaR・HHI 集中度）を提供する。

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

from stocklib import metrics, report
from stocklib.data import (
    REPO_ROOT,
    DataFetchError,
    fetch_info,
    fetch_prices,
    normalize_code,
)

REQUIRED_COLUMNS: tuple[str, ...] = ("code", "shares", "avg_cost", "acquired_date")
OPTIONAL_COLUMNS: tuple[str, ...] = ("memo",)

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

    if row_errors:
        errors.extend(row_errors)
        return None
    return Position(
        code=code,
        shares=shares,
        avg_cost=avg_cost,
        acquired_date=acquired,
        memo=(row.get("memo") or "").strip(),
    )


def load_portfolio(path: str | Path) -> list[Position]:
    """ポートフォリオ CSV を読み込み、バリデーション済みの :class:`Position` リストを返す。

    CSV の列は ``code,shares,avg_cost,acquired_date,memo``（memo のみ省略可）。
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
    )

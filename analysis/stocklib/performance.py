r"""実運用パフォーマンス計測モジュール（取引履歴 → 金額加重リターン XIRR）。

取引履歴 CSV（買付・売却・配当・入出金）から、投資家自身のキャッシュフローに基づく
**金額加重リターン（MWR = XIRR、ニュートン法 + 二分法フォールバック）**、
期間損益（実現 + 未実現 + 受取配当）、および**同じキャッシュフローをベンチマーク
（^N225 や 1306.T）に投じた場合の比較**（ベンチマーク相当 XIRR）を計算する。
価格は :func:`stocklib.data.fetch_prices` を再利用し、``synthetic=True`` で
ネットワーク不要の合成データでも動作する。CLI は ``analysis/performance_report.py``、
CSV テンプレートは ``analysis/templates/transactions-example.csv``
（運用時は gitignore 済みの ``data/transactions.csv`` に置く）。

XIRR は次を満たす年率 $r$（不定期キャッシュフローの内部収益率）:

$$\sum_{i} \frac{CF_i}{(1+r)^{t_i}} = 0, \qquad t_i = \frac{d_i - d_0}{365.25}$$

キャッシュフローの符号は投資家視点（投下 = 負、回収 = 正）で、最終フローとして
評価日の終端評価額（保有時価 + 現金残高）を正のフローに加える。

計測モードは取引履歴の内容から自動判定する:

- **口座モード**: ``deposit`` / ``withdraw`` 行が1つでもあれば、入出金を外部
  キャッシュフローとし、買付・売却・配当は口座内の資金移動（現金残高で追跡）と
  みなす。証券口座への入出金を記録している人向けで、現金の遊びも含めた
  「口座全体の実感リターン」になる。
- **ポジションモード**: 入出金の記録が無ければ、買付（負）・売却（正）・配当（正）
  そのものを外部キャッシュフローとみなす。約定履歴だけを記録している人向け。

理論的背景（TWR と MWR の使い分け・ベンチマーク選択・巧拙と運の統計的検定）は
``knowledge/math/performance-measurement-and-attribution.md`` を参照。
"""

from __future__ import annotations

import csv
import datetime as dt
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import pandas as pd

from stocklib.data import fetch_prices, normalize_code
from stocklib.report import fmt_num, fmt_pct, markdown_table

VALID_SIDES: tuple[str, ...] = ("buy", "sell", "dividend", "deposit", "withdraw")
CASH_SIDES: frozenset[str] = frozenset({"deposit", "withdraw"})
REQUIRED_COLUMNS: tuple[str, ...] = ("date", "code", "side", "shares", "price")
OPTIONAL_COLUMNS: tuple[str, ...] = ("fee", "account", "memo")

DEFAULT_BENCHMARK: str = "^N225"

_EPS: float = 1e-9


class TransactionValidationError(ValueError):
    """取引履歴 CSV の検証エラー（全行のエラーを集約して報告する）。"""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors: list[str] = list(errors)
        super().__init__("\n".join(self.errors))


@dataclass(frozen=True)
class Transaction:
    """取引履歴の1行。

    Attributes:
        date: 取引日。
        side: ``buy`` / ``sell`` / ``dividend`` / ``deposit`` / ``withdraw``。
        code: 銘柄コード（``deposit`` / ``withdraw`` は空文字）。
        shares: 株数（``dividend`` / ``deposit`` / ``withdraw`` の省略時は 1.0）。
        price: 単価（円）。``dividend`` は1株配当、``deposit`` / ``withdraw`` は
            ``shares=1`` のまま金額そのものを入れる想定。
        fee: 手数料・税（円、非負）。``buy`` は取得原価に算入、``sell`` / ``dividend``
            は受取額から控除（配当の源泉徴収税をここに計上できる）。
        account: 口座メモ（任意の自由記述。例: ``taxable`` / ``nisa_growth``）。
        memo: 自由記述メモ。
    """

    date: dt.date
    side: str
    code: str
    shares: float
    price: float
    fee: float = 0.0
    account: str = ""
    memo: str = ""

    @property
    def gross_amount(self) -> float:
        """約定代金・受取総額（``shares × price``、手数料控除前）。"""
        return self.shares * self.price

    @property
    def cash_delta(self) -> float:
        """口座の現金残高への影響（円、正 = 現金増）。"""
        if self.side == "buy":
            return -(self.gross_amount + self.fee)
        if self.side in ("sell", "dividend", "deposit"):
            return self.gross_amount - self.fee
        return -(self.gross_amount + self.fee)  # withdraw


def _parse_row(row: dict[str, str], lineno: int, errors: list[str]) -> Transaction | None:
    """CSV の1行を :class:`Transaction` に変換する。不正なら ``errors`` に追記して None。"""
    row_errors: list[str] = []

    side = (row.get("side") or "").strip().lower()
    if side not in VALID_SIDES:
        row_errors.append(
            f"{lineno}行目: side {row.get('side')!r} は {' / '.join(VALID_SIDES)} の"
            "いずれかを指定してください"
        )

    date = dt.date.min
    raw_date = (row.get("date") or "").strip()
    try:
        date = dt.date.fromisoformat(raw_date)
    except ValueError:
        row_errors.append(
            f"{lineno}行目: date {raw_date!r} を日付（YYYY-MM-DD）として解釈できません"
        )
    else:
        if date > dt.date.today():
            row_errors.append(f"{lineno}行目: date {raw_date!r} が未来の日付です")

    code = (row.get("code") or "").strip()
    if side in CASH_SIDES:
        if code:
            row_errors.append(
                f"{lineno}行目: side={side} の行に code {code!r} が入っています"
                "（入出金は銘柄と無関係のため code は空欄にしてください）"
            )
    elif side in VALID_SIDES:
        if not code:
            row_errors.append(f"{lineno}行目: side={side} には code（銘柄コード）が必須です")
        elif not normalize_code(code).endswith(".T"):
            row_errors.append(
                f"{lineno}行目: code {code!r} が銘柄コード形式（4桁数字 or 英字入り4文字）"
                "ではありません（本ツールの対象は yfinance で価格を引ける東証上場銘柄・ETF）"
            )

    shares = 1.0
    raw_shares = (row.get("shares") or "").strip()
    if raw_shares:
        try:
            shares = float(raw_shares)
        except ValueError:
            row_errors.append(f"{lineno}行目: shares {raw_shares!r} を数値に変換できません")
        else:
            if not math.isfinite(shares) or shares <= 0:
                row_errors.append(
                    f"{lineno}行目: shares は正の数を指定してください（{raw_shares!r}）"
                )
    elif side in ("buy", "sell"):
        row_errors.append(
            f"{lineno}行目: side={side} には shares（株数）が必須です"
            "（dividend/deposit/withdraw のみ省略可、既定 1）"
        )

    price = float("nan")
    raw_price = (row.get("price") or "").strip()
    try:
        price = float(raw_price)
    except ValueError:
        row_errors.append(f"{lineno}行目: price {raw_price!r} を数値に変換できません")
    else:
        if not math.isfinite(price) or price <= 0:
            row_errors.append(f"{lineno}行目: price は正の数を指定してください（{raw_price!r}）")

    fee = 0.0
    raw_fee = (row.get("fee") or "").strip()
    if raw_fee:
        try:
            fee = float(raw_fee)
        except ValueError:
            row_errors.append(f"{lineno}行目: fee {raw_fee!r} を数値に変換できません")
        else:
            if not math.isfinite(fee) or fee < 0:
                row_errors.append(
                    f"{lineno}行目: fee は 0 以上の数を指定してください（{raw_fee!r}）"
                )

    if row_errors:
        errors.extend(row_errors)
        return None
    return Transaction(
        date=date,
        side=side,
        code=code,
        shares=shares,
        price=price,
        fee=fee,
        account=(row.get("account") or "").strip(),
        memo=(row.get("memo") or "").strip(),
    )


def load_transactions(path: str | Path) -> list[Transaction]:
    """取引履歴 CSV を読み込み、日付順（同日内は記載順）の取引リストを返す。

    CSV の列は ``date,code,side,shares,price,fee,account,memo``
    （fee・account・memo は省略可）。列の意味:

    - ``date``: 取引日（YYYY-MM-DD、未来日不可）
    - ``side``: ``buy`` / ``sell`` / ``dividend`` / ``deposit`` / ``withdraw``
    - ``code``: buy/sell/dividend は4桁銘柄コード必須。deposit/withdraw は空欄
    - ``shares``: buy/sell は必須。dividend は保有株数（``shares × price`` = 受取
      総額になるよう入力。総額を直接入れる場合は空欄 + price に総額でもよい）、
      deposit/withdraw は空欄（既定 1）で ``price`` に金額を入れる
    - ``price``: 単価（円、正の数）
    - ``fee``: 手数料・税（円、0 以上。buy は取得原価に算入、sell/dividend は
      受取から控除。配当の源泉徴収税もここに計上できる）

    Raises:
        FileNotFoundError: ファイルが存在しない場合。
        TransactionValidationError: 列不足・値不正・データ行なしの場合。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"取引履歴 CSV が見つかりません: {path}")

    errors: list[str] = []
    transactions: list[Transaction] = []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = [c.strip() for c in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
        if missing:
            raise TransactionValidationError(
                [f"ヘッダー行: 必須列が不足しています: {', '.join(missing)}"
                 f"（必須: {', '.join(REQUIRED_COLUMNS)}、任意: {', '.join(OPTIONAL_COLUMNS)}）"]
            )
        for row in reader:
            row = {(k or "").strip(): (v or "") for k, v in row.items() if k is not None}
            if not any(v.strip() for v in row.values()):
                continue  # 空行はスキップ
            txn = _parse_row(row, reader.line_num, errors)
            if txn is not None:
                transactions.append(txn)

    if errors:
        raise TransactionValidationError(errors)
    if not transactions:
        raise TransactionValidationError(["データ行がありません（ヘッダーのみの CSV です）"])
    transactions.sort(key=lambda t: t.date)  # 安定ソート: 同日内は CSV の記載順を保持
    return transactions


# ---------------------------------------------------------------------------
# XIRR（金額加重リターン）
# ---------------------------------------------------------------------------


def xirr(
    flows: Sequence[tuple[dt.date, float]],
    *,
    guess: float = 0.1,
    max_iter: int = 100,
) -> float:
    r"""不定期キャッシュフローの内部収益率（XIRR、年率）を求める。

    $\sum_i CF_i (1+r)^{-t_i} = 0$（$t_i$ は最初のフローからの経過年数、365.25日 = 1年）
    を満たす $r$ を、ニュートン法で探索し、収束しない場合は符号反転区間の
    二分法にフォールバックする。

    Args:
        flows: ``(日付, 金額)`` のリスト。符号は投資家視点（投下 = 負、回収 = 正）。
        guess: ニュートン法の初期値。
        max_iter: ニュートン法の最大反復回数。

    Returns:
        年率リターン $r$（例: 0.05 = 年率5%）。

    Raises:
        ValueError: フローが2件未満、正負両方の符号が無い、または解が
            見つからない場合。
    """
    if len(flows) < 2:
        raise ValueError("XIRR には2件以上のキャッシュフローが必要です")
    amounts = [float(a) for _, a in flows]
    if not any(a < 0 for a in amounts) or not any(a > 0 for a in amounts):
        raise ValueError(
            "XIRR にはキャッシュフローに正（回収）と負（投下）の両方が必要です"
        )
    d0 = min(d for d, _ in flows)
    times = [(d - d0).days / 365.25 for d, _ in flows]
    scale = max(1.0, sum(abs(a) for a in amounts))
    tol = 1e-12 * scale

    def npv(rate: float) -> float:
        try:
            return sum(a * (1.0 + rate) ** (-t) for a, t in zip(amounts, times))
        except OverflowError:
            return math.inf

    def dnpv(rate: float) -> float:
        try:
            return sum(-t * a * (1.0 + rate) ** (-t - 1.0) for a, t in zip(amounts, times))
        except OverflowError:
            return math.inf

    # ニュートン法
    rate = guess
    for _ in range(max_iter):
        if rate <= -1.0 + 1e-9:
            break
        f = npv(rate)
        if not math.isfinite(f):
            break
        if abs(f) < tol:
            return rate
        d = dnpv(rate)
        if d == 0.0 or not math.isfinite(d):
            break
        new_rate = rate - f / d
        if not math.isfinite(new_rate) or new_rate <= -1.0 + 1e-9:
            break
        if abs(new_rate - rate) < 1e-12:
            rate = new_rate
            break
        rate = new_rate
    else:
        rate = float("nan")
    if math.isfinite(rate) and abs(npv(rate)) < tol:
        return rate

    # 二分法フォールバック: 粗いグリッドで符号反転区間を探す
    grid = [-0.9999, -0.999, -0.99, -0.95, -0.9, -0.75, -0.5, -0.25, -0.1,
            0.0, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 50.0, 100.0]
    prev_r = grid[0]
    prev_f = npv(prev_r)
    for g in grid[1:]:
        f_g = npv(g)
        if abs(prev_f) < tol:
            return prev_r
        if math.isfinite(prev_f) and math.isfinite(f_g) and prev_f * f_g < 0:
            lo, hi, f_lo = prev_r, g, prev_f
            for _ in range(200):
                mid = (lo + hi) / 2.0
                f_mid = npv(mid)
                if abs(f_mid) < tol or (hi - lo) < 1e-12:
                    return mid
                if f_lo * f_mid < 0:
                    hi = mid
                else:
                    lo, f_lo = mid, f_mid
            return (lo + hi) / 2.0
        prev_r, prev_f = g, f_g
    raise ValueError(
        "XIRR が収束しませんでした（キャッシュフローの符号・規模・日付を確認してください）"
    )


# ---------------------------------------------------------------------------
# 台帳（平均取得単価法）と評価結果
# ---------------------------------------------------------------------------


@dataclass
class _HoldingState:
    """銘柄別の保有状態（移動平均法、取得原価は手数料込み）。"""

    shares: float = 0.0
    cost: float = 0.0

    @property
    def avg_cost(self) -> float:
        return self.cost / self.shares if self.shares > _EPS else 0.0


@dataclass(frozen=True)
class HoldingValuation:
    """評価日時点の保有1銘柄の評価。"""

    code: str
    shares: float
    avg_cost: float  # 手数料込み平均取得単価（円）
    price: float  # 直近終値（円）
    price_date: dt.date

    @property
    def market_value(self) -> float:
        return self.shares * self.price

    @property
    def unrealized_pnl(self) -> float:
        return (self.price - self.avg_cost) * self.shares


@dataclass(frozen=True)
class BenchmarkComparison:
    """同じ外部キャッシュフローをベンチマークに投じた場合の複製結果。"""

    benchmark: str
    terminal_value: float  # 複製ポートフォリオの終端評価額（円）
    xirr_value: float | None
    xirr_error: str | None = None


def replicate_on_benchmark(
    flows: Sequence[tuple[dt.date, float]],
    close: pd.Series,
    benchmark: str,
    warnings: list[str] | None = None,
) -> BenchmarkComparison:
    """外部キャッシュフローをベンチマークの終値で複製し、XIRR を計算する。

    投下（負のフロー）はその日の直近終値でベンチマークを買い付け、回収（正の
    フロー）は同額分を売却したとみなす。終端評価額は保有口数 × 最終終値。
    フローがベンチマーク系列の開始より前の場合は系列最初の終値で代用し、
    ``warnings`` に注記を追記する。

    Args:
        flows: 外部キャッシュフロー（終端評価額は含めない。投下 = 負、回収 = 正）。
        close: ベンチマークの終値系列（DatetimeIndex）。
        benchmark: ベンチマークのティッカー（表示用）。
        warnings: 注記の追記先（None なら破棄）。
    """
    notes: list[str] = warnings if warnings is not None else []
    units = 0.0
    for date, amount in flows:
        px = _price_asof(close, date, notes, label=f"ベンチマーク {benchmark}")
        units += (-amount) / px  # 投下（負）→ 買付、回収（正）→ 売却
    end_date = close.index[-1].date()
    terminal = units * float(close.iloc[-1])
    xirr_value: float | None = None
    xirr_error: str | None = None
    try:
        xirr_value = xirr([*flows, (end_date, terminal)])
    except ValueError as exc:
        xirr_error = str(exc)
    return BenchmarkComparison(
        benchmark=benchmark, terminal_value=terminal,
        xirr_value=xirr_value, xirr_error=xirr_error,
    )


def _price_asof(close: pd.Series, date: dt.date, warnings: list[str], label: str) -> float:
    """``date`` 以前の直近終値を返す。系列開始前なら最初の終値で代用し注記する。"""
    sliced = close.loc[: pd.Timestamp(date)]
    if len(sliced) == 0:
        first_date = close.index[0].date()
        warnings.append(
            f"{label}: {date} は価格系列の開始（{first_date}）より前のため、"
            "系列最初の終値で代用しました（取得期間を --period で広げると解消できる場合があります）"
        )
        return float(close.iloc[0])
    return float(sliced.iloc[-1])


@dataclass(frozen=True)
class PerformanceResult:
    """実運用パフォーマンスの評価結果。"""

    mode: str  # "account"（入出金あり）| "position"（約定のみ）
    start_date: dt.date  # 最初の取引日
    end_date: dt.date  # 評価日（ベンチマーク系列の最終日）
    external_flows: list[tuple[dt.date, float]]  # 投下 = 負、回収 = 正（終端は含まない）
    terminal_value: float  # 終端評価額（保有時価 + 現金残高）
    cash_balance: float | None  # 口座モードのみ（ポジションモードは None）
    holdings: list[HoldingValuation]
    realized_pnl: float  # 実現損益（売却、手数料控除後）
    dividends_received: float  # 受取配当（税・手数料控除後）
    fees_paid: float  # 手数料・税の合計（参考値）
    xirr_value: float | None
    xirr_error: str | None
    benchmark: BenchmarkComparison
    warnings: list[str] = field(default_factory=list)
    synthetic: bool = False

    @property
    def span_years(self) -> float:
        """計測期間（年、365.25日 = 1年）。"""
        return max((self.end_date - self.start_date).days, 0) / 365.25

    @property
    def invested_total(self) -> float:
        """外部流入（投下資金）の合計（円、正の値で返す）。"""
        return -sum(a for _, a in self.external_flows if a < 0)

    @property
    def returned_total(self) -> float:
        """外部流出（回収額）の合計（円。終端評価額は含まない）。"""
        return sum(a for _, a in self.external_flows if a > 0)

    @property
    def unrealized_pnl(self) -> float:
        return sum(h.unrealized_pnl for h in self.holdings)

    @property
    def total_pnl(self) -> float:
        """総損益 = 終端評価額 + 回収額 − 投下資金（= 実現 + 未実現 + 受取配当）。"""
        return self.terminal_value + sum(a for _, a in self.external_flows)

    def to_markdown(self) -> str:
        """数値部分（サマリー・XIRR・ベンチマーク比較・明細）を Markdown で返す。"""
        mode_label = (
            "口座モード（deposit/withdraw を外部キャッシュフローとし、"
            "現金残高も評価に含む）"
            if self.mode == "account"
            else "ポジションモード（入出金の記録が無いため、buy/sell/dividend を"
            "外部キャッシュフローとみなす）"
        )
        lines: list[str] = []

        lines.append("## サマリー")
        lines.append("")
        summary_rows: list[list[object]] = [
            ["計測期間", f"{self.start_date} 〜 {self.end_date}（{self.span_years:.2f} 年）"],
            ["計測モード", mode_label],
            ["投下資金（外部流入計）", f"{fmt_num(self.invested_total, 0)} 円"],
            ["回収額（外部流出計）", f"{fmt_num(self.returned_total, 0)} 円"],
            ["終端評価額", f"{fmt_num(self.terminal_value, 0)} 円"],
        ]
        if self.cash_balance is not None:
            summary_rows.append(["うち現金残高", f"{fmt_num(self.cash_balance, 0)} 円"])
        summary_rows.append(["総損益", f"{fmt_num(self.total_pnl, 0)} 円"])
        lines.append(markdown_table(["項目", "値"], summary_rows))
        lines.append("")

        lines.append("### 損益の内訳")
        lines.append("")
        lines.append(markdown_table(
            ["内訳", "金額（円）"],
            [
                ["実現損益（売却、手数料控除後）", fmt_num(self.realized_pnl, 0)],
                ["未実現損益（保有分）", fmt_num(self.unrealized_pnl, 0)],
                ["受取配当（税・手数料控除後）", fmt_num(self.dividends_received, 0)],
                ["合計", fmt_num(self.realized_pnl + self.unrealized_pnl + self.dividends_received, 0)],
                ["（参考）支払手数料・税の合計", fmt_num(self.fees_paid, 0)],
            ],
        ))
        lines.append("")

        lines.append("## 金額加重リターン（MWR = XIRR、年率）")
        lines.append("")
        if self.xirr_value is not None:
            lines.append(f"- 実績 XIRR: **年率 {fmt_pct(self.xirr_value)}**")
        else:
            lines.append(f"- 実績 XIRR: 計算不能（{self.xirr_error}）")
        if self.span_years < 1.0:
            lines.append(
                "- **注意**: 計測期間が1年未満のため、年率換算の XIRR は実際の値動き"
                "以上に誇張されて見える（年率化は外挿である）。"
            )
        lines.append(
            f"- 計測期間 {self.span_years:.2f} 年の実績は、統計的には運とスキルを"
            "区別できない長さである点に注意（`knowledge/math/"
            "performance-measurement-and-attribution.md` 第5節: 情報比率 0.5 の"
            "運用者がスキルを 5% 水準で立証するには約 16 年を要する）。"
        )
        lines.append("")

        lines.append("## ベンチマーク比較（同じキャッシュフローを投じた場合）")
        lines.append("")
        bench = self.benchmark
        bench_xirr_str = (
            fmt_pct(bench.xirr_value) if bench.xirr_value is not None
            else f"計算不能（{bench.xirr_error}）"
        )
        own_xirr_str = (
            fmt_pct(self.xirr_value) if self.xirr_value is not None
            else f"計算不能（{self.xirr_error}）"
        )
        lines.append(markdown_table(
            ["指標", "実績（自分の運用）", f"ベンチマーク複製（{bench.benchmark}）"],
            [
                ["XIRR（年率）", own_xirr_str, bench_xirr_str],
                ["終端評価額（円）", fmt_num(self.terminal_value, 0), fmt_num(bench.terminal_value, 0)],
            ],
        ))
        if self.xirr_value is not None and bench.xirr_value is not None:
            diff_pt = (self.xirr_value - bench.xirr_value) * 100.0
            lines.append("")
            lines.append(
                f"- XIRR の差: **{diff_pt:+.2f} ポイント**（実績 − ベンチマーク複製。"
                "市場要因と銘柄選択・タイミングの寄与を分離するための材料であり、"
                "優劣の断定ではない）"
            )
        if bench.benchmark.startswith("^"):
            lines.append(
                f"- 注記: {bench.benchmark} は配当を含まない価格指数のため、比較は"
                "配当の分だけベンチマーク側が控えめになる（配当込みの比較には "
                "1306.T（TOPIX 連動 ETF、分配金調整済み）等を --benchmark に指定）。"
            )
        lines.append("")

        if self.holdings:
            lines.append("## 保有明細（評価日時点）")
            lines.append("")
            lines.append(markdown_table(
                ["コード", "株数", "平均取得単価（手数料込み）", "現在値", "評価額", "未実現損益"],
                [
                    [
                        h.code,
                        fmt_num(h.shares, 0),
                        fmt_num(h.avg_cost),
                        fmt_num(h.price),
                        fmt_num(h.market_value, 0),
                        fmt_num(h.unrealized_pnl, 0),
                    ]
                    for h in self.holdings
                ],
            ))
            lines.append("")

        lines.append("## 外部キャッシュフロー明細（XIRR の入力）")
        lines.append("")
        flow_rows: list[list[object]] = [
            [d.isoformat(), fmt_num(a, 0), "投下" if a < 0 else "回収"]
            for d, a in self.external_flows
        ]
        flow_rows.append([self.end_date.isoformat(), fmt_num(self.terminal_value, 0), "終端評価額"])
        lines.append(markdown_table(["日付", "金額（円）", "区分"], flow_rows))
        lines.append("")

        if self.warnings:
            lines.append("## 注意事項")
            lines.append("")
            lines.extend(f"- {w}" for w in self.warnings)
            lines.append("")

        return "\n".join(lines)


def _derive_period(start: dt.date, today: dt.date | None = None) -> str:
    """最初の取引日から必要な価格取得期間（yfinance の period 文字列）を導出する。"""
    today = today or dt.date.today()
    days = max((today - start).days, 1)
    years = days / 365.25
    if years >= 9.0:
        return "max"
    return f"{int(math.ceil(years)) + 1}y"


def _build_ledger(
    transactions: Sequence[Transaction],
) -> tuple[dict[str, _HoldingState], float, float, float, float, str, list[str]]:
    """取引を日付順に処理し、保有状態・損益・現金残高・モード・注記を返す。

    Returns:
        ``(holdings, realized_pnl, dividends, fees, cash, mode, warnings)``。

    Raises:
        TransactionValidationError: 保有株数を超える売却がある場合。
    """
    holdings: dict[str, _HoldingState] = {}
    realized = 0.0
    dividends = 0.0
    fees = 0.0
    cash = 0.0
    warnings: list[str] = []
    errors: list[str] = []
    mode = "account" if any(t.side in CASH_SIDES for t in transactions) else "position"
    cash_negative_warned = False

    for t in transactions:
        fees += t.fee
        if t.side == "buy":
            st = holdings.setdefault(t.code, _HoldingState())
            st.cost += t.gross_amount + t.fee
            st.shares += t.shares
        elif t.side == "sell":
            st = holdings.get(t.code)
            if st is None or st.shares < t.shares - _EPS:
                held = st.shares if st is not None else 0.0
                errors.append(
                    f"{t.date} の {t.code} 売却 {t.shares:g} 株が保有株数（{held:g} 株）を"
                    "超えています（買付の記録漏れ・株式分割の未反映を確認してください）"
                )
                continue
            avg = st.avg_cost
            realized += (t.price - avg) * t.shares - t.fee
            st.cost -= avg * t.shares
            st.shares -= t.shares
            if st.shares <= _EPS:
                st.shares = 0.0
                st.cost = 0.0
        elif t.side == "dividend":
            st = holdings.get(t.code)
            if st is None or st.shares <= _EPS:
                warnings.append(
                    f"{t.date} の {t.code} 配当は保有していない（記録上株数 0 の）銘柄への"
                    "配当です（買付の記録漏れの可能性）"
                )
            dividends += t.gross_amount - t.fee
        # deposit / withdraw は現金残高のみに影響
        cash += t.cash_delta
        if mode == "account" and cash < -_EPS and not cash_negative_warned:
            warnings.append(
                f"現金残高が {t.date} 時点で負（{cash:,.0f} 円）になりました。"
                "入金（deposit）の記録漏れの可能性があります。口座モードの XIRR は"
                "入出金の完全な記録を前提とするため、この場合の数値は歪みます"
            )
            cash_negative_warned = True

    if errors:
        raise TransactionValidationError(errors)
    return holdings, realized, dividends, fees, cash, mode, warnings


def evaluate_performance(
    transactions: Sequence[Transaction],
    *,
    benchmark: str = DEFAULT_BENCHMARK,
    synthetic: bool = False,
    period: str | None = None,
) -> PerformanceResult:
    """取引履歴から実運用パフォーマンス（XIRR・損益・ベンチマーク比較）を評価する。

    Args:
        transactions: :func:`load_transactions` が返す取引リスト。
        benchmark: 比較対象のティッカー（既定 ``^N225``。配当込み比較には
            ``1306.T`` 等の分配金調整済み ETF を推奨）。
        synthetic: True なら合成データで評価（ネットワーク不要の手法デモ）。
        period: 価格取得期間の明示指定（省略時は最初の取引日から自動導出）。

    Raises:
        TransactionValidationError: 保有株数を超える売却がある場合。
        DataFetchError: 価格取得に失敗した場合。
        ValueError: 取引リストが空の場合。
    """
    if not transactions:
        raise ValueError("取引履歴が空です")
    txns = sorted(transactions, key=lambda t: t.date)  # 安定ソート
    start_date = txns[0].date
    fetch_period = period or _derive_period(start_date)

    holdings, realized, dividends, fees, cash, mode, warnings = _build_ledger(txns)

    held_codes = [code for code, st in holdings.items() if st.shares > _EPS]
    codes_to_fetch = list(dict.fromkeys([*held_codes, benchmark]))
    prices = fetch_prices(codes_to_fetch, period=fetch_period, synthetic=synthetic)
    bench_close: pd.Series = prices[benchmark]["Close"].dropna()
    if len(bench_close) == 0:
        raise ValueError(f"ベンチマーク {benchmark} の終値系列が空です")
    end_date: dt.date = bench_close.index[-1].date()

    valuations: list[HoldingValuation] = []
    for code in held_codes:
        close = prices[code]["Close"].dropna()
        st = holdings[code]
        price = float(close.iloc[-1])
        price_date = close.index[-1].date()
        if abs((price_date - end_date).days) > 7:
            warnings.append(
                f"{code} の直近終値（{price_date}）が評価日（{end_date}）から1週間以上"
                "離れています（上場廃止・取得失敗などデータ品質を確認してください）"
            )
        valuations.append(HoldingValuation(
            code=code, shares=st.shares, avg_cost=st.avg_cost,
            price=price, price_date=price_date,
        ))
    market_value = sum(h.market_value for h in valuations)

    # 外部キャッシュフロー（投資家視点: 投下 = 負、回収 = 正）
    external_flows: list[tuple[dt.date, float]] = []
    if mode == "account":
        for t in txns:
            if t.side == "deposit":
                external_flows.append((t.date, -t.gross_amount))
            elif t.side == "withdraw":
                external_flows.append((t.date, t.gross_amount))
        terminal_value = cash + market_value
        cash_balance: float | None = cash
    else:
        for t in txns:
            if t.side == "buy":
                external_flows.append((t.date, -(t.gross_amount + t.fee)))
            elif t.side == "sell":
                external_flows.append((t.date, t.gross_amount - t.fee))
            elif t.side == "dividend":
                external_flows.append((t.date, t.gross_amount - t.fee))
        terminal_value = market_value
        cash_balance = None

    xirr_value: float | None = None
    xirr_error: str | None = None
    try:
        xirr_value = xirr([*external_flows, (end_date, terminal_value)])
    except ValueError as exc:
        xirr_error = str(exc)

    bench_comp = replicate_on_benchmark(external_flows, bench_close, benchmark, warnings)

    return PerformanceResult(
        mode=mode,
        start_date=start_date,
        end_date=end_date,
        external_flows=external_flows,
        terminal_value=terminal_value,
        cash_balance=cash_balance,
        holdings=valuations,
        realized_pnl=realized,
        dividends_received=dividends,
        fees_paid=fees,
        xirr_value=xirr_value,
        xirr_error=xirr_error,
        benchmark=bench_comp,
        warnings=warnings,
        synthetic=synthetic,
    )


__all__ = [
    "VALID_SIDES",
    "REQUIRED_COLUMNS",
    "OPTIONAL_COLUMNS",
    "DEFAULT_BENCHMARK",
    "Transaction",
    "TransactionValidationError",
    "HoldingValuation",
    "BenchmarkComparison",
    "PerformanceResult",
    "load_transactions",
    "xirr",
    "replicate_on_benchmark",
    "evaluate_performance",
]

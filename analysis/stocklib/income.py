"""配当インカム集計モジュール。

保有ポートフォリオ全体の「年間いくら配当を受け取れる見込みか」を集計する。
銘柄ごとに直近12ヶ月（TTM）の実績1株配当（yfinance ``Ticker.dividends``）×株数で
年間受取配当（税引前）を推計し、取得原価ベースの YOC（Yield on Cost）と時価ベースの
配当利回りを併記する。口座区分（``account`` 列、:mod:`stocklib.portfolio` と同一定義）が
ある場合は、NISA口座分を非課税・課税口座分を源泉徴収税率 20.315%（2025年時点、
:data:`stocklib.portfolio.CAPITAL_GAINS_TAX_RATE` を再利用）として税引後の手取りと
NISA非課税メリット（年間フロー）を試算する。

前提と限界（レポート本文にも明記する）:

- TTM 実績配当は**将来の配当を保証しない**（減配・無配転落リスク。高利回りはそれ自体が
  減配織り込みのシグナルでありうる。``knowledge/strategies/dividend-income-investing.md``）。
- yfinance の配当データは日本株で欠損・調整不備がありうる
  （``knowledge/data-sources/data-apis-and-tools.md``）。
- NISA口座の配当が非課税になるのは**株式数比例配分方式**（証券口座での配当受取）を
  選択した場合のみ（``knowledge/regulation-tax/taxation-and-nisa.md``、2024年制度）。
- ``manual_price`` を持つ行（投資信託・現金などの手入力評価行、
  :class:`stocklib.portfolio.Position` 参照）は**配当集計の対象外**
  （``dps_ttm = 0``・価格取得なしで合計に含める）。投資信託の分配金・現金の利息は
  本モジュールでは扱わない（レポートに脚注として明記する）。

CLI は ``analysis/income_report.py``。``--synthetic`` では :func:`synthetic_dividends`
（シード固定の決定論的な半期配当系列）で全機能がネットワーク不要で動く。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pandas as pd

from stocklib import report
from stocklib.data import (
    DataFetchError,
    _seed_from_code,
    fetch_prices,
    normalize_code,
)
from stocklib.portfolio import (
    ACCOUNT_LABELS,
    ACCOUNT_TAXABLE,
    CAPITAL_GAINS_TAX_RATE,
    NISA_ACCOUNTS,
    Position,
    _resolve_name_sector,
)

#: 上場株式の配当への源泉徴収税率（所得税15% + 復興特別所得税0.315% + 住民税5%、
#: 2025年時点）。:data:`stocklib.portfolio.CAPITAL_GAINS_TAX_RATE` と同率のため再利用する。
#: 実効負担は課税方式の選択（申告分離 / 総合課税+配当控除 / 申告不要）で変わりうる。
DIVIDEND_TAX_RATE: float = CAPITAL_GAINS_TAX_RATE

#: TTM（trailing twelve months）の日数窓。
_TTM_DAYS: int = 365


def synthetic_dividends(
    code: str,
    *,
    as_of: dt.date | None = None,
    years: int = 3,
) -> pd.Series:
    """合成配当系列を生成する（ネットワーク不要、シード固定で再現可能）。

    日本株で一般的な**半期配当**（中間+期末の年2回）を、``as_of`` から遡って
    ``years`` 年分生成する。年間1株配当は :func:`stocklib.data.synthetic_prices` と
    同じ価格帯の想定水準 × 利回り1〜4%相当から決定論的に導出し、年率0〜8%の
    決定論的な増配率で過去に向かって逓減させる（直近12ヶ月合計 = 想定年間配当）。

    Args:
        code: 銘柄コード（シード導出に使用。同じコードは常に同じ系列を返す）。
        as_of: 基準日（既定: 今日）。直近の支払いは基準日の約60日前になる。
        years: 生成する年数（年2回 × ``years`` 件の倍）。

    Returns:
        支払日昇順の ``pd.Series``（値は1株当たり配当円、name="Dividends"）。
    """
    if years < 1:
        raise ValueError("years は 1 以上を指定してください")
    if as_of is None:
        as_of = dt.date.today()
    seed = _seed_from_code(code) ^ 0x9E3779B9  # 価格系列とは別ストリーム
    rng = np.random.default_rng(seed)

    # data.py の合成株価と同じ価格帯（300 + seed % 9000）に、利回り1〜4%相当を掛ける
    base_price = 300.0 + float(_seed_from_code(code) % 9000)
    div_yield = float(rng.uniform(0.01, 0.04))
    growth = float(rng.uniform(0.0, 0.08))  # 年率の増配率（過去に向かって逓減）
    annual_dps = base_price * div_yield

    dates: list[pd.Timestamp] = []
    values: list[float] = []
    anchor = pd.Timestamp(as_of) - pd.Timedelta(days=60)
    for k in range(2 * years):  # k=0 が直近、半年（182日）刻みで過去へ
        year_index = k // 2
        dates.append(anchor - pd.Timedelta(days=182 * k))
        values.append(annual_dps / 2.0 / (1.0 + growth) ** year_index)

    series = pd.Series(values, index=pd.DatetimeIndex(dates), name="Dividends")
    return series.sort_index()


def fetch_dividends(code: str, *, synthetic: bool = False) -> pd.Series:
    """1銘柄の配当支払い履歴（1株当たり、支払日昇順）を取得する。

    yfinance の ``Ticker.dividends`` を利用する（非公式 API。日本株では欠損・
    調整不備がありうる。``knowledge/data-sources/data-apis-and-tools.md`` 参照）。
    配当実績が無い銘柄は空の Series を返す（例外にしない）。

    Args:
        code: 銘柄コード（4桁数字は内部で ``.T`` に正規化）。
        synthetic: True なら :func:`synthetic_dividends` の合成系列を返す。

    Raises:
        DataFetchError: yfinance の取得自体に失敗した場合。
    """
    ticker = normalize_code(code)
    if synthetic:
        return synthetic_dividends(ticker)
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover
        raise DataFetchError(
            "yfinance がインストールされていません。`pip install yfinance` を実行するか、"
            "--synthetic フラグで合成データを使用してください。"
        ) from exc
    try:
        series = yf.Ticker(ticker).dividends
    except Exception as exc:
        raise DataFetchError(
            f"{ticker} の配当履歴の取得に失敗しました（ネットワーク・ティッカー名を"
            f"確認してください。オフライン検証には --synthetic を使用できます）: {exc}"
        ) from exc
    if series is None or len(series) == 0:
        return pd.Series(dtype=float, name="Dividends")
    series = series.astype(float)
    if isinstance(series.index, pd.DatetimeIndex) and series.index.tz is not None:
        series.index = series.index.tz_localize(None)
    return series.sort_index()


def ttm_dividend(dividends: pd.Series, as_of: dt.date | None = None) -> float:
    """直近12ヶ月（TTM）の1株当たり実績配当の合計を返す。

    ``as_of``（既定: 今日）から遡って :data:`_TTM_DAYS` 日以内に支払われた配当を
    合計する。履歴が空・窓内に支払いが無い場合は 0.0。

    Args:
        dividends: :func:`fetch_dividends` が返す支払日インデックスの Series。
        as_of: 基準日（既定: 今日）。
    """
    if dividends is None or len(dividends) == 0:
        return 0.0
    if as_of is None:
        as_of = dt.date.today()
    end = pd.Timestamp(as_of) + pd.Timedelta(days=1)  # as_of 当日の支払いを含める
    start = end - pd.Timedelta(days=_TTM_DAYS)
    window = dividends[(dividends.index >= start) & (dividends.index < end)]
    return float(window.sum())


@dataclass(frozen=True)
class PositionIncome:
    """銘柄1つ分の配当インカム集計結果。

    Attributes:
        code: 銘柄コード。
        name: 銘柄名。
        shares: 保有株数。
        avg_cost: 平均取得単価（円）。
        price: 直近終値（円。手入力行は ``manual_price`` の値）。
        dps_ttm: 直近12ヶ月の実績1株配当（円、TTM合計。手入力行は常に 0）。
        account: 口座区分（CSV の任意列。None は taxable 扱い）。
        manual: True なら ``manual_price`` による手入力評価行（投信・現金等）。
            配当集計の対象外で ``dps_ttm = 0``（年間配当 0 として合計に含む）。
    """

    code: str
    name: str
    shares: float
    avg_cost: float
    price: float
    dps_ttm: float
    account: str | None = None
    manual: bool = False

    @property
    def account_type(self) -> str:
        """口座区分（未指定は ``taxable`` 扱い）。"""
        return self.account if self.account is not None else ACCOUNT_TAXABLE

    @property
    def is_nisa(self) -> bool:
        """NISA口座（つみたて/成長投資枠）かどうか。"""
        return self.account_type in NISA_ACCOUNTS

    @property
    def annual_gross(self) -> float:
        """年間受取配当見込み（税引前、``dps_ttm × shares``）。"""
        return self.dps_ttm * self.shares

    @property
    def yoc(self) -> float:
        """YOC（Yield on Cost、取得原価ベースの利回り ``dps_ttm / avg_cost``）。"""
        if not np.isfinite(self.avg_cost) or self.avg_cost <= 0:
            return float("nan")
        return self.dps_ttm / self.avg_cost

    @property
    def market_yield(self) -> float:
        """時価ベースの配当利回り（``dps_ttm / price``、TTM実績ベース）。"""
        if not np.isfinite(self.price) or self.price <= 0:
            return float("nan")
        return self.dps_ttm / self.price

    @property
    def tax_rate(self) -> float:
        """適用する源泉徴収税率（NISA は 0、課税口座は 20.315%、2025年時点）。"""
        return 0.0 if self.is_nisa else DIVIDEND_TAX_RATE

    @property
    def tax_withheld(self) -> float:
        """年間の源泉徴収税額（円）。"""
        return self.annual_gross * self.tax_rate

    @property
    def annual_net(self) -> float:
        """年間受取配当見込み（税引後・手取り、円）。"""
        return self.annual_gross - self.tax_withheld


@dataclass
class IncomeReport:
    """ポートフォリオ全体の配当インカム集計。:meth:`to_markdown` で Markdown 化できる。

    Attributes:
        as_of: 基準日（TTM 窓の右端）。
        period: 現在値の取得に使った価格期間。
        synthetic: 合成データによる実行かどうか。
        has_account: CSV に口座区分（``account`` 列）が1つでも指定されていたか。
            False の場合は全銘柄を課税口座として試算し、NISA節を出さない。
        positions: 銘柄別の集計結果。
        no_dividend: TTM 窓内に配当実績が無かった**上場銘柄**のコード（無配または
            yfinance のデータ欠損の可能性。年間配当 0 として合計に含む）。
            手入力評価行（``manual_price``）はここに含めず、:attr:`manual_codes`
            として別に注記する。
    """

    as_of: dt.date
    period: str
    synthetic: bool
    has_account: bool
    positions: list[PositionIncome]
    no_dividend: list[str]

    @property
    def manual_codes(self) -> list[str]:
        """手入力評価行（``manual_price``、配当集計の対象外）のコード一覧。"""
        return [p.code for p in self.positions if p.manual]

    @property
    def total_gross(self) -> float:
        """年間受取配当見込みの合計（税引前、円）。"""
        return float(sum(p.annual_gross for p in self.positions))

    @property
    def total_tax(self) -> float:
        """年間の源泉徴収税額の合計（円）。"""
        return float(sum(p.tax_withheld for p in self.positions))

    @property
    def total_net(self) -> float:
        """税引後の年間インカム合計（手取り、円）。"""
        return float(sum(p.annual_net for p in self.positions))

    @property
    def monthly_net(self) -> float:
        """税引後年間インカムの月割り額（``total_net / 12``、円）。"""
        return self.total_net / 12.0

    @property
    def nisa_gross(self) -> float:
        """NISA口座分の年間受取配当見込み（税引前、円）。"""
        return float(sum(p.annual_gross for p in self.positions if p.is_nisa))

    @property
    def nisa_tax_benefit(self) -> float:
        """NISA分の非課税メリット（年間フロー、円）: ``nisa_gross × 20.315%``。

        課税口座で同額の配当を受け取った場合に源泉徴収されうる額の目安
        （株式数比例配分方式の選択が前提。2025年時点税率）。
        """
        return self.nisa_gross * DIVIDEND_TAX_RATE

    def to_markdown(self) -> str:
        """レポート本文（Markdown、見出し ``##`` 以下）を生成する。

        タイトル・生成日時は含まないので、呼び出し側で
        :func:`stocklib.report.report_header` と組み合わせる。
        """
        lines: list[str] = []

        lines.append("## 銘柄別の年間受取配当見込み（直近12ヶ月実績ベース）")
        lines.append("")
        rows: list[list[object]] = []
        for p in self.positions:
            if p.manual:
                # 手入力評価行（投信・現金等）: 配当集計の対象外。誤解を招く
                # 0円/0% を並べず「-」で対象外であることを明示する（脚注参照）。
                rows.append([
                    p.code + "※",
                    p.name,
                    report.fmt_num(p.shares, 0),
                    "-", "-", "-", "-",
                    ACCOUNT_LABELS.get(p.account_type, p.account_type),
                    "-", "-",
                ])
                continue
            rows.append([
                p.code,
                p.name,
                report.fmt_num(p.shares, 0),
                report.fmt_num(p.dps_ttm),
                report.fmt_num(p.annual_gross, 0),
                report.fmt_pct(p.yoc),
                report.fmt_pct(p.market_yield),
                ACCOUNT_LABELS.get(p.account_type, p.account_type),
                report.fmt_pct(p.tax_rate, 3),
                report.fmt_num(p.annual_net, 0),
            ])
        lines.append(report.markdown_table(
            ["コード", "銘柄名", "株数", "1株配当（TTM実績）", "年間配当（税引前）",
             "YOC（取得原価）", "配当利回り（時価）", "口座区分", "源泉税率",
             "年間配当（税引後）"],
            rows,
        ))
        lines.append("")
        lines.append(
            "- 1株配当は**直近12ヶ月に支払われた実績（TTM）の合計**であり、"
            "会社予想配当ではない。日本の実務では会社予想（forward）で見るのが標準的な"
            "ため、意思決定の際は決算短信の予想配当と突き合わせること"
            "（`knowledge/strategies/dividend-income-investing.md`）。"
        )
        lines.append(
            "- YOC（Yield on Cost）は取得単価に対する現在配当の比率。増配の複利を"
            "可視化する装置であり、保有継続の判断は時価ベースの利回りと期待トータル"
            "リターンで行うのが実務的（同文書第3節の YOC 批判を参照）。"
        )
        if self.manual_codes:
            lines.append(
                "- ※ の行は CSV の任意列 `manual_price` による**手入力評価行で、"
                "配当集計の対象外**（対象: "
                + ", ".join(self.manual_codes)
                + "。年間配当 0 として合計に含む）。投資信託の分配金・現金の利息は"
                "本レポートの対象外であり、必要なら運用報告書・口座明細等で"
                "別途確認すること。"
            )
        if self.no_dividend:
            lines.append(
                "- **直近12ヶ月の配当実績が取得できなかった銘柄**（無配、または "
                "yfinance のデータ欠損の可能性。年間配当 0 として集計）: "
                + ", ".join(self.no_dividend)
                + "。無配でないはずの銘柄がここに出た場合はデータ欠損を疑い、"
                "会社開示（決算短信・配当情報）で確認すること。"
            )
        lines.append("")

        lines.append("## 税引後の年間インカム合計")
        lines.append("")
        summary_rows: list[list[object]] = [
            ["年間受取配当見込み（税引前）", report.fmt_num(self.total_gross, 0)],
            ["源泉徴収税額（課税口座分 × 20.315%、2025年時点）",
             report.fmt_num(self.total_tax, 0)],
            ["税引後の年間インカム（手取り）", report.fmt_num(self.total_net, 0)],
            ["月割り額（税引後 ÷ 12）", report.fmt_num(self.monthly_net, 0)],
        ]
        lines.append(report.markdown_table(["項目", "値（円）"], summary_rows))
        lines.append("")
        if not self.has_account:
            lines.append(
                "- CSV に `account` 列が無い（または全行空欄）ため、**全銘柄を課税口座"
                "（源泉徴収 20.315%、2025年時点）として試算**した。NISA口座の保有が"
                "あれば `account` 列（`nisa_tsumitate` / `nisa_growth` / `taxable`）を"
                "入力すると非課税メリットを分解できる。"
            )
            lines.append("")

        if self.has_account:
            lines.append("### NISA分の非課税メリット（年間フロー）")
            lines.append("")
            nisa_n = sum(1 for p in self.positions if p.is_nisa)
            if self.nisa_gross > 0:
                lines.append(
                    f"- NISA口座分の年間受取配当見込み {report.fmt_num(self.nisa_gross, 0)} 円"
                    f"（{nisa_n} 銘柄）× 20.315%（2025年時点税率）= "
                    f"**約 {report.fmt_num(self.nisa_tax_benefit, 0)} 円/年**。"
                    "課税口座で同額の配当を受け取った場合に源泉徴収されうる額の目安であり、"
                    "実績配当が維持される前提の試算（減配なら縮む）。"
                )
            else:
                lines.append(
                    "- NISA口座分の配当が無い（または 0 円）ため、配当フローの"
                    "非課税メリットの試算は 0 円/年。"
                )
            lines.append(
                "- **実務上の必須注意**: NISA口座保有株の配当が非課税になるのは、"
                "配当の受取方式に**株式数比例配分方式**（証券口座での受取）を選択して"
                "いる場合のみ。銀行振込（登録配当金受領口座方式）や配当金領収証方式では"
                "NISA口座の保有分でも 20.315% が源泉徴収される"
                "（`knowledge/regulation-tax/taxation-and-nisa.md`、2024年制度）。"
                "受取方式は証券会社の口座設定で確認できる。"
            )
            lines.append("")

        lines.append("## 前提と限界")
        lines.append("")
        lines.append(
            "- **実績配当は将来の配当を保証しない**。減配・無配転落のリスクがあり、"
            "特に極端な高利回りは市場が減配を織り込んで株価を下げた結果である"
            "ことが多い（高利回りの罠）。本レポートは受取見込みの集計であり、"
            "増配銘柄・高利回り銘柄の購入を推奨するものではない"
            "（`knowledge/strategies/dividend-income-investing.md`）。"
        )
        lines.append(
            "- 配当データは yfinance の `Ticker.dividends`（非公式 API）による。"
            "日本株では**欠損・分割調整の不備がありうる**ため、金額が大きい判断の前に"
            "会社開示（決算短信・有価証券報告書）と突き合わせること"
            "（`knowledge/data-sources/data-apis-and-tools.md`）。"
        )
        lines.append(
            "- 税率 20.315%（所得税15% + 復興特別所得税0.315% + 住民税5%、2025年時点）は"
            "源泉徴収ベースの試算。確定申告での課税方式の選択（申告分離課税 / 総合課税+"
            "配当控除 / 申告不要）により実効負担は変わりうる"
            "（`knowledge/regulation-tax/taxation-and-nisa.md`）。"
        )
        lines.append(
            "- 権利確定から支払いまでのタイムラグ・端株・貸株中の配当金相当額"
            "（雑所得扱い）は考慮していない。月割り額は年間見込みの単純均等割りであり、"
            "実際の入金は権利確定月（3月・9月集中）に偏る。"
        )
        lines.append("")
        return "\n".join(lines)


def build_income_report(
    positions: Sequence[Position],
    *,
    period: str = "1y",
    synthetic: bool = False,
    as_of: dt.date | None = None,
) -> IncomeReport:
    """保有ポジションから配当インカム集計（:class:`IncomeReport`）を作る。

    銘柄ごとに配当履歴（:func:`fetch_dividends`）から TTM 実績1株配当を求め、
    株数を掛けて年間受取配当見込み（税引前）を推計する。現在値（直近終値、
    時価利回りの分母）は :func:`stocklib.data.fetch_prices` で取得する。
    口座区分（``account``）が1つでも指定されていれば、NISA分を非課税として
    税引後手取りと非課税メリットを分解する（未指定の銘柄は課税口座扱い）。

    ``manual_price`` を持つ行（投信・現金などの手入力評価行）は配当・価格の
    取得対象から外し、``dps_ttm = 0``・``price = manual_price`` で集計に含める
    （合成モードで架空の配当が混入せず、実データモードでも非上場コードの
    価格取得で失敗しない。投信の分配金・現金の利息は対象外である旨を
    レポートに脚注で明記する）。

    Args:
        positions: :func:`stocklib.portfolio.load_portfolio` が返すポジションのリスト。
        period: 現在値の取得期間（yfinance 形式、既定 ``"1y"``）。
        synthetic: True なら合成データ（価格・配当とも）で計算する（ネットワーク不要）。
        as_of: TTM 窓の基準日（既定: 今日。テスト用）。

    Raises:
        ValueError: positions が空の場合。
        DataFetchError: 価格・配当履歴の取得に失敗した場合。
    """
    if not positions:
        raise ValueError("positions が空です（load_portfolio の結果を渡してください）")
    if as_of is None:
        as_of = dt.date.today()

    market_codes = [p.code for p in positions if p.manual_price is None]
    prices: dict[str, pd.DataFrame] = {}
    if market_codes:
        prices = fetch_prices(market_codes, period=period, synthetic=synthetic)

    incomes: list[PositionIncome] = []
    no_dividend: list[str] = []
    for pos in positions:
        if pos.manual_price is not None:
            # 手入力評価行（投信・現金等）: 配当・価格の取得をスキップし、
            # dps_ttm=0・price=manual_price で集計に含める（tax_report と同じ扱い）。
            incomes.append(PositionIncome(
                code=pos.code,
                name=pos.code,
                shares=pos.shares,
                avg_cost=pos.avg_cost,
                price=pos.manual_price,
                dps_ttm=0.0,
                account=pos.account,
                manual=True,
            ))
            continue
        dividends = fetch_dividends(pos.code, synthetic=synthetic)
        dps = ttm_dividend(dividends, as_of=as_of)
        if dps <= 0.0:
            no_dividend.append(pos.code)
        name, _sector = _resolve_name_sector(pos.code, synthetic=synthetic)
        incomes.append(PositionIncome(
            code=pos.code,
            name=name,
            shares=pos.shares,
            avg_cost=pos.avg_cost,
            price=float(prices[pos.code]["Close"].iloc[-1]),
            dps_ttm=dps,
            account=pos.account,
        ))

    return IncomeReport(
        as_of=as_of,
        period=period,
        synthetic=synthetic,
        has_account=any(p.account is not None for p in positions),
        positions=incomes,
        no_dividend=no_dividend,
    )

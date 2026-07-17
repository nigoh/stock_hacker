#!/usr/bin/env python3
"""課税口座の含み損益の税価値ビューを生成する CLI。

使い方（リポジトリルートから）:
    python3 analysis/tax_report.py [--file data/portfolio.csv] [--period 1y] [--synthetic]

保有銘柄 CSV（stocklib.portfolio.load_portfolio が読む形式。任意列 account:
nisa_tsumitate / nisa_growth / taxable、空欄・列なしは taxable 扱い）を読み込み、
課税口座（特定・一般）ポジションの銘柄別含み損益と、含み損銘柄を
「実現した場合の税価値（試算）」= 含み損 × 20.315%（2025年時点の申告分離課税率、
stocklib.portfolio.CAPITAL_GAINS_TAX_RATE）を機械的に一覧する
reports/tax-<日付>.md を生成し、そのパスを stdout に出力する。

あわせて NISA 口座の含み損は損益通算・繰越控除の対象外であること
（knowledge/regulation-tax/taxation-and-nisa.md 4.2節）を対比表示し、
実務上の注意——同一銘柄の同日買い戻しによる取得単価の平均化（総平均法に
準ずる方法）で実現できる損失が縮む点、繰越控除には確定申告が必要な点、
税価値のために投資判断を歪める本末転倒（tax tail wagging the dog、
knowledge/strategies/behavioral-finance-japan.md）——を本文に自動挿入する。

本 CLI は「実現した場合の」条件付き試算に徹する情報整理であり、
損出し・売却・保有継続の判断はしない（投資助言ではない）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from stocklib import report
from stocklib.data import REPO_ROOT, DataFetchError, fetch_prices
from stocklib.portfolio import (
    ACCOUNT_LABELS,
    ACCOUNT_TAXABLE,
    CAPITAL_GAINS_TAX_RATE,
    NISA_ACCOUNTS,
    PortfolioValidationError,
    Position,
    _resolve_name_sector,
    load_portfolio,
)

DEFAULT_PORTFOLIO_CSV: Path = REPO_ROOT / "data" / "portfolio.csv"
TEMPLATE_CSV: Path = REPO_ROOT / "analysis" / "templates" / "portfolio-example.csv"

#: 参照するナレッジ文書（レポート本文に出典として挿入する）。
TAX_DOC: str = "knowledge/regulation-tax/taxation-and-nisa.md"
BEHAVIOR_DOC: str = "knowledge/strategies/behavioral-finance-japan.md"

#: 「実現した場合の税価値（試算）」列のヘッダー（条件付き表現を固定する）。
TAX_VALUE_COLUMN: str = "実現した場合の税価値（試算）"
#: 含み益側の対になる列ヘッダー（こちらも条件付き表現）。
TAX_COST_COLUMN: str = "実現した場合の課税見込み（試算）"


@dataclass(frozen=True)
class PositionTaxView:
    """銘柄1つ分の含み損益と税務上の取り扱いビュー。

    Attributes:
        code: 銘柄コード（手入力行は任意の識別子）。
        name: 銘柄名。
        account: 口座区分（``Position.account_type`` で解決済み。未指定は taxable）。
        shares: 株数。
        avg_cost: 平均取得単価。
        price: 現在値（直近終値。手入力行は ``manual_price``）。
        cost_value: 取得原価。
        market_value: 評価額。
        pnl: 含み損益（円。負 = 含み損）。
        pnl_pct: 含み損益率。
        manual: True なら ``manual_price`` による手入力評価。
    """

    code: str
    name: str
    account: str
    shares: float
    avg_cost: float
    price: float
    cost_value: float
    market_value: float
    pnl: float
    pnl_pct: float
    manual: bool = False

    @property
    def account_label(self) -> str:
        """口座区分の日本語表示名。"""
        return ACCOUNT_LABELS.get(self.account, self.account)

    @property
    def is_nisa(self) -> bool:
        """NISA 口座（つみたて/成長投資枠）のポジションか。"""
        return self.account in NISA_ACCOUNTS

    @property
    def is_loss(self) -> bool:
        """含み損か（pnl < 0）。"""
        return self.pnl < 0

    @property
    def tax_value_if_realized(self) -> float:
        """含み損を**実現した場合の**税価値（試算、円）。

        課税口座の含み損 × 20.315%（2025年時点の申告分離課税率）。
        同一年内の実現益・申告分離課税を選択した配当等との損益通算、
        または確定申告による繰越控除（翌年以降3年）で利益と相殺できた
        場合に軽減されうる税額の上限の目安であり、通算相手が無ければ
        生じない**条件付きの試算値**。含み益の銘柄は 0。
        NISA 口座は損益通算・繰越控除の対象外のため常に 0。
        """
        if self.is_nisa:
            return 0.0
        return max(-self.pnl, 0.0) * CAPITAL_GAINS_TAX_RATE

    @property
    def tax_cost_if_realized(self) -> float:
        """含み益を**実現した場合の**課税見込み（試算、円）。

        課税口座の含み益 × 20.315%（2025年時点）。含み損の銘柄は 0。
        NISA 口座は譲渡益非課税のため常に 0。
        """
        if self.is_nisa:
            return 0.0
        return max(self.pnl, 0.0) * CAPITAL_GAINS_TAX_RATE


@dataclass
class TaxValueSummary:
    """課税口座/NISA口座の含み損益 税価値ビュー。:meth:`to_markdown` で Markdown 化できる。"""

    as_of: dt.date
    period: str
    synthetic: bool
    taxable: list[PositionTaxView]
    nisa: list[PositionTaxView]

    @property
    def taxable_loss_total(self) -> float:
        """課税口座の含み損合計（負値。含み損銘柄のみの合算）。"""
        return float(sum(v.pnl for v in self.taxable if v.is_loss))

    @property
    def taxable_gain_total(self) -> float:
        """課税口座の含み益合計（含み益銘柄のみの合算）。"""
        return float(sum(v.pnl for v in self.taxable if not v.is_loss))

    @property
    def tax_value_total(self) -> float:
        """課税口座の含み損を実現した場合の税価値（試算）の合計。"""
        return float(sum(v.tax_value_if_realized for v in self.taxable))

    @property
    def tax_cost_total(self) -> float:
        """課税口座の含み益を実現した場合の課税見込み（試算）の合計。"""
        return float(sum(v.tax_cost_if_realized for v in self.taxable))

    @property
    def nisa_loss_total(self) -> float:
        """NISA 口座の含み損合計（負値。含み損銘柄のみの合算）。"""
        return float(sum(v.pnl for v in self.nisa if v.is_loss))

    def to_markdown(self) -> str:
        """レポート本文（Markdown、見出し ``##`` 以下）を生成する。"""
        lines: list[str] = []
        lines.append(self._taxable_markdown())
        lines.append(self._nisa_markdown())
        lines.append(self._notes_markdown())
        lines.append(self._assumptions_markdown())
        return "\n".join(lines)

    def _position_rows(self, views: Sequence[PositionTaxView]) -> list[list[object]]:
        rows: list[list[object]] = []
        for v in views:
            price_cell = report.fmt_num(v.price)
            if v.manual:
                price_cell += "※"
            rows.append([
                v.code,
                v.name,
                report.fmt_num(v.shares, 0),
                report.fmt_num(v.avg_cost),
                price_cell,
                report.fmt_num(v.cost_value, 0),
                report.fmt_num(v.market_value, 0),
                report.fmt_num(v.pnl, 0),
                report.fmt_pct(v.pnl_pct),
                report.fmt_num(v.tax_value_if_realized, 0) if v.is_loss else "-",
                (report.fmt_num(v.tax_cost_if_realized, 0)
                 if (not v.is_loss and v.pnl > 0) else "-"),
            ])
        return rows

    def _manual_footnote(self, views: Sequence[PositionTaxView]) -> list[str]:
        manual_codes = [v.code for v in views if v.manual]
        if not manual_codes:
            return []
        return [
            "- ※ の現在値は CSV の任意列 `manual_price` による**手入力評価**"
            f"（対象: {', '.join(manual_codes)}）。手入力値の取得日・鮮度の管理は"
            "ユーザーの責任であり、他銘柄の直近終値と評価時点がずれうる。",
            "",
        ]

    def _taxable_markdown(self) -> str:
        lines: list[str] = []
        lines.append("## 課税口座（特定・一般）の含み損益と税価値ビュー")
        lines.append("")
        lines.append(
            "保有 CSV の `account` 列が `taxable`（空欄・列なしも taxable 扱い）の"
            "ポジションを対象に、含み損益と「実現した場合の」税額への影響を機械的に"
            "一覧する。年末の損出し（含み損の実現による損益通算・繰越控除の活用、"
            f"{TAX_DOC} 2節）を検討する際の判断材料の整理であり、"
            "**どの銘柄を売却するか・保有を続けるかの判断はしない**。"
        )
        lines.append("")
        if not self.taxable:
            lines.append(
                "課税口座のポジションはありません（全ポジションが NISA 口座）。"
            )
            lines.append("")
            return "\n".join(lines)

        lines.append(report.markdown_table(
            ["コード", "銘柄名", "株数", "平均取得単価", "現在値", "取得原価",
             "評価額", "含み損益", "損益率", TAX_VALUE_COLUMN, TAX_COST_COLUMN],
            self._position_rows(self.taxable),
        ))
        lines.append("")
        lines.extend(self._manual_footnote(self.taxable))

        n_loss = sum(1 for v in self.taxable if v.is_loss)
        lines.append(
            f"- 集計: 課税口座 {len(self.taxable)} 銘柄のうち含み損 {n_loss} 銘柄、"
            f"含み損合計 {report.fmt_num(self.taxable_loss_total, 0)} 円。"
            f"**{TAX_VALUE_COLUMN}の合計 = 含み損合計 × 20.315%"
            f"（2025年時点の申告分離課税率）= {report.fmt_num(self.tax_value_total, 0)} 円**。"
        )
        lines.append(
            f"- 参考: 含み益合計 {report.fmt_num(self.taxable_gain_total, 0)} 円、"
            f"{TAX_COST_COLUMN}の合計 {report.fmt_num(self.tax_cost_total, 0)} 円"
            "（益出し側も同じ税率で条件付き試算。売却手数料等の譲渡費用は未考慮）。"
        )
        lines.append(
            f"- **「{TAX_VALUE_COLUMN}」は条件付きの上限の目安**: 実現した損失が"
            "税額を軽減するのは、(1) 同一年内の実現益・申告分離課税を選択した"
            "配当等と損益通算できた場合、または (2) 確定申告による繰越控除"
            "（翌年以降3年）の期間内に利益と相殺できた場合に限られる"
            f"（{TAX_DOC} 2.2節・2.3節）。通算・繰越の相手となる利益が無ければ"
            "税価値は生じない。"
        )
        lines.append("")
        return "\n".join(lines)

    def _nisa_markdown(self) -> str:
        lines: list[str] = []
        lines.append("## NISA口座の含み損益（損益通算・繰越控除の対象外）")
        lines.append("")
        if not self.nisa:
            lines.append(
                "NISA口座（nisa_tsumitate / nisa_growth）のポジションはありません"
                "（`account` 列が無い・空欄の行はすべて課税口座扱い）。"
            )
            lines.append("")
        else:
            lines.append(report.markdown_table(
                ["コード", "銘柄名", "口座区分", "株数", "平均取得単価", "現在値",
                 "取得原価", "評価額", "含み損益", "損益率", TAX_VALUE_COLUMN],
                [
                    [
                        v.code,
                        v.name,
                        v.account_label,
                        report.fmt_num(v.shares, 0),
                        report.fmt_num(v.avg_cost),
                        report.fmt_num(v.price) + ("※" if v.manual else ""),
                        report.fmt_num(v.cost_value, 0),
                        report.fmt_num(v.market_value, 0),
                        report.fmt_num(v.pnl, 0),
                        report.fmt_pct(v.pnl_pct),
                        "対象外（0円）" if v.is_loss else "-",
                    ]
                    for v in self.nisa
                ],
            ))
            lines.append("")
            lines.extend(self._manual_footnote(self.nisa))
            if self.nisa_loss_total < 0:
                reference = -self.nisa_loss_total * CAPITAL_GAINS_TAX_RATE
                lines.append(
                    f"- NISA口座の含み損合計は {report.fmt_num(self.nisa_loss_total, 0)} 円。"
                    "**同じ含み損が課税口座にあれば "
                    f"{report.fmt_num(reference, 0)} 円（× 20.315%、2025年時点）の"
                    "税価値（試算）に相当するが、NISA では 0 円**——この非対称が"
                    "課税口座との対比の要点。"
                )
        lines.append(
            "- **対象外の理由**: NISA口座内の譲渡損失は税務上「なかったもの」と"
            "され、課税口座の譲渡益・配当との損益通算も、翌年以降3年間の繰越控除も"
            "できない（2025年時点）。NISA は「利益が出れば税率0%、損失が出れば"
            "税務メリットもゼロ」という非対称な制度である"
            f"（{TAX_DOC} 「4.2 NISAの税制上の性格と注意点」参照）。"
        )
        lines.append("")
        return "\n".join(lines)

    def _notes_markdown(self) -> str:
        lines: list[str] = []
        lines.append("## 実務上の注意（自動挿入）")
        lines.append("")
        lines.append(
            "1. **同一銘柄を同日に買い戻すと実現できる損失が縮む**: 特定口座の"
            "取得単価は**総平均法に準ずる方法**で計算されるため、同じ日に売却と"
            "買付（いわゆる損出しクロス）を行うと、売却分の取得単価が同日の買付分と"
            "平均化され、実現される損失が上表の含み損より小さくなる（2025年時点の"
            "実務）。同日を避けて翌営業日以降に買い戻す方法が知られているが、"
            "その間の株価変動リスクを負うトレードオフがある。"
        )
        lines.append(
            "2. **繰越控除には確定申告が必要**: 損益通算してなお残った損失を"
            "翌年以降3年間繰り越すには確定申告が必要で、取引の無い年も申告を"
            f"継続しないと繰越が途切れる（{TAX_DOC} 「2.3 繰越控除」）。"
            "源泉徴収あり特定口座でも、複数の証券会社にまたがる損益通算や"
            "繰越控除には申告が要る。申告すると配偶者控除の判定や国民健康保険料の"
            "算定に影響しうる点も実務上の論点（同 2.1節）。"
        )
        lines.append(
            "3. **税価値のために投資判断を歪めない（tax tail wagging the dog）**: "
            "税価値は売却に付随する二次的な結果であって目的ではない。保有継続の"
            "判断はまず「今この価格で新規に買うか」（ゼロベース・テスト）で行い、"
            "税務はその後に考える。含み損の放置（塩漬け・処分効果）も、税価値だけを"
            "理由にした機械的な実現も、いずれも行動バイアスの現れになりうる"
            f"（{BEHAVIOR_DOC} の「実践的対処」参照）。"
        )
        lines.append("")
        return "\n".join(lines)

    def _assumptions_markdown(self) -> str:
        lines: list[str] = []
        lines.append("## データと前提")
        lines.append("")
        lines.append(
            "- 税率: 20.315%（所得税15% + 復興特別所得税0.315% + 住民税5%、"
            "申告分離課税、2025年時点。`stocklib.portfolio.CAPITAL_GAINS_TAX_RATE`）。"
        )
        if self.synthetic:
            lines.append(
                "- 現在値: 合成データ（--synthetic）の直近終値。"
                "**実データではない**（手入力行は `manual_price` の値）。"
            )
        else:
            lines.append(
                f"- 現在値: yfinance（非公式API）の期間 {self.period} の直近終値"
                f"（取得日: {self.as_of.isoformat()}。手入力行は `manual_price` の値）。"
            )
        lines.append(
            "- 「実現した場合の〜」はいずれも**条件付きの試算値**であり、実際の税額は"
            "年間の取引全体・配当の課税方式選択・他の証券会社の損益・繰越損失の有無"
            "等で変わる。売買手数料等の譲渡費用は考慮していない。"
        )
        lines.append("")
        return "\n".join(lines)


def resolve_last_prices(
    positions: Sequence[Position],
    *,
    period: str = "1y",
    synthetic: bool = False,
) -> dict[str, float]:
    """各ポジションの現在値（直近終値）を解決する。

    ``manual_price`` を持つ行は手入力値をそのまま使い（価格取得なし）、
    それ以外は :func:`stocklib.data.fetch_prices` の直近終値を使う。
    全行が手入力ならネットワーク不要で動く。

    Raises:
        DataFetchError: 価格取得に失敗した場合。
    """
    market_codes = [p.code for p in positions if p.manual_price is None]
    last: dict[str, float] = {}
    if market_codes:
        prices = fetch_prices(market_codes, period=period, synthetic=synthetic)
        for code in market_codes:
            last[code] = float(prices[code]["Close"].iloc[-1])
    for p in positions:
        if p.manual_price is not None:
            last[p.code] = p.manual_price
    return last


def build_tax_summary(
    positions: Sequence[Position],
    last_prices: dict[str, float],
    *,
    period: str = "1y",
    synthetic: bool = False,
) -> TaxValueSummary:
    """ポジションと現在値から :class:`TaxValueSummary` を構築する。

    ``account`` 列（``Position.account_type``。未指定は taxable 扱い）に基づき、
    課税口座と NISA 口座に分けて銘柄別ビュー（:class:`PositionTaxView`）を作る。

    Args:
        positions: :func:`stocklib.portfolio.load_portfolio` が返すポジションのリスト。
        last_prices: 銘柄コード → 現在値（:func:`resolve_last_prices` の結果）。
        period: 価格取得期間（レポートの前提表示に使う）。
        synthetic: True なら合成データ注記を出す。

    Raises:
        ValueError: positions が空の場合。
    """
    if not positions:
        raise ValueError("positions が空です（load_portfolio の結果を渡してください）")

    taxable: list[PositionTaxView] = []
    nisa: list[PositionTaxView] = []
    for p in positions:
        price = last_prices[p.code]
        mv = p.shares * price
        is_manual = p.manual_price is not None
        if is_manual:
            name = p.code
        else:
            name, _sector = _resolve_name_sector(p.code, synthetic=synthetic)
        view = PositionTaxView(
            code=p.code,
            name=name,
            account=p.account_type,
            shares=p.shares,
            avg_cost=p.avg_cost,
            price=price,
            cost_value=p.cost_value,
            market_value=mv,
            pnl=mv - p.cost_value,
            pnl_pct=mv / p.cost_value - 1.0,
            manual=is_manual,
        )
        (nisa if view.is_nisa else taxable).append(view)

    return TaxValueSummary(
        as_of=dt.date.today(),
        period=period,
        synthetic=synthetic,
        taxable=taxable,
        nisa=nisa,
    )


def build_report(summary: TaxValueSummary, source: Path) -> str:
    """レポート全文（Markdown）を構築する。"""
    n_total = len(summary.taxable) + len(summary.nisa)
    lines: list[str] = [report.report_header("課税口座の含み損益 税価値ビュー")]
    lines.append(
        f"- 保有情報: {source}（{n_total} 銘柄 = 課税口座 {len(summary.taxable)} + "
        f"NISA {len(summary.nisa)}） / 価格期間: {summary.period}"
    )
    if summary.synthetic:
        lines.append(
            "- **データ: 合成データ（--synthetic）による手法デモであり、実データではありません**"
        )
    lines.append(
        "- 本レポートは含み損益の税務上の取り扱いを**条件付きで**整理した判断材料であり、"
        "損出し・売却・保有継続のいずれも判断しない。"
    )
    lines.append("")
    lines.append(summary.to_markdown())
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="保有銘柄 CSV から課税口座の含み損益 税価値ビュー（損出し・損益通算の"
        "判断材料の機械的整理）レポートを生成する",
    )
    parser.add_argument(
        "--file", type=Path, default=None,
        help=f"ポートフォリオ CSV のパス（既定: {DEFAULT_PORTFOLIO_CSV}）",
    )
    parser.add_argument("--period", default="1y", help="価格取得期間（既定: 1y）")
    parser.add_argument(
        "--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）",
    )
    args = parser.parse_args(argv)

    path: Path = args.file if args.file is not None else DEFAULT_PORTFOLIO_CSV
    if not path.exists():
        if args.file is None:
            print(
                "data/portfolio.csv を作成してください。"
                f"テンプレート: analysis/templates/portfolio-example.csv（{TEMPLATE_CSV}）",
                file=sys.stderr,
            )
        else:
            print(
                f"エラー: ポートフォリオ CSV が見つかりません: {path}\n"
                "テンプレート: analysis/templates/portfolio-example.csv",
                file=sys.stderr,
            )
        return 1

    try:
        positions = load_portfolio(path)
        last_prices = resolve_last_prices(
            positions, period=args.period, synthetic=args.synthetic,
        )
        summary = build_tax_summary(
            positions, last_prices, period=args.period, synthetic=args.synthetic,
        )
        content = build_report(summary, path)
    except (PortfolioValidationError, DataFetchError, ValueError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    filename = f"tax-{dt.date.today().isoformat()}.md"
    out = report.save_report(content, filename)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

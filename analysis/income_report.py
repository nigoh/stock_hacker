#!/usr/bin/env python3
"""配当インカム・レポートを生成する CLI。

使い方（リポジトリルートから）:
    python3 analysis/income_report.py [--file data/portfolio.csv] [--period 1y] [--synthetic]

保有銘柄 CSV（列: code,shares,avg_cost,acquired_date,memo,fx_at_cost,account,
target_weight,manual_price。stocklib.portfolio.load_portfolio と同一形式）を読み込み、
銘柄ごとに直近12ヶ月（TTM）の実績1株配当（yfinance Ticker.dividends）× 株数で
年間受取配当見込みを推計し、取得原価ベースの YOC・時価ベースの配当利回り・
税引後の手取り・月割り額をまとめた reports/income-<日付>.md を生成し、
そのパスを stdout に出力する。

任意列 manual_price を持つ行（投資信託・現金などの手入力評価行）は配当・価格の
取得対象から外し、年間配当 0 として集計に含める（投信の分配金・現金の利息は
本レポートの対象外である旨をレポートに脚注で明記する）。

任意列 account（nisa_tsumitate / nisa_growth / taxable。空欄・列なしは taxable 扱い）
がある場合は、NISA口座分を非課税・課税口座分を源泉徴収 20.315%（2025年時点、
stocklib.portfolio の税率定数を再利用）として手取りを併記し、NISA分の非課税メリット
（年間フロー = NISA分税引前配当 × 20.315%）を試算する。NISA配当の非課税には
株式数比例配分方式の選択が必要（knowledge/regulation-tax/taxation-and-nisa.md）である
旨をレポートに必ず注記する。

実績配当（TTM）は将来の配当を保証せず（減配リスク）、yfinance の配当データは日本株で
欠損・調整不備がありうる（knowledge/data-sources/data-apis-and-tools.md）。レポート本文に
前提と限界の節として明記される。--synthetic では合成配当系列（シード固定・年2回の
半期配当）で全機能がネットワーク不要で動く（レポートに合成データである旨を明記）。

保有情報は既定で data/portfolio.csv に置く（data/ は gitignore 対象のため git 管理外）。
テンプレート: analysis/templates/portfolio-example.csv
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from stocklib import report
from stocklib.data import (
    REPO_ROOT,
    DataFetchError,
    add_source_argument,
    set_default_source,
)
from stocklib.income import IncomeReport, build_income_report
from stocklib.portfolio import PortfolioValidationError, load_portfolio

DEFAULT_PORTFOLIO_CSV: Path = REPO_ROOT / "data" / "portfolio.csv"
TEMPLATE_CSV: Path = REPO_ROOT / "analysis" / "templates" / "portfolio-example.csv"


def build_report(income: IncomeReport, source: Path) -> str:
    """レポート本文（Markdown）を構築する。"""
    lines: list[str] = [report.report_header("配当インカム・レポート")]
    lines.append(f"- 保有情報: {source}（{len(income.positions)} 銘柄）")
    lines.append(f"- 基準日: {income.as_of.isoformat()}（直近12ヶ月の実績配当を集計）")
    if income.synthetic:
        lines.append(
            "- **データ: 合成データ（--synthetic）による手法デモであり、実データではありません**"
        )
    else:
        lines.append(
            f"- データソース: yfinance（Ticker.dividends、非公式 API）、"
            f"取得日 {dt.date.today().isoformat()}。現在値は同ソースの直近終値"
            f"（期間 {income.period}）。"
        )
    lines.append("")
    lines.append(income.to_markdown())
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="保有銘柄 CSV から年間受取配当見込み（TTM実績ベース）と"
        "NISA非課税メリットをまとめた配当インカム・レポートを生成する"
    )
    parser.add_argument(
        "--file", type=Path, default=None,
        help=f"ポートフォリオ CSV のパス（既定: {DEFAULT_PORTFOLIO_CSV}）",
    )
    parser.add_argument(
        "--period", default="1y",
        help="現在値（時価利回りの分母）の価格取得期間（既定: 1y）",
    )
    parser.add_argument(
        "--synthetic", action="store_true",
        help="合成データで実行（ネットワーク不要。レポートに合成データである旨を明記）",
    )
    add_source_argument(parser)
    args = parser.parse_args(argv)
    set_default_source(args.source)

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
        income = build_income_report(
            positions,
            period=args.period,
            synthetic=args.synthetic,
        )
        content = build_report(income, path)
    except (PortfolioValidationError, DataFetchError, ValueError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    filename = f"income-{dt.date.today().isoformat()}.md"
    out = report.save_report(content, filename)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""実運用パフォーマンス・レポート（金額加重リターン XIRR）を生成する CLI。

使い方（リポジトリルートから）:
    python3 analysis/performance_report.py [--file data/transactions.csv]
        [--benchmark ^N225] [--period 3y] [--synthetic]

取引履歴 CSV（列: date,code,side,shares,price,fee,account,memo。side は
buy / sell / dividend / deposit / withdraw。fee・account・memo は省略可）を読み込み、
「自分の運用はこれまで実際に年率何%だったのか」を測る:

- **金額加重リターン（MWR = XIRR）**: 投資家自身のキャッシュフロー（いつ・いくら
  投下・回収したか）の内部収益率。ニュートン法 + 二分法フォールバックで解く。
- **期間損益**: 実現損益 + 未実現損益 + 受取配当（税・手数料控除後）の内訳。
- **ベンチマーク比較**: 同じキャッシュフローを ^N225（または 1306.T 等）に投じた
  場合の XIRR・終端評価額との比較（市場要因と自分の判断の寄与を分離する材料）。

reports/performance-<日付>.md を生成し、そのパスを stdout の最終行に出力する
（免責文は stocklib.report.save_report が自動付与）。

取引履歴は既定で data/transactions.csv に置く（data/ は gitignore 対象のため
git 管理外）。テンプレート: analysis/templates/transactions-example.csv。
計測ロジックは stocklib.performance、理論的背景は
knowledge/math/performance-measurement-and-attribution.md を参照。
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
from stocklib.performance import (
    DEFAULT_BENCHMARK,
    PerformanceResult,
    TransactionValidationError,
    evaluate_performance,
    load_transactions,
)

DEFAULT_TRANSACTIONS_CSV: Path = REPO_ROOT / "data" / "transactions.csv"
TEMPLATE_CSV: Path = REPO_ROOT / "analysis" / "templates" / "transactions-example.csv"


def _interpretation_lines(result: PerformanceResult) -> list[str]:
    """解釈の枠組みと実績値の外挿に関する注意（knowledge 文書の要約）。"""
    bench = result.benchmark.benchmark
    return [
        "## 解釈の枠組み（MWR と TWR、ベンチマーク比較の意味）",
        "",
        "- 本レポートの **金額加重リターン（MWR = XIRR）** は「いつ・いくら入金"
        "（投下）したか」という**入金タイミングの巧拙まで含んだ、投資家個人の"
        "損益実感に対応する数値**である。",
        "- 運用対象そのものの成績（銘柄選択の巧拙）を測る **時間加重リターン"
        "（TWR）** とは異なる。同じ銘柄を持っていても、高値で買い増していれば "
        "MWR は TWR を下回る。自分の売買タイミングの検証には MWR、銘柄選択力の"
        "評価には TWR と、目的に応じて使い分ける。",
        f"- **ベンチマーク比較**（同じキャッシュフローを {bench} に投じた場合の "
        "XIRR）は、リターンのうち「市場全体に乗っていただけの部分」（市場要因）と"
        "「銘柄選択・タイミングの寄与」を**分離するための材料**である。差の符号だけで"
        "優劣を断定できるものではない。",
        "- 詳細: `knowledge/math/performance-measurement-and-attribution.md`"
        "（TWR/MWR の定義・ベンチマーク選択・巧拙と運の統計的検定）。",
        "",
        "## 実績値の外挿に関する注意（asset_plan との接続）",
        "",
        "- **実績 XIRR を asset_plan progress の --return（想定リターン）にそのまま"
        "入力しないこと。** 実績リターンは (1) 観測期間が短いほど運の寄与が大きい"
        "（情報比率 0.5 の運用者がスキルを 5% 水準で統計的に立証するには約 16 年を"
        "要する）、(2) 入金タイミング要因を含む、(3) 好成績の期間の後には平均回帰が"
        "起きうる——ため、将来の期待リターンの推定値としては上下どちらにも偏りうる。",
        "- 資産形成プランの想定リターンは市場インデックスの長期期待リターンを軸に"
        "保守的に置き、実績 XIRR は「プランとの乖離を定点観測する実績値」として"
        "扱うのが本環境の推奨である（想定と実績の突き合わせ自体には本レポートを"
        "継続的に使える）。",
        "- 本レポートは成績の良し悪しを評価・断定しない。数値と比較材料の提示に"
        "徹する。",
        "",
    ]


def build_report(result: PerformanceResult, source: Path) -> str:
    """レポート本文（Markdown）を構築する。"""
    lines: list[str] = [report.report_header("実運用パフォーマンス・レポート（金額加重リターン）")]
    lines.append(f"- 取引履歴: {source}")
    lines.append(f"- ベンチマーク: {result.benchmark.benchmark} / 評価日: {result.end_date}")
    if result.synthetic:
        lines.append(
            "- **データ: 合成データ（--synthetic）による手法デモであり、実データではありません**"
        )
    else:
        lines.append(
            f"- データソース: yfinance（非公式 API・調整済み終値、取得日: {dt.date.today()}）。"
            "分割・配当調整の不備がありうるため、異常な数値はまずデータ品質を疑うこと"
        )
    lines.append("")
    lines.append(result.to_markdown())
    lines.extend(_interpretation_lines(result))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="取引履歴 CSV から実運用パフォーマンス（XIRR・損益・ベンチマーク比較）レポートを生成する"
    )
    parser.add_argument(
        "--file", type=Path, default=None,
        help=f"取引履歴 CSV のパス（既定: {DEFAULT_TRANSACTIONS_CSV}）",
    )
    parser.add_argument(
        "--benchmark", default=DEFAULT_BENCHMARK,
        help="比較対象のティッカー（既定: ^N225。配当込みの比較には分配金調整済みの "
        "1306.T 等を推奨）",
    )
    parser.add_argument(
        "--period", default=None,
        help="価格取得期間（yfinance 形式、例: 3y。省略時は最初の取引日から自動導出）",
    )
    parser.add_argument("--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）")
    add_source_argument(parser)
    args = parser.parse_args(argv)
    set_default_source(args.source)

    path: Path = args.file if args.file is not None else DEFAULT_TRANSACTIONS_CSV
    if not path.exists():
        if args.file is None:
            print(
                "data/transactions.csv を作成してください。"
                f"テンプレート: analysis/templates/transactions-example.csv（{TEMPLATE_CSV}）",
                file=sys.stderr,
            )
        else:
            print(
                f"エラー: 取引履歴 CSV が見つかりません: {path}\n"
                "テンプレート: analysis/templates/transactions-example.csv",
                file=sys.stderr,
            )
        return 1

    try:
        transactions = load_transactions(path)
        result = evaluate_performance(
            transactions,
            benchmark=args.benchmark,
            synthetic=args.synthetic,
            period=args.period,
        )
        content = build_report(result, path)
    except (TransactionValidationError, DataFetchError, ValueError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    filename = f"performance-{dt.date.today().isoformat()}.md"
    out = report.save_report(content, filename)
    if result.xirr_value is not None:
        print(f"実績 XIRR: 年率 {result.xirr_value * 100:.2f}%（計測期間 {result.span_years:.2f} 年）")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

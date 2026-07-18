#!/usr/bin/env python3
"""リサーチジャーナル CLI — 分析仮説の記録と事後検証。

「分析のやりっぱなし」をなくすためのツール。分析で得た仮説を
``journal/<YYYY>/<日付>-<slug>.md`` に記録し（記録時点の終値を自動スナップショット）、
検証予定日が来たら現在値・同期間のベンチマーク（^N225）騰落と突き合わせて
hit / miss / mixed を機械的に判定する。

使い方（リポジトリルートから）:

    # 1. 仮説を記録（雛形生成。生成後に ## 仮説 / ## 根拠 / ## 反証条件 を必ず記入）
    python3 analysis/research_journal.py new --codes 7203 --title "決算後の上方修正期待" \\
        --direction up --review-days 60

    # 複数銘柄・銘柄ごとの方向指定も可能
    python3 analysis/research_journal.py new --codes 7203 6758 \\
        --title "..." --direction 7203:up,6758:down --review-days 30

    # 2. 検証期日が来たエントリの確認（status: open かつ review_date <= 今日）
    python3 analysis/research_journal.py due

    # 3. 検証の実行（## 検証結果 を追記し frontmatter を reviewed に更新）
    python3 analysis/research_journal.py verify journal/2026/2026-07-16-....md

    # 全エントリのサマリー
    python3 analysis/research_journal.py list

全サブコマンドで ``--synthetic``（合成データ・ネットワーク不要）が使える。
``new --synthetic`` は frontmatter に機械可読な ``data: synthetic`` を書き込み、
``verify`` はエントリの ``data``（欠落時は real 扱い）と ``--synthetic`` 指定が
食い違うと拒否する（合成スナップショットと実データの比較は判定として無意味なため）。
判定ロジックの詳細は ``stocklib.journal.judge_direction`` の docstring を参照。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from stocklib import journal, report
from stocklib.data import DataFetchError, add_source_argument, set_default_source
from stocklib.journal import JOURNAL_DIR, JournalEntry, JournalError


def _direction_label(entry: JournalEntry) -> str:
    """direction を表示用文字列にする（全銘柄同一なら1語、異なれば code:dir 列挙）。"""
    directions = set(entry.direction.values())
    if len(directions) == 1:
        return next(iter(directions))
    return ",".join(f"{c}:{entry.direction[c]}" for c in entry.codes)


def _entry_row(entry: JournalEntry) -> list[object]:
    return [
        entry.id,
        entry.date.isoformat(),
        " ".join(entry.codes),
        _direction_label(entry),
        entry.review_date.isoformat(),
        entry.status,
        entry.outcome,
    ]


_TABLE_HEADERS = ["id", "date", "codes", "direction", "review_date", "status", "outcome"]


def cmd_new(args: argparse.Namespace) -> int:
    entry = journal.new_entry(
        codes=[c.strip() for c in args.codes],
        title=args.title,
        direction=args.direction,
        review_days=args.review_days,
        synthetic=args.synthetic,
        benchmark=args.benchmark,
        slug=args.slug,
        journal_dir=args.journal_dir,
    )
    assert entry.path is not None
    print("エントリ雛形を作成しました。## 仮説 / ## 根拠 / ## 反証条件 を必ず記入してください。")
    print(f"検証予定日: {entry.review_date.isoformat()}（`research_journal.py due` で期日確認）")
    print(entry.path)
    return 0


def cmd_due(args: argparse.Namespace) -> int:
    entries = journal.iter_entries(args.journal_dir)
    due = journal.due_entries(entries)
    if not due:
        n_open = sum(1 for e in entries if e.status == "open")
        print(f"検証期日を迎えたエントリはありません（open: {n_open} 件 / 全 {len(entries)} 件）。")
        return 0
    print(f"検証期日を迎えたエントリ: {len(due)} 件")
    print()
    print(report.markdown_table(_TABLE_HEADERS, [_entry_row(e) for e in due]))
    print()
    print("verify で検証してください: python3 analysis/research_journal.py verify <path>")
    for e in due:
        print(f"  {e.path}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    entry = journal.load_entry(args.path)
    verdicts = journal.verify_entry(entry, synthetic=args.synthetic)
    print(f"検証しました: {entry.title}（{entry.id}）")
    for v in verdicts:
        print(
            f"  {v.code} direction={v.direction} 騰落率={v.stock_return * 100:+.2f}% "
            f"ベンチ={v.benchmark_return * 100:+.2f}% 超過={v.excess_return * 100:+.2f}% → {v.result}"
        )
    print(f"総合判定: {entry.outcome}")
    print(entry.path)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    entries = journal.iter_entries(args.journal_dir)
    if not entries:
        print(f"エントリがありません（{args.journal_dir}）。new で作成してください。")
        return 0
    print(report.markdown_table(_TABLE_HEADERS, [_entry_row(e) for e in entries]))
    reviewed = [e for e in entries if e.status == "reviewed"]
    print()
    print(
        f"全 {len(entries)} 件 / open {len(entries) - len(reviewed)} 件 / reviewed {len(reviewed)} 件"
        + (
            "（hit {h} / miss {m} / mixed {x}）".format(
                h=sum(1 for e in reviewed if e.outcome == "hit"),
                m=sum(1 for e in reviewed if e.outcome == "miss"),
                x=sum(1 for e in reviewed if e.outcome == "mixed"),
            )
            if reviewed
            else ""
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="リサーチジャーナル（分析仮説の記録と事後検証）",
    )
    parser.add_argument(
        "--journal-dir", type=Path, default=JOURNAL_DIR,
        help=f"ジャーナルのルートディレクトリ（既定: {JOURNAL_DIR}。主にテスト用）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new", help="仮説エントリの雛形を生成する（終値を自動スナップショット）")
    p_new.add_argument("--codes", nargs="+", required=True, help="対象銘柄コード（例: 7203 6758）")
    p_new.add_argument("--title", required=True, help="仮説のタイトル（例: 決算後の上方修正期待）")
    p_new.add_argument(
        "--direction", required=True,
        help="想定方向: up/down/neutral（全銘柄共通）または '7203:up,6758:down'（銘柄ごと）",
    )
    p_new.add_argument("--review-days", type=int, default=60, help="検証予定日までの日数（既定: 60）")
    p_new.add_argument("--benchmark", default=journal.DEFAULT_BENCHMARK,
                       help=f"ベンチマークティッカー（既定: {journal.DEFAULT_BENCHMARK}）")
    p_new.add_argument("--slug", default=None, help="ファイル名スラッグ（省略時はタイトル/コードから生成）")
    p_new.add_argument("--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）")
    add_source_argument(p_new)
    p_new.set_defaults(func=cmd_new)

    p_due = sub.add_parser("due", help="検証期日を迎えた open エントリを一覧する")
    p_due.add_argument("--synthetic", action="store_true",
                       help="他コマンドとの一貫性のために受け付ける（due は価格取得を行わない）")
    p_due.set_defaults(func=cmd_due)

    p_verify = sub.add_parser("verify", help="エントリを検証し ## 検証結果 を追記する")
    p_verify.add_argument("path", type=Path, help="エントリファイルのパス")
    p_verify.add_argument("--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）")
    add_source_argument(p_verify)
    p_verify.set_defaults(func=cmd_verify)

    p_list = sub.add_parser("list", help="全エントリのサマリーテーブルを表示する")
    p_list.add_argument("--synthetic", action="store_true",
                        help="他コマンドとの一貫性のために受け付ける（list は価格取得を行わない）")
    p_list.set_defaults(func=cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    set_default_source(getattr(args, "source", None))
    try:
        return int(args.func(args))
    except (JournalError, DataFetchError, ValueError, OSError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

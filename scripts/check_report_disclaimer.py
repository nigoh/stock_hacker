#!/usr/bin/env python3
"""PostToolUse フック: reports/ 配下のレポートに免責文が含まれているか検査する。

CLI 生成レポート（analyze_stock.py 等）は stocklib.report が免責文
（stocklib.report.DISCLAIMER）を自動追記するが、/market スキルや加筆・再構成の
ように Claude が Write/Edit で直接書く reports/*.md には機械的な担保がない。

Claude Code の PostToolUse（Write|Edit）フックとして stdin から JSON を受け取り、
書き込まれたファイルが reports/ 配下の Markdown なのに本文に
「投資助言ではありません」（または「免責」）を含まない場合、exit 2
（stderr のメッセージが Claude にフィードバックされ、免責文の追加を促す）。
それ以外は exit 0。

手動テスト用に、引数でファイルパスを直接渡すこともできる:
    python3 scripts/check_report_disclaimer.py reports/analyze-7203-2026-07-16.md
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 免責文とみなすキーワード（いずれか1つを含めば合格）
_DISCLAIMER_KEYWORDS: tuple[str, ...] = ("投資助言ではありません", "免責")

_FEEDBACK_MESSAGE: str = (
    "レポート末尾に免責文を追加してください（stocklib.report.DISCLAIMER 参照）。"
    "reports/ 配下のレポートには「投資助言ではありません」を含む免責の一文が必須です"
    "（CLAUDE.md の規約）。"
)


def repo_root() -> Path:
    """リポジトリルートを返す（フック実行時は CLAUDE_PROJECT_DIR、無ければこのファイルの親の親）。"""
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def target_file_path(argv: list[str]) -> str | None:
    """検査対象のファイルパスを引数または stdin の JSON（tool_input.file_path）から得る。"""
    if len(argv) > 1:
        return argv[1]
    if sys.stdin.isatty():
        return None
    raw = sys.stdin.read()
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    file_path = tool_input.get("file_path")
    return file_path if isinstance(file_path, str) else None


def has_disclaimer(text: str) -> bool:
    """本文に免責文（キーワードのいずれか）が含まれているかを返す。"""
    return any(keyword in text for keyword in _DISCLAIMER_KEYWORDS)


def main(argv: list[str]) -> int:
    file_path = target_file_path(argv)
    if not file_path:
        return 0  # 対象パスが取れない場合は何もしない（ブロックしない）

    root = repo_root()
    reports_dir = (root / "reports").resolve()
    path = Path(file_path)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()

    # reports/ 配下の Markdown のみ検査対象
    try:
        path.relative_to(reports_dir)
    except ValueError:
        return 0
    if path.suffix.lower() != ".md":
        return 0
    if not path.is_file():
        return 0  # 書き込み後に消えている等の異常時はブロックしない

    text = path.read_text(encoding="utf-8")
    if has_disclaimer(text):
        return 0

    print(_FEEDBACK_MESSAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

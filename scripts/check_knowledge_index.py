#!/usr/bin/env python3
"""PostToolUse フック: knowledge/ 配下の文書が 00-index.md に索引されているか検査する。

Claude Code の PostToolUse（Write|Edit）フックとして stdin から JSON を受け取り、
書き込まれたファイルが knowledge/ 配下の Markdown 文書なのに knowledge/00-index.md
から参照されていない場合、exit 2（stderr のメッセージが Claude にフィードバックされ、
索引への反映を促す）。それ以外は exit 0。

手動テスト用に、引数でファイルパスを直接渡すこともできる:
    python3 scripts/check_knowledge_index.py knowledge/math/new-doc.md

リポジトリ全体の整合チェック（CI 用）は --all で実行する:
    python3 scripts/check_knowledge_index.py --all

--all モードは以下の4点を検査し、違反があれば全件を stderr に列挙して exit 1:
  (1) knowledge/**/*.md の全文書が 00-index.md からリンクされている
  (2) 00-index.md 内の全 .md リンクが実在ファイルに解決する
  (3) 索引冒頭の「全N文書」の N が実ファイル数（索引自身を除く）と一致する
  (4) 各文書の「関連トピック」節にある相対 .md リンクが実在ファイルに解決する
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# [テキスト](ターゲット) 形式の Markdown リンクのターゲット部分を抜き出す
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
# 索引冒頭の「全N文書」表記
_DOC_COUNT_RE = re.compile(r"全\s*(\d+)\s*文書")


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


def extract_md_link_targets(text: str) -> list[str]:
    """Markdown テキストから .md へのローカルリンクのターゲットを抽出する。

    外部リンク（scheme あり）とページ内アンカーは無視し、
    フラグメント（#...）は除去して返す。
    """
    targets: list[str] = []
    for raw in _MD_LINK_RE.findall(text):
        target = raw.split("#", 1)[0]
        if not target:
            continue  # 純粋なアンカーリンク
        if "://" in target or target.startswith("mailto:"):
            continue
        if not target.lower().endswith(".md"):
            continue
        targets.append(target)
    return targets


def related_topics_section(text: str) -> str:
    """文書テキストから「関連トピック」節（見出し〜次の ## 見出しまで）を抜き出す。無ければ空文字。"""
    lines = text.splitlines()
    collected: list[str] = []
    in_section = False
    for line in lines:
        if re.match(r"^##\s*関連トピック\s*$", line):
            in_section = True
            continue
        if in_section and re.match(r"^##\s", line):
            break
        if in_section:
            collected.append(line)
    return "\n".join(collected)


def check_all(root: Path) -> list[str]:
    """リポジトリ全体の索引整合を検査し、違反メッセージのリストを返す（空なら合格）。"""
    violations: list[str] = []
    knowledge_dir = (root / "knowledge").resolve()
    index_path = knowledge_dir / "00-index.md"

    if not knowledge_dir.is_dir():
        return [f"knowledge/ ディレクトリが存在しません: {knowledge_dir}"]
    if not index_path.is_file():
        return ["knowledge/00-index.md が存在しません。索引ファイルを作成してください。"]

    docs = sorted(
        p.resolve()
        for p in knowledge_dir.rglob("*.md")
        if p.resolve() != index_path.resolve()
    )
    index_text = index_path.read_text(encoding="utf-8")

    # (2) 索引内の全 .md リンクが実在ファイルに解決する
    referenced: set[Path] = set()
    for target in extract_md_link_targets(index_text):
        resolved = (knowledge_dir / target).resolve()
        if not resolved.is_file():
            violations.append(
                f"索引のリンク切れ: knowledge/00-index.md 内のリンク '{target}' が実在ファイルに解決しません。"
            )
            continue
        referenced.add(resolved)

    # (1) 全文書が索引からリンクされている
    for doc in docs:
        if doc not in referenced:
            rel = doc.relative_to(knowledge_dir).as_posix()
            violations.append(
                f"未索引の文書: knowledge/{rel} が knowledge/00-index.md からリンクされていません。"
            )

    # (3) 「全N文書」の N が実ファイル数と一致する
    count_match = _DOC_COUNT_RE.search(index_text)
    if count_match is None:
        violations.append(
            "文書数の表記なし: knowledge/00-index.md 冒頭に「全N文書」の表記が見つかりません。"
        )
    else:
        declared = int(count_match.group(1))
        actual = len(docs)
        if declared != actual:
            violations.append(
                f"文書数の不一致: 索引には「全{declared}文書」とありますが、"
                f"実際の文書数（00-index.md を除く）は {actual} です。"
            )

    # (4) 各文書の「関連トピック」節の相対リンクが解決する
    for doc in docs:
        section = related_topics_section(doc.read_text(encoding="utf-8"))
        if not section:
            continue
        rel = doc.relative_to(knowledge_dir).as_posix()
        for target in extract_md_link_targets(section):
            resolved = (doc.parent / target).resolve()
            if not resolved.is_file():
                violations.append(
                    f"関連トピックのリンク切れ: knowledge/{rel} の「関連トピック」節のリンク "
                    f"'{target}' が実在ファイルに解決しません。"
                )

    return violations


def run_all_mode(root: Path) -> int:
    """--all モードの実行本体。違反を列挙して exit code を返す。"""
    violations = check_all(root)
    if violations:
        print(f"ナレッジ索引の整合チェック: {len(violations)} 件の違反", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1
    knowledge_dir = root / "knowledge"
    n_docs = sum(1 for p in knowledge_dir.rglob("*.md")) - 1
    print(f"ナレッジ索引の整合チェック: OK（全{n_docs}文書、違反なし）")
    return 0


def main(argv: list[str]) -> int:
    if "--all" in argv[1:]:
        return run_all_mode(repo_root())

    file_path = target_file_path(argv)
    if not file_path:
        return 0  # 対象パスが取れない場合は何もしない（ブロックしない）

    root = repo_root()
    knowledge_dir = (root / "knowledge").resolve()
    path = Path(file_path)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()

    # knowledge/ 配下の Markdown 文書のみ検査対象
    try:
        rel = path.relative_to(knowledge_dir)
    except ValueError:
        return 0
    if path.suffix.lower() != ".md":
        return 0
    if rel.as_posix() == "00-index.md":
        return 0

    index_path = knowledge_dir / "00-index.md"
    if not index_path.is_file():
        print(
            "knowledge/00-index.md が存在しません。索引ファイルを作成し、"
            f"'{rel.as_posix()}' への参照を追加してください（CLAUDE.md の規約）。",
            file=sys.stderr,
        )
        return 2

    index_text = index_path.read_text(encoding="utf-8")
    if rel.as_posix() in index_text:
        return 0

    print(
        f"未索引の knowledge 文書: knowledge/{rel.as_posix()} が knowledge/00-index.md "
        "から参照されていません。CLAUDE.md の規約に従い、該当カテゴリの節に "
        f"[タイトル]({rel.as_posix()}) 形式で索引エントリ（1〜2行の要約付き）を追加してください。",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

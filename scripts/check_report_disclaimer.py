#!/usr/bin/env python3
"""PostToolUse フック: 外部に出る成果物に免責文が含まれているか検査する。

**なぜこの検査があるのか（防いでいる事故）**

本リポジトリの出力は投資助言ではなく分析支援である、という前提が CLAUDE.md の
不変条件になっている。CLI が生成するレポートは :data:`stocklib.report.DISCLAIMER`
が、ジャーナルエントリは :data:`stocklib.journal.DISCLAIMER` が自動で免責を入れるが、
**Claude が Write/Edit で直接書く/加筆する成果物には機械的な担保がない**
（/market スキルのレポート、ジャーナル本文の書き足し、サイトのページ追加など）。
免責の無い「銘柄コード + 方向 + 予想」という並びは、形式上もっとも投資助言に
見えやすい。この検査は、その状態のファイルが**公開・共有されるのを止める**ための
最後のガードレールである。**消さないこと。**

検査対象（いずれも人目に触れる／公開される成果物）:

- ``reports/**/*.md`` — 分析レポート。外部共有・意思決定に使われる
- ``journal/**/*.md`` — リサーチジャーナル。**git 管理対象で GitHub 上に公開される**
  （``journal/README.md`` は書式の説明であって分析記録ではないため除外）
- ``docs/*.html`` — GitHub Pages で公開されるサイトのページ

``docs/*.md``（getting-started.md・automation.md 等）は**意図的に対象外**である。
これらは環境の使い方を説明する運用ガイドであって分析結果の提示ではなく、
免責を強制すると実態に合わない検査で運用が壊れる（過剰検査を避ける判断）。

**キーワードの厳格さ**: かつては「投資助言ではありません」**または**「免責」の
どちらかを含めば合格だった。これは緩すぎて、「免責事項は後述」のように免責を
**書いていない**文書でも「免責」の2文字だけで通過してしまう。現在は
「投資助言である」ことを**明示的に否定する文言**を必須とする（:data:`_REQUIRED_PHRASES`）。
対象ごとに実際の定型文（report.DISCLAIMER / journal.DISCLAIMER / サイトのフッター）
に合わせた語を許容する。

Claude Code の PostToolUse（Write|Edit）フックとして stdin から JSON を受け取り、
書き込まれたファイルが対象で免責を欠く場合 exit 2（stderr のメッセージが Claude に
フィードバックされ、免責文の追加を促す）。それ以外は exit 0。
なお matcher は ``Write|Edit`` のみで、Bash 経由の書き込みは検査されない
（誤爆が多くなるため意図的にそうしている）。``--all`` の一括検査が受け皿になる。

手動実行:
    python3 scripts/check_report_disclaimer.py reports/analyze-7203-2026-07-16.md
    python3 scripts/check_report_disclaimer.py --all   # 対象ファイルを一括検査
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import NamedTuple

# 「投資助言ではない」ことを明示的に否定する文言。単なる「免責」の語は不可。
_ADVICE_DENIAL: str = "投資助言ではありません"

# サイトのフッターで使われている否定表現（docs/*.html の現行文言）。
_ADVICE_DENIAL_SITE: str = "投資助言ではなく分析支援"


class Scope(NamedTuple):
    """免責検査の対象範囲を表す。

    （dataclass ではなく NamedTuple なのは、このスクリプトが importlib で
    ``sys.modules`` に登録せず読み込まれる場合——テストがそうしている——でも
    型解決に失敗しないようにするため。）

    Attributes:
        name: 表示用の名称（``reports/`` 等）。
        rel_dir: リポジトリルートからの相対ディレクトリ。
        pattern: ``--all`` で走査するときの glob パターン。
        suffix: 対象とする拡張子（小文字）。
        exclude_names: 対象から外すファイル名（ベース名で比較）。
        required: いずれか1つを含めば合格とする文言。
        guidance: 不合格時に提示する具体的な直し方。
    """

    name: str
    rel_dir: str
    pattern: str
    suffix: str
    exclude_names: frozenset[str]
    required: tuple[str, ...]
    guidance: str


_SCOPES: tuple[Scope, ...] = (
    Scope(
        name="reports/",
        rel_dir="reports",
        pattern="**/*.md",
        suffix=".md",
        exclude_names=frozenset(),
        required=(_ADVICE_DENIAL,),
        guidance="レポート末尾に stocklib.report.DISCLAIMER の免責文を追加してください",
    ),
    Scope(
        name="journal/",
        rel_dir="journal",
        pattern="**/*.md",
        suffix=".md",
        # README.md は書式の説明であって分析記録ではない
        exclude_names=frozenset({"README.md"}),
        required=(_ADVICE_DENIAL,),
        guidance=(
            "エントリ冒頭に stocklib.journal.DISCLAIMER の免責文を追加してください"
            "（journal/ は git 管理対象で GitHub 上に公開されます）"
        ),
    ),
    Scope(
        name="docs/",
        rel_dir="docs",
        # サイトのページはフラット構成（サブディレクトリは assets/ のみ）
        pattern="*.html",
        suffix=".html",
        exclude_names=frozenset(),
        required=(_ADVICE_DENIAL, _ADVICE_DENIAL_SITE),
        guidance=(
            "ページのフッターに免責（「投資助言ではなく分析支援です」）を追加してください"
            "（docs/ は GitHub Pages で公開されます）"
        ),
    ),
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


def scope_for(path: Path, root: Path) -> Scope | None:
    """``path`` が属する検査対象範囲を返す（対象外なら ``None``）。

    ディレクトリ・拡張子・除外ファイル名で判定する。パスは解決済みを前提とする。
    """
    for scope in _SCOPES:
        base = (root / scope.rel_dir).resolve()
        try:
            relative = path.relative_to(base)
        except ValueError:
            continue
        if path.suffix.lower() != scope.suffix:
            continue
        if path.name in scope.exclude_names:
            continue
        # docs/ はフラット構成のみ対象（assets/ 配下などは対象外）
        if scope.pattern.count("/") == 0 and len(relative.parts) > 1:
            continue
        return scope
    return None


def has_disclaimer(text: str, scope: Scope) -> bool:
    """本文が対象範囲の要求する免責文言を含むかを返す。

    「免責」の語だけでは合格しない（「免責事項は後述」のような**免責を書いていない**
    文書を通してしまうため）。投資助言であることを明示的に否定する文言が必要。
    """
    return any(phrase in text for phrase in scope.required)


def feedback_message(scope: Scope, path: Path) -> str:
    """不合格時に stderr へ出すメッセージを組み立てる。"""
    phrases = " / ".join(f"「{p}」" for p in scope.required)
    return (
        f"{path} に免責文がありません。{scope.guidance}。"
        f"{scope.name} の成果物には {phrases} のいずれかを含む免責の一文が必須です"
        "（CLAUDE.md の規約）。「免責」の語だけでは不足です。"
    )


def check_file(path: Path, root: Path) -> str | None:
    """1ファイルを検査し、問題があればメッセージを、無ければ ``None`` を返す。"""
    scope = scope_for(path, root)
    if scope is None:
        return None
    if not path.is_file():
        return None  # 書き込み後に消えている等の異常時はブロックしない
    text = path.read_text(encoding="utf-8", errors="replace")
    if has_disclaimer(text, scope):
        return None
    return feedback_message(scope, path)


def iter_targets(root: Path) -> list[Path]:
    """``--all`` 用に、リポジトリ内の検査対象ファイルを列挙する（安定順）。"""
    targets: list[Path] = []
    for scope in _SCOPES:
        base = (root / scope.rel_dir).resolve()
        if not base.is_dir():
            continue
        for path in sorted(base.glob(scope.pattern)):
            if path.is_file() and scope_for(path.resolve(), root) is not None:
                targets.append(path.resolve())
    return targets


def check_all(root: Path) -> int:
    """対象ファイルを一括検査する。全て合格なら 0、違反があれば 2。"""
    problems = [msg for path in iter_targets(root) if (msg := check_file(path, root))]
    if problems:
        for msg in problems:
            print(msg, file=sys.stderr)
        print(f"免責文の欠落: {len(problems)} 件", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "--all":
        return check_all(repo_root())

    file_path = target_file_path(argv)
    if not file_path:
        return 0  # 対象パスが取れない場合は何もしない（ブロックしない）

    root = repo_root()
    path = Path(file_path)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()

    message = check_file(path, root)
    if message is None:
        return 0
    print(message, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

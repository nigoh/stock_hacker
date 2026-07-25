#!/usr/bin/env python3
"""ドキュメント中の件数表記とリポジトリ実体の整合を検査する（表記ドリフト検出）。

ナレッジ文書を 90 → 100 に増やしたときのように、件数はリポジトリの複数の文書
（README.md・CLAUDE.md・knowledge/00-index.md・docs/*.html・docs/*.md）に散在する。1箇所でも
直し忘れると、公開サイトの記述とリポジトリ実体が食い違う。このスクリプトは実体を
数え、文書中の件数表記と機械的に突き合わせて不一致を報告する。

    python3 scripts/check_doc_counts.py            # 全体チェック（違反があれば exit 1）
    python3 scripts/check_doc_counts.py --verbose  # 検出した件数表記を全件表示（誤検知の確認用）

数える対象（実測）:
  - ナレッジ文書数     knowledge/**/*.md（00-index.md を除く）
  - ナレッジ分野数     knowledge/*/ のディレクトリ数
  - 分野別の文書数     knowledge/<分野>/**/*.md
  - 分析 CLI 数        analysis/*.py
  - stocklib モジュール数  analysis/stocklib/*.py（__init__.py を除く）
  - スキル数           .claude/skills/*/
  - スラッシュコマンド数   .claude/commands/*.md
  - サブエージェント数     .claude/agents/*.md

検出方針（誤検知を出さないことを最優先）:
  「<数字><助数詞>」の件数表記を拾い、直前・直後の文脈語（「スキル」「コマンド」
  「CLI」等）のうち**最も近いもの**で何の件数かを判定する。文脈語が窓の中に無い、
  文の区切り（句点・改行）をまたぐ、複数の文脈語が同距離で競合する——といった
  「確実に判定できない」ケースはすべて無視する。よって「2024年11月」「33業種」
  「225先物」「30銘柄」のような件数でない数字や、リポジトリ実体と無関係な数字
  （バックテスト戦略の「5種類」、MAPE-K の「1周1件」等）は拾わない。
  reports/ は gitignore 対象のため検査対象外。
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# 実測（リポジトリの実体を数える）
# ---------------------------------------------------------------------------

# 実測値のキー → 人間向けラベル
METRIC_LABELS: dict[str, str] = {
    "knowledge_docs": "ナレッジ文書数（knowledge/**/*.md、00-index.md を除く）",
    "knowledge_categories": "ナレッジ分野数（knowledge/*/）",
    "cli_scripts": "分析 CLI 数（analysis/*.py）",
    "stocklib_modules": "stocklib モジュール数（analysis/stocklib/*.py、__init__.py を除く）",
    "skills": "スキル数（.claude/skills/*/）",
    "commands": "スラッシュコマンド数（.claude/commands/*.md）",
    "agents": "サブエージェント数（.claude/agents/*.md）",
}

# 分野別文書数のメトリクスキーの接頭辞（例: "dir:technical"）
DIR_METRIC_PREFIX = "dir:"


def repo_root() -> Path:
    """リポジトリルートを返す（CLAUDE_PROJECT_DIR があればそれ、無ければこのファイルの親の親）。"""
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def count_knowledge_docs(root: Path) -> int:
    """knowledge/ 配下の Markdown 文書数（索引 00-index.md を除く）を返す。"""
    knowledge = root / "knowledge"
    if not knowledge.is_dir():
        return 0
    return sum(1 for p in knowledge.rglob("*.md") if p.name != "00-index.md")


def count_knowledge_docs_by_category(root: Path) -> dict[str, int]:
    """分野ディレクトリ名 → その配下の Markdown 文書数の辞書を返す。"""
    knowledge = root / "knowledge"
    if not knowledge.is_dir():
        return {}
    counts: dict[str, int] = {}
    for d in sorted(p for p in knowledge.iterdir() if p.is_dir()):
        counts[d.name] = sum(1 for p in d.rglob("*.md") if p.name != "00-index.md")
    return counts


def count_knowledge_categories(root: Path) -> int:
    """knowledge/ 直下の分野ディレクトリ数を返す。"""
    return len(count_knowledge_docs_by_category(root))


def count_cli_scripts(root: Path) -> int:
    """analysis/ 直下の分析 CLI（*.py）の本数を返す。"""
    analysis = root / "analysis"
    if not analysis.is_dir():
        return 0
    return sum(1 for p in analysis.glob("*.py"))


def count_stocklib_modules(root: Path) -> int:
    """analysis/stocklib/ のモジュール数（__init__.py を除く）を返す。"""
    stocklib = root / "analysis" / "stocklib"
    if not stocklib.is_dir():
        return 0
    return sum(1 for p in stocklib.glob("*.py") if p.name != "__init__.py")


def count_skills(root: Path) -> int:
    """.claude/skills/ 配下のスキルディレクトリ数を返す。"""
    skills = root / ".claude" / "skills"
    if not skills.is_dir():
        return 0
    return sum(1 for p in skills.iterdir() if p.is_dir())


def count_commands(root: Path) -> int:
    """.claude/commands/ 配下のスラッシュコマンド定義（*.md）の数を返す。"""
    commands = root / ".claude" / "commands"
    if not commands.is_dir():
        return 0
    return sum(1 for p in commands.glob("*.md"))


def count_agents(root: Path) -> int:
    """.claude/agents/ 配下のサブエージェント定義（*.md）の数を返す。"""
    agents = root / ".claude" / "agents"
    if not agents.is_dir():
        return 0
    return sum(1 for p in agents.glob("*.md"))


def measure(root: Path) -> dict[str, int]:
    """リポジトリ実体を数え、メトリクスキー → 実測値の辞書を返す。"""
    actual: dict[str, int] = {
        "knowledge_docs": count_knowledge_docs(root),
        "knowledge_categories": count_knowledge_categories(root),
        "cli_scripts": count_cli_scripts(root),
        "stocklib_modules": count_stocklib_modules(root),
        "skills": count_skills(root),
        "commands": count_commands(root),
        "agents": count_agents(root),
    }
    for name, n in count_knowledge_docs_by_category(root).items():
        actual[f"{DIR_METRIC_PREFIX}{name}"] = n
    return actual


def metric_label(metric: str) -> str:
    """メトリクスキーの人間向けラベルを返す。"""
    if metric.startswith(DIR_METRIC_PREFIX):
        return f"knowledge/{metric[len(DIR_METRIC_PREFIX):]}/ の文書数"
    return METRIC_LABELS.get(metric, metric)


# ---------------------------------------------------------------------------
# 検出（ドキュメント中の件数表記を拾う）
# ---------------------------------------------------------------------------

# 文脈語の探索を打ち切る区切り（これをまたいだ文脈語は「無関係」とみなす）
_SENTENCE_BREAK_RE = re.compile(r"[。！？\n]")
# HTML タグ（1行内で閉じるもの。docs/*.html はこの形で書かれている）
_HTML_TAG_RE = re.compile(r"<[^>]*>")
# HTML の実体参照（件数表記の判定に影響する最小限のみ戻す）
_ENTITIES: tuple[tuple[str, str], ...] = (
    ("&amp;", "&"),
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&quot;", '"'),
    ("&#39;", "'"),
    ("&nbsp;", " "),
)


@dataclass(frozen=True)
class Rule:
    """件数表記1パターンの検出ルール。

    pattern の第1グループが数字。metric が指定されていれば無条件にその実測値と
    突き合わせる。keyword_map があれば、数字の前後 window 文字以内で最も近い
    文脈語からメトリクスを決める（見つからない／同距離で競合する場合は無視）。
    """

    name: str
    pattern: re.Pattern[str]
    metric: str | None = None
    keyword_map: tuple[tuple[str, str], ...] = ()
    window: int = 14
    side: str = "both"  # "both" | "before" | "after"


# 「N文書」— 助数詞「文書」はこのリポジトリではナレッジ文書数以外に使われない
_RULE_DOCS = Rule(
    name="N文書",
    pattern=re.compile(r"(\d+)\s*(?:ナレッジ)?文書"),
    metric="knowledge_docs",
)
# 「文書索引（N件）」— 助数詞「件」は多義なので、直前に「文書索引」「文書数」がある場合のみ
_RULE_DOCS_KEN = Rule(
    name="N件",
    pattern=re.compile(r"(\d+)\s*件"),
    keyword_map=(("文書索引", "knowledge_docs"), ("文書数", "knowledge_docs")),
    window=8,
    side="before",
)
# 「Markdown 文書数（合計N）」
_RULE_DOCS_TOTAL = Rule(
    name="文書数（合計N）",
    pattern=re.compile(r"文書数\s*[（(]\s*合計\s*(\d+)"),
    metric="knowledge_docs",
)
# 「N分野」「Nの分野」
_RULE_CATEGORIES = Rule(
    name="N分野",
    pattern=re.compile(r"(\d+)\s*の?\s*分野"),
    metric="knowledge_categories",
)
# 「Nカテゴリ」— CLI のカテゴリ等と紛れるため、ナレッジ文脈が近いときのみ
_RULE_CATEGORIES_KATEGORI = Rule(
    name="Nカテゴリ",
    pattern=re.compile(r"(\d+)\s*の?\s*カテゴリ"),
    keyword_map=(
        ("ナレッジ", "knowledge_categories"),
        ("knowledge", "knowledge_categories"),
        ("文書", "knowledge_categories"),
        ("索引", "knowledge_categories"),
        ("知識", "knowledge_categories"),
    ),
    window=40,
)
# 「N本」— 「CLI」「コマンド」「スクリプト」が近いときだけ分析 CLI の本数とみなす
_RULE_CLI_HON = Rule(
    name="N本",
    pattern=re.compile(r"(\d+)\s*本"),
    keyword_map=(
        ("CLI", "cli_scripts"),
        ("コマンド", "cli_scripts"),
        ("スクリプト", "cli_scripts"),
    ),
    window=12,
)
# 「Nの分析 CLI」（助数詞なし）。「20の CLI が対応」のような部分集合の記述と
# 紛れないよう、全体を指す「分析 CLI」という言い方のときだけ拾う。
_RULE_CLI_NO = Rule(
    name="Nの分析 CLI",
    pattern=re.compile(r"(\d+)\s*の\s*分析\s*CLI"),
    metric="cli_scripts",
)
# 「Nモジュール」
_RULE_MODULES = Rule(
    name="Nモジュール",
    pattern=re.compile(r"(\d+)\s*の?\s*(?:stocklib\s*)?モジュール"),
    metric="stocklib_modules",
)
# 「N種」— 直近の文脈語でスキル／コマンド／エージェントを判定（「N種類」は除外）
_RULE_SHU = Rule(
    name="N種",
    pattern=re.compile(r"(\d+)\s*種(?!類)"),
    keyword_map=(
        ("スキル", "skills"),
        ("コマンド", "commands"),
        ("エージェント", "agents"),
    ),
    window=14,
)
# 「スキル18・コマンド20・サブエージェント4」のような助数詞なしの併記。
# 数字の直後が区切り記号・行末のときだけ拾う（「スキル 2 つ目」等は拾わない）。
_BARE_TAIL = r"(?=\s*(?:[・。、，,）)]|[—–-]|$))"
_RULE_BARE_SKILLS = Rule(
    name="スキルN",
    pattern=re.compile(r"スキル\s*(\d+)" + _BARE_TAIL),
    metric="skills",
)
_RULE_BARE_COMMANDS = Rule(
    name="コマンドN",
    pattern=re.compile(r"(?:スラッシュ)?コマンド\s*(\d+)" + _BARE_TAIL),
    metric="commands",
)
_RULE_BARE_AGENTS = Rule(
    name="エージェントN",
    pattern=re.compile(r"(?:サブ)?エージェント\s*(\d+)" + _BARE_TAIL),
    metric="agents",
)

TEXT_RULES: tuple[Rule, ...] = (
    _RULE_DOCS,
    _RULE_DOCS_KEN,
    _RULE_DOCS_TOTAL,
    _RULE_CATEGORIES,
    _RULE_CATEGORIES_KATEGORI,
    _RULE_CLI_HON,
    _RULE_CLI_NO,
    _RULE_MODULES,
    _RULE_SHU,
    _RULE_BARE_SKILLS,
    _RULE_BARE_COMMANDS,
    _RULE_BARE_AGENTS,
)

# docs/*.html のスタッツブロック: <span class="n">N</span><span class="k">ラベル</span>
_STAT_RE = re.compile(r'<span class="n">(\d+)</span>\s*<span class="k">([^<]*)</span>')
# ラベル → メトリクス（表に無いラベル（「カテゴリ」「hooks」「索引 (00-index.md)」等）は無視）
STAT_LABEL_METRICS: dict[str, str] = {
    "ナレッジ文書": "knowledge_docs",
    "文書": "knowledge_docs",
    "分野": "knowledge_categories",
    "分析CLI": "cli_scripts",
    "stocklibモジュール": "stocklib_modules",
    "スキル": "skills",
    "スラッシュコマンド": "commands",
    "コマンド": "commands",
    "サブエージェント": "agents",
    "エージェント": "agents",
}

# docs/*.html の <meta name="description" content="..."> の本文（タグ除去では消えるため個別に拾う）
_META_DESCRIPTION_RE = re.compile(
    r'<meta\s+name="description"\s+content="([^"]*)"', re.IGNORECASE
)

# docs/*.html の絞り込みバーの件数: <span data-filter-count="kb">100</span>
_FILTER_COUNT_RE = re.compile(r'<span data-filter-count="([a-z0-9_-]+)">(\d+)</span>')
FILTER_COUNT_METRICS: dict[str, str] = {
    "kb": "knowledge_docs",
    "cli": "cli_scripts",
    "cmd": "commands",
}


@dataclass(frozen=True)
class Finding:
    """ドキュメント中で見つかった件数表記1つ。"""

    path: str
    line_no: int
    metric: str
    value: int
    snippet: str
    rule: str = ""

    @property
    def dedup_key(self) -> tuple[str, int, str, int]:
        """同じ行の同じ表記を複数ルールが拾ったときに重複排除するためのキー。"""
        return (self.path, self.line_no, self.metric, self.value)


def strip_html(line: str) -> str:
    """HTML タグを除去し、最小限の実体参照を戻した行を返す。"""
    text = _HTML_TAG_RE.sub("", line)
    for entity, char in _ENTITIES:
        text = text.replace(entity, char)
    return text


def nearest_metric(
    text: str, start: int, end: int, rule: Rule
) -> str | None:
    """数字の前後から最も近い文脈語を探し、対応するメトリクスキーを返す。

    文脈語が窓の中に無い、文の区切り（句点・改行）をまたぐ、複数のメトリクスが
    同じ距離で競合する場合は None（＝判定不能なので無視する）。
    """
    best_distance: int | None = None
    best_metric: str | None = None
    ambiguous = False

    left = text[max(0, start - rule.window) : start]
    right = text[end : end + rule.window]

    for keyword, metric in rule.keyword_map:
        candidates: list[int] = []
        if rule.side in ("both", "before"):
            idx = left.rfind(keyword)
            if idx != -1:
                gap = left[idx + len(keyword) :]
                if not _SENTENCE_BREAK_RE.search(gap):
                    candidates.append(len(gap))
        if rule.side in ("both", "after"):
            idx = right.find(keyword)
            if idx != -1:
                gap = right[:idx]
                if not _SENTENCE_BREAK_RE.search(gap):
                    candidates.append(len(gap))
        for distance in candidates:
            if best_distance is None or distance < best_distance:
                best_distance, best_metric, ambiguous = distance, metric, False
            elif distance == best_distance and metric != best_metric:
                ambiguous = True

    if ambiguous:
        return None
    return best_metric


def scan_line(path: str, line_no: int, raw_line: str, is_html: bool) -> list[Finding]:
    """1行から件数表記を検出する（判定できたものだけを返す）。"""
    findings: list[Finding] = []

    if is_html:
        for m in _STAT_RE.finditer(raw_line):
            label = re.sub(r"\s+", "", m.group(2))
            metric = STAT_LABEL_METRICS.get(label)
            if metric is not None:
                findings.append(
                    Finding(path, line_no, metric, int(m.group(1)), m.group(0), "stats")
                )
        for m in _FILTER_COUNT_RE.finditer(raw_line):
            metric = FILTER_COUNT_METRICS.get(m.group(1))
            if metric is not None:
                findings.append(
                    Finding(path, line_no, metric, int(m.group(2)), m.group(0), "filter-count")
                )

    texts = [strip_html(raw_line) if is_html else raw_line]
    if is_html:
        texts.extend(m.group(1) for m in _META_DESCRIPTION_RE.finditer(raw_line))

    for text in texts:
        for rule in TEXT_RULES:
            for m in rule.pattern.finditer(text):
                metric = rule.metric
                if metric is None:
                    metric = nearest_metric(text, m.start(1), m.end(1), rule)
                if metric is None:
                    continue  # 判定できないものは無視する（誤検知を出さない）
                findings.append(
                    Finding(
                        path, line_no, metric, int(m.group(1)), m.group(0).strip(), rule.name
                    )
                )
    return findings


def scan_extra_rules(
    path: str, line_no: int, text: str, dir_names: list[str]
) -> list[Finding]:
    """分野ディレクトリの件数表記（docs/knowledge.html の「technical · 5」）を検出する。"""
    findings: list[Finding] = []
    for name in dir_names:
        for m in re.finditer(rf"{re.escape(name)}\s*·\s*(\d+)", text):
            findings.append(
                Finding(
                    path,
                    line_no,
                    f"{DIR_METRIC_PREFIX}{name}",
                    int(m.group(1)),
                    m.group(0).strip(),
                    "分野·N",
                )
            )
    return findings


def target_files(root: Path) -> list[Path]:
    """検査対象ファイル（README.md・CLAUDE.md・knowledge/00-index.md・docs/*.html・docs/*.md）を返す。"""
    paths: list[Path] = []
    for rel in ("README.md", "CLAUDE.md", "knowledge/00-index.md"):
        p = root / rel
        if p.is_file():
            paths.append(p)
    docs = root / "docs"
    if docs.is_dir():
        paths.extend(sorted(docs.glob("*.html")))
        paths.extend(sorted(docs.glob("*.md")))
    return paths


def collect_findings(root: Path) -> list[Finding]:
    """検査対象ファイルを走査し、判定できた件数表記を全て返す（重複排除済み）。"""
    dir_names = sorted(count_knowledge_docs_by_category(root))
    findings: list[Finding] = []
    for path in target_files(root):
        rel = path.relative_to(root).as_posix()
        is_html = path.suffix.lower() == ".html"
        for line_no, raw_line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            findings.extend(scan_line(rel, line_no, raw_line, is_html))
            text = strip_html(raw_line) if is_html else raw_line
            findings.extend(scan_extra_rules(rel, line_no, text, dir_names))

    unique: dict[tuple[str, int, str, int], Finding] = {}
    for f in findings:
        unique.setdefault(f.dedup_key, f)
    return sorted(unique.values(), key=lambda f: (f.path, f.line_no, f.metric))


def check_all(root: Path) -> tuple[list[str], list[Finding]]:
    """件数表記の整合を検査し、（違反メッセージ, 検出した全表記）を返す。"""
    actual = measure(root)
    findings = collect_findings(root)
    violations: list[str] = []
    for f in findings:
        expected = actual.get(f.metric)
        if expected is None:
            continue  # 実測できないメトリクスは判定しない
        if f.value != expected:
            violations.append(
                f"{f.path}:{f.line_no} 「{f.snippet}」は{metric_label(f.metric)}の表記ですが、"
                f"実測は {expected} です（表記は {f.value}）。"
            )
    return violations, findings


def main(argv: list[str]) -> int:
    verbose = "--verbose" in argv[1:] or "-v" in argv[1:]
    root = repo_root()
    violations, findings = check_all(root)

    if verbose:
        actual = measure(root)
        print("実測値:")
        for key, value in actual.items():
            print(f"  {key:28s} = {value}  （{metric_label(key)}）")
        print(f"検出した件数表記: {len(findings)} 箇所")
        for f in findings:
            expected = actual.get(f.metric)
            mark = "OK " if expected == f.value else "NG "
            print(f"  {mark}{f.path}:{f.line_no} [{f.rule}] {f.metric}={f.value} 「{f.snippet}」")

    if violations:
        print(
            f"ドキュメント件数表記の整合チェック: {len(violations)} 件の不一致",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        print(
            "  リポジトリ実体に合わせて表記を更新してください"
            "（実測値の一覧は --verbose で確認できます）。",
            file=sys.stderr,
        )
        return 1

    print(
        f"ドキュメント件数表記の整合チェック: OK"
        f"（{len(target_files(root))} ファイル・{len(findings)} 箇所の件数表記、不一致なし）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

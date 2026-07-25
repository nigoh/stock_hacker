"""scripts/check_report_disclaimer.py のテスト。

tmp_path に模擬ツリー（reports/・journal/・docs/）を作り、免責文の有無による
合格（exit 0）・不合格（exit 2）と、対象外パス・stdin JSON モード・``--all`` の
一括検査を検証する。repo_root() は CLAUDE_PROJECT_DIR 環境変数を優先するため、
monkeypatch で差し替える。ネットワークアクセスは不要。

**現行リポジトリの実ファイルが全て通ること**（:class:`TestRealRepository`）を
併せて検証する——検査を厳格にした結果、既存の公開物が一斉に落ちる（＝運用が
止まる）ことを防ぐため。落ちた場合は、その成果物に本当に免責が要るかを
判断してから直すこと。
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "check_report_disclaimer.py"
)

_spec = importlib.util.spec_from_file_location("check_report_disclaimer", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
crd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crd)

REPO_ROOT = Path(__file__).resolve().parents[2]

DISCLAIMER_LINE = (
    "> **免責事項**: 本レポートは分析支援を目的としており、投資助言ではありません。"
)

# docs/*.html のフッターで使われている否定表現
SITE_DISCLAIMER_LINE = (
    "<p>免責：本環境および本サイトの出力は投資助言ではなく分析支援です。</p>"
)


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("reports", "journal", "docs"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    return tmp_path


def write_report(repo: Path, name: str, body: str) -> Path:
    path = repo / "reports" / name
    path.write_text(body, encoding="utf-8")
    return path


def write_file(repo: Path, rel: str, body: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 引数モード（手動テスト用）
# ---------------------------------------------------------------------------


class TestArgvMode:
    def test_report_with_disclaimer_passes(self, repo: Path) -> None:
        path = write_report(
            repo, "analyze-7203-2026-07-16.md", f"# レポート\n\n本文。\n\n{DISCLAIMER_LINE}\n"
        )
        assert crd.main(["prog", str(path)]) == 0

    def test_menseki_keyword_alone_is_no_longer_enough(self, repo: Path) -> None:
        # かつては「免責」の2文字で合格していたが、それでは
        # 「免責事項は後述」のように免責を書いていない文書まで通ってしまう。
        path = write_report(repo, "screen-2026-07-16.md", "# 結果\n\n## 免責\n\n注意書き。\n")
        assert crd.main(["prog", str(path)]) == 2

    def test_forward_reference_to_menseki_is_rejected(self, repo: Path) -> None:
        path = write_report(repo, "screen-2026-07-17.md", "# 結果\n\n免責事項は後述。\n")
        assert crd.main(["prog", str(path)]) == 2

    def test_report_without_disclaimer_fails_with_exit_2(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write_report(repo, "market-review-2026-07-16.md", "# 市況\n\n本文のみ。注意書きなし。\n")
        assert crd.main(["prog", str(path)]) == 2
        err = capsys.readouterr().err
        assert "免責文がありません" in err
        assert "レポート末尾に" in err
        assert "stocklib.report.DISCLAIMER" in err
        assert "投資助言ではありません" in err  # 要求する文言を具体的に示す
        assert str(path) in err  # どのファイルかが分かる

    def test_relative_path_is_resolved_against_repo_root(self, repo: Path) -> None:
        write_report(repo, "compare.md", f"# 比較\n\n{DISCLAIMER_LINE}\n")
        assert crd.main(["prog", "reports/compare.md"]) == 0

    def test_file_outside_reports_is_skipped(self, repo: Path) -> None:
        outside = repo / "README.md"
        outside.write_text("# readme（免責文なしでも対象外）\n", encoding="utf-8")
        assert crd.main(["prog", str(outside)]) == 0

    def test_knowledge_doc_is_skipped(self, repo: Path) -> None:
        knowledge = repo / "knowledge"
        knowledge.mkdir()
        doc = knowledge / "doc.md"
        doc.write_text("# 知識文書\n", encoding="utf-8")
        assert crd.main(["prog", str(doc)]) == 0

    def test_non_markdown_in_reports_is_skipped(self, repo: Path) -> None:
        csv = repo / "reports" / "data.csv"
        csv.write_text("a,b\n", encoding="utf-8")
        assert crd.main(["prog", str(csv)]) == 0

    def test_missing_file_is_skipped(self, repo: Path) -> None:
        assert crd.main(["prog", str(repo / "reports" / "gone.md")]) == 0


# ---------------------------------------------------------------------------
# stdin JSON モード（PostToolUse フック用）
# ---------------------------------------------------------------------------


def run_with_stdin_json(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> int:
    stdin = io.StringIO(json.dumps(payload) if not isinstance(payload, str) else payload)
    stdin.isatty = lambda: False  # type: ignore[method-assign]
    monkeypatch.setattr(crd.sys, "stdin", stdin)
    return crd.main(["prog"])


class TestStdinJsonMode:
    def test_hook_json_without_disclaimer_fails(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write_report(repo, "analyze-6758.md", "# レポート\n\n本文のみ。\n")
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(path)}}
        assert run_with_stdin_json(monkeypatch, payload) == 2
        assert "免責文" in capsys.readouterr().err

    def test_hook_json_with_disclaimer_passes(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = write_report(repo, "analyze-9984.md", f"# レポート\n\n{DISCLAIMER_LINE}\n")
        payload = {"tool_name": "Edit", "tool_input": {"file_path": str(path)}}
        assert run_with_stdin_json(monkeypatch, payload) == 0

    def test_invalid_json_is_ignored(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert run_with_stdin_json(monkeypatch, "not-json {") == 0

    def test_missing_tool_input_is_ignored(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert run_with_stdin_json(monkeypatch, {"tool_name": "Write"}) == 0

    def test_empty_stdin_is_ignored(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert run_with_stdin_json(monkeypatch, "") == 0


# ---------------------------------------------------------------------------
# journal/（git 管理対象で GitHub 上に公開される）
# ---------------------------------------------------------------------------


class TestJournalScope:
    def test_entry_without_disclaimer_fails(self, repo: Path) -> None:
        path = write_file(
            repo, "journal/2026/2026-07-25-x.md", "---\nid: x\n---\n\n## 仮説\n\n7203 は上がる。\n"
        )
        assert crd.main(["prog", str(path)]) == 2

    def test_entry_with_journal_disclaimer_passes(self, repo: Path) -> None:
        from stocklib.journal import DISCLAIMER as JOURNAL_DISCLAIMER

        path = write_file(
            repo, "journal/2026/2026-07-25-y.md", f"---\nid: y\n---\n\n{JOURNAL_DISCLAIMER}\n\n## 仮説\n"
        )
        assert crd.main(["prog", str(path)]) == 0

    def test_readme_is_excluded(self, repo: Path) -> None:
        # README は書式の説明であって分析記録ではない
        path = write_file(repo, "journal/README.md", "# ジャーナルの書式\n\n説明のみ。\n")
        assert crd.main(["prog", str(path)]) == 0

    def test_non_markdown_in_journal_is_skipped(self, repo: Path) -> None:
        path = write_file(repo, "journal/2026/notes.txt", "メモ\n")
        assert crd.main(["prog", str(path)]) == 0

    def test_feedback_mentions_journal_disclaimer(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write_file(repo, "journal/2026/2026-07-25-z.md", "---\nid: z\n---\n\n本文。\n")
        assert crd.main(["prog", str(path)]) == 2
        assert "stocklib.journal.DISCLAIMER" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# docs/*.html（GitHub Pages で公開される）
# ---------------------------------------------------------------------------


class TestDocsScope:
    def test_html_without_disclaimer_fails(self, repo: Path) -> None:
        path = write_file(repo, "docs/page.html", "<h1>ページ</h1><p>本文のみ。</p>\n")
        assert crd.main(["prog", str(path)]) == 2

    def test_html_with_site_footer_passes(self, repo: Path) -> None:
        path = write_file(
            repo, "docs/page.html", f"<h1>ページ</h1><footer>{SITE_DISCLAIMER_LINE}</footer>\n"
        )
        assert crd.main(["prog", str(path)]) == 0

    def test_html_with_report_style_disclaimer_passes(self, repo: Path) -> None:
        path = write_file(repo, "docs/other.html", "<p>本レポートは投資助言ではありません。</p>\n")
        assert crd.main(["prog", str(path)]) == 0

    def test_docs_markdown_is_out_of_scope(self, repo: Path) -> None:
        # 運用ガイド（getting-started.md 等）は分析結果ではないため意図的に対象外。
        # 過剰検査で運用を壊さないための判断。
        path = write_file(repo, "docs/getting-started.md", "# はじめかた\n\n手順の説明のみ。\n")
        assert crd.main(["prog", str(path)]) == 0

    def test_assets_subdirectory_is_out_of_scope(self, repo: Path) -> None:
        path = write_file(repo, "docs/assets/fragment.html", "<span>部品</span>\n")
        assert crd.main(["prog", str(path)]) == 0


# ---------------------------------------------------------------------------
# --all（一括検査）
# ---------------------------------------------------------------------------


class TestAllMode:
    def test_all_passes_when_every_target_has_disclaimer(self, repo: Path) -> None:
        write_report(repo, "analyze-7203.md", f"# レポート\n\n{DISCLAIMER_LINE}\n")
        write_file(repo, "journal/2026/e.md", "---\nid: e\n---\n\n投資助言ではありません。\n")
        write_file(repo, "docs/index.html", f"<footer>{SITE_DISCLAIMER_LINE}</footer>\n")
        write_file(repo, "journal/README.md", "# 書式\n")
        write_file(repo, "docs/getting-started.md", "# 手順\n")
        assert crd.main(["prog", "--all"]) == 0

    def test_all_reports_every_violation(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write_report(repo, "ok.md", f"# ok\n\n{DISCLAIMER_LINE}\n")
        write_report(repo, "bad-report.md", "# 本文のみ\n")
        write_file(repo, "journal/2026/bad-entry.md", "---\nid: b\n---\n\n本文。\n")
        write_file(repo, "docs/bad-page.html", "<p>本文</p>\n")
        assert crd.main(["prog", "--all"]) == 2
        err = capsys.readouterr().err
        assert "bad-report.md" in err
        assert "bad-entry.md" in err
        assert "bad-page.html" in err
        assert "ok.md" not in err
        assert "3 件" in err

    def test_all_on_empty_repo_passes(self, repo: Path) -> None:
        assert crd.main(["prog", "--all"]) == 0


# ---------------------------------------------------------------------------
# 現行リポジトリの実ファイル
# ---------------------------------------------------------------------------


class TestRealRepository:
    """**厳格化した検査を現行リポジトリの実ファイルが全て通ること。**

    ここが落ちたら、検査を緩める前に「その成果物に本当に免責が要るか」を
    判断すること（対象範囲の誤りなら scope を直す、免責漏れならファイルを直す）。
    """

    def test_all_current_files_pass(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(REPO_ROOT))
        exit_code = crd.main(["prog", "--all"])
        assert exit_code == 0, capsys.readouterr().err

    def test_scope_actually_covers_real_files(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """検査が空振りしていない（対象0件で緑になっていない）ことを確認する。"""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(REPO_ROOT))
        targets = crd.iter_targets(REPO_ROOT)
        by_scope = {}
        for path in targets:
            scope = crd.scope_for(path, REPO_ROOT)
            assert scope is not None
            by_scope.setdefault(scope.name, []).append(path)

        # journal/ の実エントリと docs/ の実ページが対象に入っていること
        assert by_scope.get("journal/"), "journal/ のエントリが検査対象に入っていない"
        assert len(by_scope.get("docs/", [])) >= 5, "docs/*.html が検査対象に入っていない"
        assert all(p.name != "README.md" for p in by_scope["journal/"])

    def test_generated_disclaimers_satisfy_the_rule(self) -> None:
        """CLI が自動付与する定型文が、厳格化した規則を満たすこと。

        ここが崩れると、正しく生成したレポート/エントリを hook が弾いて
        運用が止まる（定型文と検査の同期を守るための固定）。
        """
        from stocklib.journal import DISCLAIMER as JOURNAL_DISCLAIMER
        from stocklib.report import DISCLAIMER as REPORT_DISCLAIMER

        reports_scope = next(s for s in crd._SCOPES if s.name == "reports/")
        journal_scope = next(s for s in crd._SCOPES if s.name == "journal/")
        assert crd.has_disclaimer(REPORT_DISCLAIMER, reports_scope)
        assert crd.has_disclaimer(JOURNAL_DISCLAIMER, journal_scope)

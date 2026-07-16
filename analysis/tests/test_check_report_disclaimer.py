"""scripts/check_report_disclaimer.py のテスト。

tmp_path に模擬 reports/ ツリーを作り、免責文の有無による合格（exit 0）・
不合格（exit 2）と、対象外パス・stdin JSON モードの挙動を検証する。
repo_root() は CLAUDE_PROJECT_DIR 環境変数を優先するため、monkeypatch で差し替える。
ネットワークアクセスは不要。
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

DISCLAIMER_LINE = (
    "> **免責事項**: 本レポートは分析支援を目的としており、投資助言ではありません。"
)


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "reports").mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    return tmp_path


def write_report(repo: Path, name: str, body: str) -> Path:
    path = repo / "reports" / name
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

    def test_report_with_menseki_keyword_passes(self, repo: Path) -> None:
        # 「免責」の語だけでも合格（表現ゆれの許容）
        path = write_report(repo, "screen-2026-07-16.md", "# 結果\n\n## 免責\n\n注意書き。\n")
        assert crd.main(["prog", str(path)]) == 0

    def test_report_without_disclaimer_fails_with_exit_2(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write_report(repo, "market-review-2026-07-16.md", "# 市況\n\n本文のみ。注意書きなし。\n")
        assert crd.main(["prog", str(path)]) == 2
        err = capsys.readouterr().err
        assert "レポート末尾に免責文を追加してください" in err
        assert "stocklib.report.DISCLAIMER" in err

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

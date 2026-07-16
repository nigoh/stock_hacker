"""scripts/check_knowledge_index.py のテスト。

tmp_path に模擬 knowledge ツリーを作り、単一ファイルモード（hook 用、違反時 exit 2）と
--all モード（CI 用、違反時 exit 1）の合格・不合格の両パスを検証する。
repo_root() は CLAUDE_PROJECT_DIR 環境変数を優先するため、monkeypatch で差し替える。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_knowledge_index.py"

_spec = importlib.util.spec_from_file_location("check_knowledge_index", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
cki = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cki)


def make_knowledge_tree(root: Path, *, doc_count_label: int = 2) -> None:
    """整合の取れた模擬 knowledge ツリーを作る（文書2本 + 索引）。"""
    knowledge = root / "knowledge"
    (knowledge / "math").mkdir(parents=True)
    (knowledge / "technical").mkdir(parents=True)

    (knowledge / "math" / "doc-a.md").write_text(
        "# 文書A\n\n本文。\n\n## 関連トピック\n\n- [文書B](../technical/doc-b.md)\n",
        encoding="utf-8",
    )
    (knowledge / "technical" / "doc-b.md").write_text(
        "# 文書B\n\n本文。関連トピック節なし。\n",
        encoding="utf-8",
    )
    (knowledge / "00-index.md").write_text(
        f"# 索引\n\n全{doc_count_label}文書。\n\n"
        "## 数学（`math/`）\n\n- [文書A](math/doc-a.md) — 要約。\n\n"
        "## テクニカル（`technical/`）\n\n- [文書B](technical/doc-b.md) — 要約。\n",
        encoding="utf-8",
    )


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    make_knowledge_tree(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# 単一ファイルモード（hook 用）
# ---------------------------------------------------------------------------


class TestSingleFileMode:
    def test_indexed_doc_passes(self, repo: Path) -> None:
        assert cki.main(["prog", str(repo / "knowledge" / "math" / "doc-a.md")]) == 0

    def test_unindexed_doc_fails_with_exit_2(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        new_doc = repo / "knowledge" / "math" / "doc-c.md"
        new_doc.write_text("# 文書C\n", encoding="utf-8")
        assert cki.main(["prog", str(new_doc)]) == 2
        assert "math/doc-c.md" in capsys.readouterr().err

    def test_relative_path_is_resolved_against_repo_root(self, repo: Path) -> None:
        assert cki.main(["prog", "knowledge/math/doc-a.md"]) == 0

    def test_index_itself_is_skipped(self, repo: Path) -> None:
        assert cki.main(["prog", str(repo / "knowledge" / "00-index.md")]) == 0

    def test_file_outside_knowledge_is_skipped(self, repo: Path) -> None:
        outside = repo / "README.md"
        outside.write_text("# readme\n", encoding="utf-8")
        assert cki.main(["prog", str(outside)]) == 0

    def test_non_markdown_in_knowledge_is_skipped(self, repo: Path) -> None:
        csv = repo / "knowledge" / "math" / "data.csv"
        csv.write_text("a,b\n", encoding="utf-8")
        assert cki.main(["prog", str(csv)]) == 0

    def test_missing_index_fails_with_exit_2(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (repo / "knowledge" / "00-index.md").unlink()
        doc = repo / "knowledge" / "math" / "doc-a.md"
        assert cki.main(["prog", str(doc)]) == 2
        assert "00-index.md" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --all モード（CI 用）
# ---------------------------------------------------------------------------


class TestAllMode:
    def test_consistent_tree_passes(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cki.main(["prog", "--all"]) == 0
        assert "OK" in capsys.readouterr().out

    def test_unreferenced_doc_fails(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # 索引に載せずに文書を追加（カウントは合わせて未索引だけを検出させる）
        (repo / "knowledge" / "math" / "orphan.md").write_text("# 孤児\n", encoding="utf-8")
        index = repo / "knowledge" / "00-index.md"
        index.write_text(
            index.read_text(encoding="utf-8").replace("全2文書", "全3文書"),
            encoding="utf-8",
        )
        assert cki.main(["prog", "--all"]) == 1
        err = capsys.readouterr().err
        assert "未索引" in err
        assert "math/orphan.md" in err

    def test_broken_index_link_fails(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # 文書を削除 → 索引のリンク切れ + カウント不一致の両方を検出する
        (repo / "knowledge" / "technical" / "doc-b.md").unlink()
        assert cki.main(["prog", "--all"]) == 1
        err = capsys.readouterr().err
        assert "リンク切れ" in err
        assert "technical/doc-b.md" in err
        assert "文書数の不一致" in err

    def test_wrong_doc_count_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        make_knowledge_tree(tmp_path, doc_count_label=99)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        assert cki.main(["prog", "--all"]) == 1
        err = capsys.readouterr().err
        assert "文書数の不一致" in err
        assert "全99文書" in err

    def test_missing_doc_count_label_fails(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        index = repo / "knowledge" / "00-index.md"
        index.write_text(
            index.read_text(encoding="utf-8").replace("全2文書。", "たくさんの文書。"),
            encoding="utf-8",
        )
        assert cki.main(["prog", "--all"]) == 1
        assert "全N文書" in capsys.readouterr().err

    def test_broken_related_topics_link_fails(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        doc_a = repo / "knowledge" / "math" / "doc-a.md"
        doc_a.write_text(
            "# 文書A\n\n本文。\n\n## 関連トピック\n\n- [消えた文書](../technical/gone.md)\n",
            encoding="utf-8",
        )
        assert cki.main(["prog", "--all"]) == 1
        err = capsys.readouterr().err
        assert "関連トピックのリンク切れ" in err
        assert "gone.md" in err

    def test_related_topics_without_links_is_ok(self, repo: Path) -> None:
        # 実リポジトリの多数派: 関連トピック節がプレーンテキストのみ → 違反にしない
        doc_a = repo / "knowledge" / "math" / "doc-a.md"
        doc_a.write_text(
            "# 文書A\n\n本文。\n\n## 関連トピック\n\n- 文書B（technical/doc-b）\n",
            encoding="utf-8",
        )
        assert cki.main(["prog", "--all"]) == 0

    def test_external_and_anchor_links_are_ignored(self, repo: Path) -> None:
        index = repo / "knowledge" / "00-index.md"
        index.write_text(
            index.read_text(encoding="utf-8")
            + "\n- [外部](https://example.com/x.md) と [アンカー](#section) は無視。\n",
            encoding="utf-8",
        )
        assert cki.main(["prog", "--all"]) == 0

    def test_missing_knowledge_dir_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        assert cki.main(["prog", "--all"]) == 1
        assert "knowledge/" in capsys.readouterr().err

    def test_missing_index_fails(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (repo / "knowledge" / "00-index.md").unlink()
        assert cki.main(["prog", "--all"]) == 1
        assert "00-index.md" in capsys.readouterr().err

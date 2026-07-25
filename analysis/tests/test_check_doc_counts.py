"""scripts/check_doc_counts.py のテスト。

tmp_path に模擬リポジトリ（knowledge / analysis / .claude / docs）を作り、
実測関数の正しさ、件数表記の検出、不一致の検出、そして**誤検知しないこと**を検証する。
実リポジトリに対して exit 0 になること（＝現状の表記が実体と一致していること）も
回帰テストとして押さえる。repo_root() は CLAUDE_PROJECT_DIR 環境変数を優先するため、
模擬リポジトリのテストでは monkeypatch で差し替える。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_doc_counts.py"

_spec = importlib.util.spec_from_file_location("check_doc_counts", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
cdc = importlib.util.module_from_spec(_spec)
# dataclass の型解決が sys.modules を引くため、exec_module の前に登録しておく
sys.modules["check_doc_counts"] = cdc
_spec.loader.exec_module(cdc)


def make_repo(
    root: Path,
    *,
    readme: str = "",
    claude_md: str = "",
    index_lead: str = "全3文書。市場制度・数学の2カテゴリで知識を体系化する。",
    html: str = "",
) -> None:
    """整合の取れた模擬リポジトリを作る。

    実体は ナレッジ3文書 / 2分野（math=2, technical=1）・分析CLI 2本・
    stocklib 1モジュール・スキル2種・コマンド3種・エージェント1種。
    """
    (root / "knowledge" / "math").mkdir(parents=True)
    (root / "knowledge" / "technical").mkdir(parents=True)
    (root / "knowledge" / "math" / "a.md").write_text("# A\n", encoding="utf-8")
    (root / "knowledge" / "math" / "b.md").write_text("# B\n", encoding="utf-8")
    (root / "knowledge" / "technical" / "c.md").write_text("# C\n", encoding="utf-8")
    (root / "knowledge" / "00-index.md").write_text(
        f"# 索引\n\n{index_lead}\n", encoding="utf-8"
    )

    (root / "analysis" / "stocklib").mkdir(parents=True)
    (root / "analysis" / "analyze_stock.py").write_text("", encoding="utf-8")
    (root / "analysis" / "screen.py").write_text("", encoding="utf-8")
    (root / "analysis" / "stocklib" / "__init__.py").write_text("", encoding="utf-8")
    (root / "analysis" / "stocklib" / "data.py").write_text("", encoding="utf-8")

    for name in ("analyze-stock", "screen-market"):
        (root / ".claude" / "skills" / name).mkdir(parents=True)
        (root / ".claude" / "skills" / name / "SKILL.md").write_text("", encoding="utf-8")
    (root / ".claude" / "commands").mkdir(parents=True)
    for name in ("analyze", "screen", "brief"):
        (root / ".claude" / "commands" / f"{name}.md").write_text("", encoding="utf-8")
    (root / ".claude" / "agents").mkdir(parents=True)
    (root / ".claude" / "agents" / "stock-analyst.md").write_text("", encoding="utf-8")

    (root / "README.md").write_text(readme, encoding="utf-8")
    (root / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "index.html").write_text(html, encoding="utf-8")


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    make_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    return tmp_path


def values_by_metric(findings: list[object]) -> dict[str, int]:
    """検出結果を メトリクスキー → 表記された数字 の辞書にする（テストの読みやすさ用）。"""
    return {f.metric: f.value for f in findings}  # type: ignore[attr-defined]


def scan(line: str, *, html: bool = False) -> list[object]:
    """1行を走査して検出結果を返す（分野ディレクトリ規則も含める）。"""
    findings = list(cdc.scan_line("x", 1, line, html))
    text = cdc.strip_html(line) if html else line
    findings += cdc.scan_extra_rules("x", 1, text, ["math", "technical"])
    return findings


# ---------------------------------------------------------------------------
# 実測
# ---------------------------------------------------------------------------


class TestMeasure:
    def test_knowledge_docs_excludes_index(self, repo: Path) -> None:
        assert cdc.count_knowledge_docs(repo) == 3

    def test_knowledge_categories(self, repo: Path) -> None:
        assert cdc.count_knowledge_categories(repo) == 2

    def test_knowledge_docs_by_category(self, repo: Path) -> None:
        assert cdc.count_knowledge_docs_by_category(repo) == {"math": 2, "technical": 1}

    def test_cli_scripts(self, repo: Path) -> None:
        assert cdc.count_cli_scripts(repo) == 2

    def test_stocklib_modules_excludes_init(self, repo: Path) -> None:
        assert cdc.count_stocklib_modules(repo) == 1

    def test_skills_commands_agents(self, repo: Path) -> None:
        assert cdc.count_skills(repo) == 2
        assert cdc.count_commands(repo) == 3
        assert cdc.count_agents(repo) == 1

    def test_measure_dict(self, repo: Path) -> None:
        actual = cdc.measure(repo)
        assert actual["knowledge_docs"] == 3
        assert actual["knowledge_categories"] == 2
        assert actual["cli_scripts"] == 2
        assert actual["stocklib_modules"] == 1
        assert actual["skills"] == 2
        assert actual["commands"] == 3
        assert actual["agents"] == 1
        assert actual["dir:math"] == 2

    def test_missing_directories_count_zero(self, tmp_path: Path) -> None:
        assert cdc.count_knowledge_docs(tmp_path) == 0
        assert cdc.count_cli_scripts(tmp_path) == 0
        assert cdc.count_skills(tmp_path) == 0

    def test_real_repo_counts_are_positive(self) -> None:
        actual = cdc.measure(_REPO_ROOT)
        for key in ("knowledge_docs", "cli_scripts", "skills", "commands", "agents"):
            assert actual[key] > 0


# ---------------------------------------------------------------------------
# 件数表記の検出
# ---------------------------------------------------------------------------


class TestDetection:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("スキル18種（`.claude/skills/*/SKILL.md`）", {"skills": 18}),
            ("スラッシュコマンド17種（`/analyze` 等）", {"commands": 17}),
            ("サブエージェント4種（stock-analyst 等）", {"agents": 4}),
            ("分析CLI 14本 + 共通ライブラリ `stocklib`", {"cli_scripts": 14}),
            ("共通ライブラリ `stocklib`、22本の CLI、ユニバース定義", {"cli_scripts": 22}),
            ("ナレッジベース100文書（`knowledge/`）", {"knowledge_docs": 100}),
            ("全90文書。", {"knowledge_docs": 90}),
            ("データソース・投資戦略の10カテゴリで日本株の知識を体系化する。", {"knowledge_categories": 10}),
            ("25のstocklib モジュール", {"stocklib_modules": 25}),
        ],
    )
    def test_markdown_expressions(self, line: str, expected: dict[str, int]) -> None:
        assert values_by_metric(scan(line)) == expected

    def test_multiple_metrics_on_one_line(self) -> None:
        line = "スキル18種・スラッシュコマンド20種・サブエージェント4種。"
        assert values_by_metric(scan(line)) == {"skills": 18, "commands": 20, "agents": 4}

    def test_bare_counts_without_counter(self) -> None:
        line = "スキル18・コマンド20・サブエージェント4。"
        assert values_by_metric(scan(line)) == {"skills": 18, "commands": 20, "agents": 4}

    def test_heading_style_bare_count(self) -> None:
        assert values_by_metric(scan("スキル 18 — どんなときに、どれを使うか")) == {"skills": 18}

    def test_docs_and_categories_together(self) -> None:
        assert values_by_metric(scan("100文書・10分野の索引。")) == {
            "knowledge_docs": 100,
            "knowledge_categories": 10,
        }

    def test_html_stat_block(self) -> None:
        line = '<div class="stat"><span class="n">90</span><span class="k">ナレッジ文書</span></div>'
        assert values_by_metric(scan(line, html=True)) == {"knowledge_docs": 90}

    def test_html_filter_count(self) -> None:
        line = '<span class="micro"><span data-filter-count="kb">100</span> 件</span>'
        assert values_by_metric(scan(line, html=True)) == {"knowledge_docs": 100}

    def test_html_meta_description(self) -> None:
        line = '<meta name="description" content="スキル18・スラッシュコマンド20・サブエージェント4・hooks 3 の一覧。">'
        assert values_by_metric(scan(line, html=True)) == {
            "skills": 18,
            "commands": 20,
            "agents": 4,
        }

    def test_document_index_heading(self) -> None:
        assert values_by_metric(scan("<h2>文書索引（90件）</h2>", html=True)) == {
            "knowledge_docs": 90
        }

    def test_total_docs_caption(self) -> None:
        line = '<p class="micro">数字は各ディレクトリ配下の Markdown 文書数（合計90）。</p>'
        assert values_by_metric(scan(line, html=True)) == {"knowledge_docs": 90}

    def test_category_directory_count(self) -> None:
        line = '<span class="call">technical · 5</span>'
        assert values_by_metric(scan(line, html=True)) == {"dir:technical": 5}


# ---------------------------------------------------------------------------
# 誤検知しないこと（件数表記でない数字・実体と無関係な数字は拾わない）
# ---------------------------------------------------------------------------


class TestNoFalsePositives:
    @pytest.mark.parametrize(
        "line",
        [
            "2024年11月に制度が変わった（2025年時点）。",
            "東証33業種の分類と TOPIX-17 のずれ。",
            "日経225先物と TOPIX 500 の裁定。",
            "ユニバースは主要30銘柄（liquid30.csv）。",
            "ma_cross / rsi_reversal / macd / bollinger_reversal / dca の5種類です。",
            "計画イシューのチェック済み・未着手を1周1件だけ安全に実装する。",
            "実データが1件も取れなかった場合は exit 2。",
            "個別銘柄の総合分析を1本走らせます。",
            "市況とウォッチリスト各銘柄のシグナル検出を1本にまとめる。",
            "この2本がこの環境の閉ループの実体です。",
            "20の CLI が対応（非対応は asset_plan.py と build_universe.py）。",
            "01（翌営業日の予想生成）を1コマンドで実行する夜間運用の本体です。",
            "スキル 2 つ目の候補。",
            "無料プランは12週間遅延（2026年時点）。",
            "PBR 1倍割れと ROE 8%基準。",
            "第3四半期の決算は8月10日に発表される。",
        ],
    )
    def test_plain_text_yields_no_findings(self, line: str) -> None:
        assert scan(line) == []

    @pytest.mark.parametrize(
        "line",
        [
            '<div class="stat"><span class="n">6</span><span class="k">カテゴリ</span></div>',
            '<div class="stat"><span class="n">9</span><span class="k">自動実行契約つき</span></div>',
            '<div class="stat"><span class="n">3</span><span class="k">hooks</span></div>',
            '<div class="stat"><span class="n">1</span><span class="k">索引 (00-index.md)</span></div>',
            '<div class="loop-step"><span class="n">01</span><h3>予想</h3></div>',
            '<span class="call">Category 05 · forecast</span>',
        ],
    )
    def test_html_without_mapped_labels_yields_no_findings(self, line: str) -> None:
        assert scan(line, html=True) == []

    def test_subset_sentence_does_not_leak_into_total(self) -> None:
        # 「5本です。」の直後に「分析 CLI」が来ても、句点をまたぐ文脈語は採用しない
        line = "最初に覚えるとよい5本です。分析 CLI は全部で22本あり、"
        assert values_by_metric(scan(line)) == {"cli_scripts": 22}

    def test_nearest_keyword_wins(self) -> None:
        # 「スキル」より「コマンド」の方が数字に近いので、コマンド数として読む
        assert values_by_metric(scan("スキル・スラッシュコマンド 3 種")) == {"commands": 3}

    def test_ambiguous_context_is_ignored(self) -> None:
        # 前後に別メトリクスの文脈語が同距離で並ぶ場合は判定不能として無視する
        rule = cdc.Rule(
            name="test",
            pattern=cdc.re.compile(r"(\d+)"),
            keyword_map=(("スキル", "skills"), ("コマンド", "commands")),
            window=10,
        )
        assert cdc.nearest_metric("スキル3コマンド", 3, 4, rule) is None

    def test_keyword_outside_window_is_ignored(self) -> None:
        rule = cdc.Rule(
            name="test",
            pattern=cdc.re.compile(r"(\d+)"),
            keyword_map=(("スキル", "skills"),),
            window=3,
        )
        assert cdc.nearest_metric("スキルははははは3", 8, 9, rule) is None

    def test_cli_tab_count_is_not_read_as_commands(self) -> None:
        line = '<span class="micro"><span data-filter-count="cli">22</span> 件</span>'
        assert values_by_metric(scan(line, html=True)) == {"cli_scripts": 22}


# ---------------------------------------------------------------------------
# 全体チェック（CI 用）
# ---------------------------------------------------------------------------


class TestCheckAll:
    def test_consistent_repo_passes(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (repo / "README.md").write_text(
            "ナレッジベース3文書。スキル2種・スラッシュコマンド3種・サブエージェント1種。\n"
            "共通ライブラリ `stocklib`、2本の CLI。\n",
            encoding="utf-8",
        )
        assert cdc.main(["prog"]) == 0
        assert "OK" in capsys.readouterr().out

    def test_no_count_expressions_passes(self, repo: Path) -> None:
        assert cdc.main(["prog"]) == 0

    def test_mismatch_is_detected(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (repo / "README.md").write_text("スキル15種とスラッシュコマンド17種。\n", encoding="utf-8")
        assert cdc.main(["prog"]) == 1
        err = capsys.readouterr().err
        assert "2 件の不一致" in err
        assert "README.md:1" in err
        assert "15" in err and "17" in err

    def test_mismatch_in_html_stat_block(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (repo / "docs" / "index.html").write_text(
            '<div class="stat"><span class="n">90</span><span class="k">ナレッジ文書</span></div>\n',
            encoding="utf-8",
        )
        assert cdc.main(["prog"]) == 1
        assert "docs/index.html:1" in capsys.readouterr().err

    def test_category_directory_mismatch_is_detected(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (repo / "docs" / "index.html").write_text(
            '<span class="call">math · 7</span>\n', encoding="utf-8"
        )
        assert cdc.main(["prog"]) == 1
        assert "knowledge/math/" in capsys.readouterr().err

    def test_all_target_files_are_scanned(self, repo: Path) -> None:
        rels = {p.relative_to(repo).as_posix() for p in cdc.target_files(repo)}
        assert rels == {"README.md", "CLAUDE.md", "knowledge/00-index.md", "docs/index.html"}

    def test_verbose_lists_findings(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (repo / "README.md").write_text("スキル2種。\n", encoding="utf-8")
        assert cdc.main(["prog", "--verbose"]) == 0
        out = capsys.readouterr().out
        assert "実測値:" in out
        assert "skills=2" in out


class TestRealRepository:
    def test_repo_root_defaults_to_script_parent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        assert cdc.repo_root() == _REPO_ROOT

    def test_current_repository_has_no_drift(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        exit_code = cdc.main(["prog"])
        captured = capsys.readouterr()
        assert exit_code == 0, captured.err
        assert "OK" in captured.out

    def test_current_repository_detects_many_expressions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 検出数が 0 に落ちたら（＝ルールが壊れて何も拾わなくなったら）気づけるようにする
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        _, findings = cdc.check_all(_REPO_ROOT)
        assert len(findings) >= 40

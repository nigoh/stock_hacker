"""書き込み経路のパス封じ込めテスト（ネットワーク不使用）。

**何を守っているのか**: 出力ファイル名にはユーザー入力がそのまま混ざる
（``analyze_stock.py --code`` → レポート名・チャート名、``research_journal.py --slug``
→ エントリのファイル名）。検証せずに連結すると ``--code ../../../../etc/x`` の
ような値で**意図した出力ディレクトリの外**に書き込めてしまう。セキュリティ監査で
``stocklib.report.save_report`` に実際に見つかった欠陥がこれで、同型の経路が
``charts`` / ``journal`` にも残っていた。

このテストは**モックを使わず実際に一時ディレクトリへ書き込み、ファイルが
どこに着地したかを確認する**（「例外が出たか」だけでは、書き込まれてから
例外が出る実装を見逃すため）。各経路について
(1) 悪意あるファイル名が出力ディレクトリ外に出ないこと、(2) 正常系が通ること、
(3) 不正名が拒否されることを検証する。

封じ込めの実装は :mod:`stocklib.safepath`。
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pandas as pd
import pytest

from stocklib import charts, forecast, journal, report
from stocklib.data import synthetic_prices
from stocklib.journal import JournalEntry, JournalError
from stocklib.safepath import contained_path, safe_name

REPO_ROOT = Path(__file__).resolve().parents[2]

# 出力ディレクトリの外へ抜け出そうとする典型的なファイル名（無害化して受け入れる）
TRAVERSAL_NAMES: tuple[str, ...] = (
    "../escaped",
    "../../escaped",
    "../../../../tmp/escaped",
    "sub/escaped",
    "/tmp/escaped",
)

# 受け付けてはいけないファイル名（空・カレント/親ディレクトリ・隠しファイル・NUL）。
# ``..\\x`` は POSIX では区切り文字ではなく単なる先頭ドットのファイル名になるため、
# 「隠しファイル」として拒否される（Windows 形式の traversal もここで止まる）。
INVALID_NAMES: tuple[str, ...] = ("", ".", "..", ".hidden", "a\x00b", "..\\escaped")


def _tree(root: Path) -> set[Path]:
    """``root`` 配下の全ファイルの集合（着地点の確認用）。"""
    return {p for p in root.rglob("*") if p.is_file()}


# ---------------------------------------------------------------------------
# stocklib.safepath（封じ込めの土台）
# ---------------------------------------------------------------------------


class TestSafePath:
    def test_safe_name_strips_directory_components(self) -> None:
        assert safe_name("a/b/c.md") == "c.md"
        assert safe_name("../../x.md") == "x.md"
        assert safe_name("/etc/passwd") == "passwd"

    @pytest.mark.parametrize("name", INVALID_NAMES)
    def test_safe_name_rejects_invalid(self, name: str) -> None:
        with pytest.raises(ValueError):
            safe_name(name)

    def test_contained_path_stays_inside_base(self, tmp_path: Path) -> None:
        base = tmp_path / "out"
        base.mkdir()
        for name in TRAVERSAL_NAMES:
            path = contained_path(base, f"{name}.md")
            assert path.parent == base.resolve(), f"{name} が {path} に逃げた"


# ---------------------------------------------------------------------------
# 経路1: reports/img/ へのチャート出力（stocklib.charts）
# ---------------------------------------------------------------------------


class TestChartsContainment:
    """``charts.img_path`` / ``charts.save_figure``。

    実際の攻撃経路は ``analyze_stock.py`` の ``--code``（→ ``img_stem``）。
    """

    @pytest.fixture()
    def img_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        d = tmp_path / "reports" / "img"
        d.mkdir(parents=True)
        monkeypatch.setattr(charts, "IMG_DIR", d)
        return d

    @pytest.mark.parametrize("stem", TRAVERSAL_NAMES)
    def test_malicious_stem_lands_inside_img_dir(
        self, img_dir: Path, tmp_path: Path, stem: str
    ) -> None:
        # analyze_stock.py と同じ組み立て方（img_stem に --code が入る）
        path = charts.img_path(f"{stem}-price.png")
        assert path.parent == img_dir.resolve()

        # 実際に PNG を書き、reports/img/ の中にだけ落ちることを確認する
        df = synthetic_prices("7203", days=120)
        out = charts.plot_price_chart(df, "7203", path)
        assert out.exists()
        assert out.parent == img_dir.resolve()
        assert _tree(tmp_path) <= _tree(img_dir), "reports/img/ の外にファイルが作られた"

    @pytest.mark.parametrize("name", INVALID_NAMES)
    def test_img_path_rejects_invalid_names(self, img_dir: Path, name: str) -> None:
        with pytest.raises(ValueError):
            charts.img_path(name)

    def test_save_figure_rejects_parent_traversal(self, img_dir: Path, tmp_path: Path) -> None:
        # IMG_DIR / "../.." のように .. を含むパスを直接渡された場合も拒否する
        df = synthetic_prices("7203", days=120)
        before = _tree(tmp_path)
        with pytest.raises(ValueError):
            charts.plot_price_chart(df, "7203", img_dir / ".." / ".." / "escaped.png")
        assert _tree(tmp_path) == before, "拒否したのにファイルが書かれた"

    def test_normal_filenames_still_work(self, img_dir: Path) -> None:
        today = dt.date.today().isoformat()
        # 各 CLI が実際に生成する img_stem（既存の正常系）
        for name in (
            f"analyze-7203-{today}-price.png",
            f"analyze-7203-mid-{today}-price.png",
            f"compare-7203-6758-usd-{today}-relative.png",
            f"backtest-ma_cross-7203-{today}-equity.png",
            f"backtest-dca-7203-{today}-dca.png",
            f"plan-project-{today}.png",
        ):
            assert charts.img_path(name).parent == img_dir.resolve()

    def test_explicit_outside_dir_still_allowed(self, tmp_path: Path) -> None:
        # save_figure 自体は出力先ディレクトリを縛らない（テストが tmp_path を使うため）。
        # 封じ込めは img_path の責務。
        df = synthetic_prices("7203", days=120)
        out = charts.plot_price_chart(df, "7203", tmp_path / "elsewhere" / "ok.png")
        assert out.exists()

    def test_cli_callers_use_img_path(self) -> None:
        """CLI が ``IMG_DIR`` を直接 join していないことを守る（再発防止）。

        ``charts.IMG_DIR / f"{img_stem}-...png"`` と書くと封じ込めを迂回できるため、
        チャートを出す CLI は必ず ``charts.img_path(...)`` を使う。
        """
        # IMG_DIR に続けてパスを join しているコード（IMG_DIR / "..." / f"...")
        join_re = re.compile(r"IMG_DIR\s*\)?\s*/\s*[fr]?[\"']")
        offenders = []
        for name in ("analyze_stock.py", "compare.py", "run_backtest.py", "asset_plan.py"):
            text = (REPO_ROOT / "analysis" / name).read_text(encoding="utf-8")
            for line in text.splitlines():
                if join_re.search(line):
                    offenders.append(f"{name}: {line.strip()}")
        assert not offenders, "IMG_DIR を直接 join せず charts.img_path を使うこと: " + str(offenders)


# ---------------------------------------------------------------------------
# 経路2: journal/<YYYY>/ へのエントリ出力（stocklib.journal）
# ---------------------------------------------------------------------------


def _entry(entry_id: str = "2026-01-05-test") -> JournalEntry:
    return JournalEntry(
        id=entry_id,
        date=dt.date(2026, 1, 5),
        title="テスト仮説",
        codes=["7203"],
        direction={"7203": "up"},
        review_date=dt.date(2026, 3, 6),
        status="open",
        data="real",
        entry_prices={"7203": 1000.0},
        benchmark="^N225",
        benchmark_entry=40000.0,
        body="## 仮説\n\nx\n\n## 根拠\n\ny\n\n## 反証条件\n\nz\n\n## 検証結果\n\n（未検証）",
    )


class TestJournalContainment:
    """``journal.new_entry`` / ``journal.save_entry``。攻撃経路は ``--slug``。"""

    @pytest.mark.parametrize("slug", ["../evil", "../../evil", "sub/evil", "/tmp/evil"])
    def test_malicious_slug_is_rejected_and_writes_nothing(
        self, tmp_path: Path, slug: str
    ) -> None:
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        before = _tree(tmp_path)
        with pytest.raises(JournalError):
            journal.new_entry(
                codes=["7203"], title="t", direction="up", review_days=30,
                synthetic=True, slug=slug, journal_dir=journal_dir,
            )
        assert _tree(tmp_path) == before, f"slug={slug!r} でファイルが書かれた"

    @pytest.mark.parametrize("slug", [" ", ".", "..", "\x00"])
    def test_empty_or_dot_slug_is_rejected(self, tmp_path: Path, slug: str) -> None:
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        with pytest.raises(JournalError):
            journal.new_entry(
                codes=["7203"], title="t", direction="up", review_days=30,
                synthetic=True, slug=slug, journal_dir=journal_dir,
            )

    def test_normal_slug_creates_entry_in_year_dir(self, tmp_path: Path) -> None:
        journal_dir = tmp_path / "journal"
        entry = journal.new_entry(
            codes=["7203"], title="ゴールデンクロス後のモメンタム", direction="up",
            review_days=60, synthetic=True, slug="golden-cross", journal_dir=journal_dir,
            today=dt.date(2026, 7, 25),
        )
        assert entry.path == journal_dir / "2026" / "2026-07-25-golden-cross.md"
        assert entry.path.exists()
        assert _tree(tmp_path) == {entry.path}

    def test_auto_generated_slug_still_works(self, tmp_path: Path) -> None:
        # make_slug 由来（日本語タイトル → コードで代替）の正常系
        entry = journal.new_entry(
            codes=["7203"], title="日本語だけのタイトル", direction="up", review_days=30,
            synthetic=True, journal_dir=tmp_path / "journal", today=dt.date(2026, 7, 25),
        )
        assert entry.path is not None and entry.path.exists()
        assert entry.path.name == "2026-07-25-7203.md"

    def test_save_entry_contains_malicious_id_inside_year_dir(self, tmp_path: Path) -> None:
        # entry.id を直接汚染された場合も年ディレクトリの中に封じ込める
        journal_dir = tmp_path / "journal"
        entry = _entry(entry_id="../../../../escaped")
        path = journal.save_entry(entry, journal_dir)
        assert path.parent == (journal_dir / "2026").resolve()
        assert _tree(tmp_path) == {path}

    def test_save_entry_rejects_invalid_explicit_path(self, tmp_path: Path) -> None:
        entry = _entry()
        entry.path = tmp_path / "journal" / "2026" / ".."
        before = _tree(tmp_path)
        with pytest.raises(ValueError):
            journal.save_entry(entry, tmp_path / "journal")
        assert _tree(tmp_path) == before

    def test_explicit_path_outside_is_still_allowed(self, tmp_path: Path) -> None:
        # verify は「ユーザーが指定した既存エントリ」に書き戻すため、
        # 明示パスへの保存自体は禁止しない（ファイル名の健全性だけ見る）。
        entry = _entry()
        entry.path = tmp_path / "elsewhere" / "2026-01-05-test.md"
        assert journal.save_entry(entry, tmp_path / "journal").exists()


# ---------------------------------------------------------------------------
# 経路3: forecasts/ の台帳 CSV（stocklib.forecast）
# ---------------------------------------------------------------------------


class TestForecastLedger:
    """``forecast.save_ledger``。

    ``--ledger`` で任意パスを指定できるのは正当な機能なので**封じ込めない**。
    親ディレクトリの作成とファイル名の健全性のみを検査する。
    """

    @staticmethod
    def _ledger() -> pd.DataFrame:
        return pd.DataFrame(columns=list(forecast.LEDGER_COLUMNS))

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / "ledger.csv"
        out = forecast.save_ledger(self._ledger(), path)
        assert out.exists() and out == path

    # ``tmp_path / ""`` や ``tmp_path / "."`` は pathlib が畳んでしまい
    # 「不正なファイル名」として渡せないため、実際に指定しうる形だけを検査する。
    @pytest.mark.parametrize("name", [".hidden", "a\x00b", "..\\evil"])
    def test_rejects_invalid_filenames(self, tmp_path: Path, name: str) -> None:
        before = _tree(tmp_path)
        with pytest.raises(ValueError):
            forecast.save_ledger(self._ledger(), tmp_path / name)
        assert _tree(tmp_path) == before

    def test_rejects_parent_dir_as_filename(self, tmp_path: Path) -> None:
        # --ledger forecasts/sub/.. のようにベース名が ".." になる指定
        (tmp_path / "sub").mkdir()
        with pytest.raises(ValueError):
            forecast.save_ledger(self._ledger(), tmp_path / "sub" / "..")

    def test_rejects_directory_target(self, tmp_path: Path) -> None:
        # --ledger forecasts/ のようにディレクトリを渡された場合は分かる形で失敗する
        with pytest.raises(ValueError, match="ディレクトリ"):
            forecast.save_ledger(self._ledger(), tmp_path)

    def test_arbitrary_path_is_allowed_by_design(self, tmp_path: Path) -> None:
        # 封じ込めないことが仕様（複数台帳の使い分け・検証用の別ファイル）
        outside = tmp_path / "somewhere" / "my-ledger.csv"
        assert forecast.save_ledger(self._ledger(), outside).exists()

    def test_default_ledger_name_is_valid(self) -> None:
        assert safe_name(forecast.DEFAULT_LEDGER.name) == "ledger.csv"


# ---------------------------------------------------------------------------
# 経路0（既修正）: reports/ へのレポート出力。回帰しないよう併せて守る
# ---------------------------------------------------------------------------


class TestReportContainment:
    @pytest.fixture()
    def reports_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        d = tmp_path / "reports"
        d.mkdir()
        monkeypatch.setattr(report, "REPORTS_DIR", d)
        return d

    @pytest.mark.parametrize("code", ["../../../../etc/evil", "sub/evil", "/tmp/evil"])
    def test_malicious_code_lands_inside_reports(
        self, reports_dir: Path, tmp_path: Path, code: str
    ) -> None:
        # analyze_stock.py と同じ組み立て方
        filename = f"analyze-{code}-2026-07-25.md"
        path = report.save_report("# レポート\n\n本文。\n", filename)
        assert path.parent == reports_dir.resolve()
        assert _tree(tmp_path) == {path}

    @pytest.mark.parametrize("name", INVALID_NAMES)
    def test_rejects_invalid_filenames(self, reports_dir: Path, name: str) -> None:
        with pytest.raises(ValueError):
            report.save_report("# x\n", name)

    def test_normal_filename_works(self, reports_dir: Path) -> None:
        path = report.save_report("# x\n", "analyze-7203-2026-07-25.md")
        assert path == (reports_dir / "analyze-7203-2026-07-25.md").resolve()

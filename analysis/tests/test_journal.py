"""stocklib.journal / research_journal.py のテスト（合成データ・ネットワーク不使用）。"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import pytest

from stocklib import journal
from stocklib.journal import (
    MIXED_THRESHOLD,
    JournalEntry,
    JournalError,
    aggregate_outcome,
    due_entries,
    dump_frontmatter,
    entry_from_parts,
    entry_to_markdown,
    judge_direction,
    load_entry,
    make_slug,
    new_entry,
    normalize_direction,
    parse_frontmatter,
    save_entry,
    verify_entry,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TODAY = dt.date.today()


def _make_entry(
    *,
    codes: list[str] | None = None,
    direction: dict[str, str] | None = None,
    entry_prices: dict[str, float] | None = None,
    review_date: dt.date | None = None,
    status: str = "open",
    data: str = "real",
) -> JournalEntry:
    codes = codes or ["7203"]
    return JournalEntry(
        id="2026-01-05-test",
        date=dt.date(2026, 1, 5),
        title="テスト仮説",
        codes=codes,
        direction=direction or {c: "up" for c in codes},
        review_date=review_date or dt.date(2026, 3, 6),
        status=status,
        data=data,
        entry_prices=entry_prices or {c: 1000.0 for c in codes},
        benchmark="^N225",
        benchmark_entry=40000.0,
        body="## 仮説\n\nx\n\n## 根拠\n\ny\n\n## 反証条件\n\nz\n\n## 検証結果\n\n（未検証）",
    )


# ---------------------------------------------------------------------------
# frontmatter パーサ
# ---------------------------------------------------------------------------


def test_frontmatter_roundtrip_scalar_list_nested() -> None:
    meta: dict[str, object] = {
        "id": "2026-01-05-test",
        "date": dt.date(2026, 1, 5),
        "title": "決算後の上方修正期待: 【テスト】",
        "codes": ["7203", "6758"],
        "direction": {"7203": "up", "6758": "down"},
        "benchmark_entry": 40000.5,
        "empty": None,
        "flag": True,
    }
    text = dump_frontmatter(meta) + "\n本文です。\n"
    parsed, body = parse_frontmatter(text)
    assert body.strip() == "本文です。"
    assert parsed["id"] == "2026-01-05-test"
    assert parsed["date"] == "2026-01-05"  # 日付は文字列として往復（dataclass 側で変換）
    assert parsed["title"] == meta["title"]
    assert parsed["codes"] == ["7203", "6758"]  # 引用符付きなので文字列のまま
    assert parsed["direction"] == {"7203": "up", "6758": "down"}
    assert parsed["benchmark_entry"] == pytest.approx(40000.5)
    assert parsed["empty"] is None
    assert parsed["flag"] is True


def test_frontmatter_absent_returns_empty_meta() -> None:
    meta, body = parse_frontmatter("# ただの Markdown\n")
    assert meta == {}
    assert body == "# ただの Markdown\n"


def test_frontmatter_rejects_broken_line() -> None:
    text = "---\nid test\n---\n"
    with pytest.raises(JournalError):
        parse_frontmatter(text)


def test_entry_roundtrip_through_markdown(tmp_path: Path) -> None:
    entry = _make_entry(
        codes=["7203", "6758"],
        direction={"7203": "up", "6758": "down"},
        entry_prices={"7203": 3250.5, "6758": 12000.0},
    )
    entry.path = tmp_path / "2026" / f"{entry.id}.md"
    save_entry(entry)
    loaded = load_entry(entry.path)
    assert loaded.id == entry.id
    assert loaded.date == entry.date
    assert loaded.codes == ["7203", "6758"]
    assert loaded.direction == {"7203": "up", "6758": "down"}
    assert loaded.review_date == entry.review_date
    assert loaded.status == "open"
    assert loaded.outcome == "pending"
    assert loaded.entry_prices == {"7203": pytest.approx(3250.5), "6758": pytest.approx(12000.0)}
    assert loaded.benchmark_entry == pytest.approx(40000.0)
    assert "## 反証条件" in loaded.body


def test_uniform_direction_dumped_as_scalar() -> None:
    entry = _make_entry(codes=["7203", "6758"], direction={"7203": "up", "6758": "up"})
    text = entry_to_markdown(entry)
    assert "direction: up" in text
    meta, _ = parse_frontmatter(text)
    loaded = entry_from_parts(meta, "")
    assert loaded.direction == {"7203": "up", "6758": "up"}


def test_entry_validation_rejects_bad_values() -> None:
    entry = _make_entry()
    entry.direction = {"7203": "sideways"}
    with pytest.raises(JournalError):
        entry.validate()
    entry = _make_entry(status="closed")
    with pytest.raises(JournalError):
        entry.validate()
    entry = _make_entry(data="fake")
    with pytest.raises(JournalError):
        entry.validate()


def test_data_field_roundtrip_and_legacy_default(tmp_path: Path) -> None:
    # data: synthetic は frontmatter に書かれ、往復して保持される
    entry = _make_entry(data="synthetic")
    text = entry_to_markdown(entry)
    assert "data: synthetic" in text
    meta, body = parse_frontmatter(text)
    assert entry_from_parts(meta, body).data == "synthetic"
    # data キーの無い既存エントリは real 扱い（後方互換）
    meta.pop("data")
    assert entry_from_parts(meta, body).data == "real"


# ---------------------------------------------------------------------------
# slug / direction 正規化
# ---------------------------------------------------------------------------


def test_make_slug_ascii_title() -> None:
    assert make_slug("Golden Cross Momentum!", ["7203"]) == "golden-cross-momentum"


def test_make_slug_japanese_falls_back_to_codes() -> None:
    assert make_slug("決算後の上方修正期待", ["7203", "6758"]) == "7203-6758"


def test_normalize_direction_single_and_per_code() -> None:
    assert normalize_direction("up", ["7203", "6758"]) == {"7203": "up", "6758": "up"}
    assert normalize_direction("7203:up, 6758:down", ["7203", "6758"]) == {
        "7203": "up",
        "6758": "down",
    }


def test_normalize_direction_rejects_bad_input() -> None:
    with pytest.raises(JournalError):
        normalize_direction("sideways", ["7203"])
    with pytest.raises(JournalError):
        normalize_direction("7203:up", ["7203", "6758"])  # 6758 が未指定
    with pytest.raises(JournalError):
        normalize_direction("9999:up", ["7203"])  # codes に無い銘柄


# ---------------------------------------------------------------------------
# new_entry（雛形生成、合成データ）
# ---------------------------------------------------------------------------


def test_new_entry_creates_template_with_snapshot(tmp_path: Path) -> None:
    entry = new_entry(
        codes=["7203"],
        title="テスト仮説",
        direction="up",
        review_days=60,
        synthetic=True,
        journal_dir=tmp_path,
    )
    assert entry.path is not None
    assert entry.path.exists()
    assert entry.path.parent.name == str(TODAY.year)
    assert entry.review_date == TODAY + dt.timedelta(days=60)
    assert entry.status == "open" and entry.outcome == "pending"
    # スナップショットが合成データの直近終値と一致する
    from stocklib.data import fetch_prices

    close = float(fetch_prices("7203", period="3mo", synthetic=True)["7203"]["Close"].iloc[-1])
    assert entry.entry_prices["7203"] == pytest.approx(close, rel=1e-6)
    assert entry.benchmark_entry is not None and entry.benchmark_entry > 0
    text = entry.path.read_text(encoding="utf-8")
    for section in ("## 仮説", "## 根拠", "## 反証条件", "## 検証結果"):
        assert section in text
    assert "合成データ" in text  # synthetic 注記
    # 機械可読なデータ出所の印が frontmatter に書かれる
    assert entry.data == "synthetic"
    assert "data: synthetic" in text


def test_new_entry_rejects_duplicate_id(tmp_path: Path) -> None:
    new_entry(codes=["7203"], title="t", direction="up", review_days=1,
              synthetic=True, slug="dup", journal_dir=tmp_path)
    with pytest.raises(JournalError):
        new_entry(codes=["7203"], title="t", direction="up", review_days=1,
                  synthetic=True, slug="dup", journal_dir=tmp_path)


# ---------------------------------------------------------------------------
# due 判定の境界
# ---------------------------------------------------------------------------


def test_due_entries_boundary() -> None:
    today = dt.date(2026, 7, 16)
    yesterday_due = _make_entry(review_date=dt.date(2026, 7, 15))
    today_due = _make_entry(review_date=today)
    tomorrow = _make_entry(review_date=dt.date(2026, 7, 17))
    reviewed = _make_entry(review_date=dt.date(2026, 7, 1), status="reviewed")
    due = due_entries([yesterday_due, today_due, tomorrow, reviewed], today=today)
    assert due == [yesterday_due, today_due]  # 当日を含む・期日超過を含む・reviewed は除外


# ---------------------------------------------------------------------------
# iter_entries（非エントリの黙殺と壊れエントリの警告の区別）
# ---------------------------------------------------------------------------


def test_iter_entries_warns_on_broken_frontmatter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    year_dir = tmp_path / "2026"
    year_dir.mkdir(parents=True)
    # frontmatter を持たない非エントリ → 黙ってスキップ（警告なし）
    (year_dir / "notes.md").write_text("# ただのメモ\n", encoding="utf-8")
    # frontmatter はあるが必須キー欠落で壊れている → stderr に警告してスキップ
    (year_dir / "broken.md").write_text("---\nid: broken\n---\n\n本文\n", encoding="utf-8")
    # 正常なエントリ
    ok = _make_entry()
    ok.path = year_dir / f"{ok.id}.md"
    save_entry(ok)

    entries = journal.iter_entries(tmp_path)
    captured = capsys.readouterr()
    assert [e.id for e in entries] == [ok.id]
    assert "警告" in captured.err and "broken.md" in captured.err
    assert "notes.md" not in captured.err  # 非エントリは警告を出さない


# ---------------------------------------------------------------------------
# 判定ロジック（judge_direction / aggregate_outcome）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("direction", "excess", "expected"),
    [
        ("up", 0.05, "hit"),
        ("up", -0.05, "miss"),
        ("up", 0.019999, "mixed"),
        ("up", -0.019999, "mixed"),
        ("up", MIXED_THRESHOLD, "hit"),  # 境界: ちょうど +2% は hit
        ("up", -MIXED_THRESHOLD, "miss"),  # 境界: ちょうど -2% は miss
        ("down", -0.05, "hit"),
        ("down", 0.05, "miss"),
        ("down", 0.01, "mixed"),
        ("neutral", 0.0, "hit"),
        ("neutral", 0.019999, "hit"),
        ("neutral", MIXED_THRESHOLD, "miss"),
        ("neutral", -0.05, "miss"),
    ],
)
def test_judge_direction(direction: str, excess: float, expected: str) -> None:
    assert judge_direction(direction, excess) == expected


def test_judge_direction_rejects_unknown() -> None:
    with pytest.raises(JournalError):
        judge_direction("sideways", 0.0)


def test_aggregate_outcome() -> None:
    assert aggregate_outcome(["hit", "hit"]) == "hit"
    assert aggregate_outcome(["miss"]) == "miss"
    assert aggregate_outcome(["hit", "miss"]) == "mixed"
    assert aggregate_outcome(["mixed", "hit"]) == "mixed"
    with pytest.raises(JournalError):
        aggregate_outcome([])


# ---------------------------------------------------------------------------
# verify（人工価格で手計算一致）
# ---------------------------------------------------------------------------


def test_verify_hit_with_artificial_prices(tmp_path: Path) -> None:
    # 銘柄 +10%、ベンチマーク +2% → 超過 +8% → up は hit
    entry = _make_entry(entry_prices={"7203": 1000.0})
    entry.path = tmp_path / "2026" / f"{entry.id}.md"
    save_entry(entry)
    verdicts = verify_entry(
        entry,
        today=dt.date(2026, 3, 6),
        current_prices={"7203": 1100.0},
        benchmark_price=40800.0,
    )
    (v,) = verdicts
    assert v.stock_return == pytest.approx(0.10)
    assert v.benchmark_return == pytest.approx(0.02)
    assert v.excess_return == pytest.approx(0.08)
    assert v.result == "hit"
    assert entry.status == "reviewed"
    assert entry.outcome == "hit"
    assert entry.verified_date == dt.date(2026, 3, 6)
    # ファイルにも反映されている
    loaded = load_entry(entry.path)
    assert loaded.status == "reviewed" and loaded.outcome == "hit"
    assert "総合判定: **hit**" in loaded.body
    assert "（未検証）" not in loaded.body  # プレースホルダーが置換されている


def test_verify_miss_and_mixed_aggregate(tmp_path: Path) -> None:
    # 7203: up だが超過 -10% → miss、6758: down で超過 -10% → hit → 総合 mixed
    entry = _make_entry(
        codes=["7203", "6758"],
        direction={"7203": "up", "6758": "down"},
        entry_prices={"7203": 1000.0, "6758": 2000.0},
    )
    entry.path = tmp_path / "2026" / f"{entry.id}.md"
    save_entry(entry)
    verdicts = verify_entry(
        entry,
        today=dt.date(2026, 3, 6),
        current_prices={"7203": 900.0, "6758": 1800.0},
        benchmark_price=40000.0,  # ベンチマーク騰落 0%
    )
    assert [v.result for v in verdicts] == ["miss", "hit"]
    assert entry.outcome == "mixed"


def test_verify_mixed_below_threshold(tmp_path: Path) -> None:
    # 銘柄 +1%、ベンチマーク 0% → 超過 +1%（<2%）→ up は mixed
    entry = _make_entry(entry_prices={"7203": 1000.0})
    entry.path = tmp_path / "2026" / f"{entry.id}.md"
    save_entry(entry)
    verify_entry(entry, current_prices={"7203": 1010.0}, benchmark_price=40000.0)
    assert entry.outcome == "mixed"


def test_verify_rejects_already_reviewed(tmp_path: Path) -> None:
    entry = _make_entry(status="reviewed")
    with pytest.raises(JournalError):
        verify_entry(entry, current_prices={"7203": 1000.0}, benchmark_price=40000.0)


def test_verify_rejects_data_source_mismatch(tmp_path: Path) -> None:
    """合成スナップショットの実データ検証（およびその逆）を JournalError で拒否する。"""
    # data: synthetic のエントリを実データ（synthetic=False）で verify → 拒否
    entry = new_entry(
        codes=["7203"], title="t", direction="up", review_days=0,
        synthetic=True, slug="mismatch-syn", journal_dir=tmp_path,
    )
    assert entry.data == "synthetic"
    with pytest.raises(JournalError, match="無意味"):
        verify_entry(entry, synthetic=False)
    assert entry.status == "open"  # 拒否時はエントリが変更されない

    # data: real のエントリを --synthetic で verify → 拒否（価格取得前に落ちるのでネットワーク不要）
    real_entry = _make_entry(data="real")
    real_entry.path = tmp_path / "2026" / f"{real_entry.id}.md"
    save_entry(real_entry)
    with pytest.raises(JournalError, match="data: real"):
        verify_entry(real_entry, synthetic=True)


def test_verify_mismatch_check_skipped_with_injected_prices(tmp_path: Path) -> None:
    """current_prices / benchmark_price を完全指定した場合は従来どおり検証できる。"""
    entry = _make_entry(data="synthetic", entry_prices={"7203": 1000.0})
    entry.path = tmp_path / "2026" / f"{entry.id}.md"
    save_entry(entry)
    verdicts = verify_entry(
        entry,
        synthetic=False,  # data: synthetic と不一致だが、価格取得が発生しないため許容
        current_prices={"7203": 1100.0},
        benchmark_price=40000.0,
    )
    assert verdicts[0].result == "hit"
    assert entry.status == "reviewed"


def test_verify_synthetic_end_to_end(tmp_path: Path) -> None:
    """new → verify を合成データのみで通す（価格注入なし）。"""
    entry = new_entry(
        codes=["7203"], title="t", direction="neutral", review_days=0,
        synthetic=True, slug="e2e", journal_dir=tmp_path,
    )
    verdicts = verify_entry(entry, synthetic=True)
    assert entry.status == "reviewed"
    assert entry.outcome in ("hit", "miss", "mixed")
    assert len(verdicts) == 1
    assert "検証時価格は合成データ" in entry.path.read_text(encoding="utf-8")  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# CLI スモーク（subprocess + --synthetic）
# ---------------------------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "analysis/research_journal.py", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_cli_new_due_verify_list_flow(tmp_path: Path) -> None:
    jdir = str(tmp_path / "journal")
    # new（review-days 0 で即日 due にする）
    proc = _run(
        "--journal-dir", jdir, "new",
        "--codes", "7203",
        "--title", "決算後の上方修正期待",
        "--direction", "up",
        "--review-days", "0",
        "--synthetic",
    )
    assert proc.returncode == 0, proc.stderr
    entry_path = Path(proc.stdout.strip().splitlines()[-1])
    assert entry_path.exists()
    assert entry_path.name == f"{TODAY.isoformat()}-7203.md"

    # due に載る
    proc = _run("--journal-dir", jdir, "due", "--synthetic")
    assert proc.returncode == 0, proc.stderr
    assert "検証期日を迎えたエントリ: 1 件" in proc.stdout
    assert str(entry_path) in proc.stdout

    # verify
    proc = _run("--journal-dir", jdir, "verify", str(entry_path), "--synthetic")
    assert proc.returncode == 0, proc.stderr
    assert "総合判定:" in proc.stdout
    content = entry_path.read_text(encoding="utf-8")
    assert "status: reviewed" in content
    assert "## 検証結果" in content and "判定基準" in content

    # verify 済みは due から消え、list に反映される
    proc = _run("--journal-dir", jdir, "due")
    assert proc.returncode == 0, proc.stderr
    assert "検証期日を迎えたエントリはありません" in proc.stdout
    proc = _run("--journal-dir", jdir, "list")
    assert proc.returncode == 0, proc.stderr
    assert "reviewed" in proc.stdout and "全 1 件" in proc.stdout


def test_cli_verify_twice_fails(tmp_path: Path) -> None:
    jdir = str(tmp_path / "journal")
    proc = _run("--journal-dir", jdir, "new", "--codes", "7203", "--title", "t",
                "--direction", "up", "--review-days", "0", "--synthetic")
    assert proc.returncode == 0, proc.stderr
    entry_path = proc.stdout.strip().splitlines()[-1]
    assert _run("--journal-dir", jdir, "verify", entry_path, "--synthetic").returncode == 0
    proc = _run("--journal-dir", jdir, "verify", entry_path, "--synthetic")
    assert proc.returncode == 1
    assert "エラー" in proc.stderr


def test_cli_verify_rejects_data_source_mismatch(tmp_path: Path) -> None:
    """合成スナップショットのエントリを --synthetic なしで verify すると拒否される。"""
    jdir = str(tmp_path / "journal")
    proc = _run("--journal-dir", jdir, "new", "--codes", "7203", "--title", "t",
                "--direction", "up", "--review-days", "0", "--synthetic")
    assert proc.returncode == 0, proc.stderr
    entry_path = proc.stdout.strip().splitlines()[-1]
    # 整合チェックは価格取得前に走るためネットワーク不要で拒否される
    proc = _run("--journal-dir", jdir, "verify", entry_path)
    assert proc.returncode == 1
    assert "data: synthetic" in proc.stderr and "無意味" in proc.stderr
    # エントリは open のまま（status: reviewed に汚染されない）
    assert "status: open" in Path(entry_path).read_text(encoding="utf-8")


def test_cli_new_rejects_bad_direction(tmp_path: Path) -> None:
    proc = _run("--journal-dir", str(tmp_path), "new", "--codes", "7203", "--title", "t",
                "--direction", "sideways", "--synthetic")
    assert proc.returncode == 1
    assert "エラー" in proc.stderr


# ---------------------------------------------------------------------------
# コミット済みサンプルエントリの整合性
# ---------------------------------------------------------------------------


def test_committed_sample_entry_is_valid() -> None:
    sample_dir = REPO_ROOT / "journal"
    entries = journal.iter_entries(sample_dir)
    assert entries, "journal/ にサンプルエントリが必要"
    for e in entries:
        e.validate()
    # 実運用エントリが増えても壊れないよう、サンプルは id で特定する
    sample_id = "2026-07-16-sample-synthetic-golden-cross"
    matches = [e for e in entries if e.id == sample_id]
    assert matches, f"サンプルエントリ（id: {sample_id}）が journal/ に必要"
    sample = matches[0]
    assert "サンプル" in sample.title
    assert "合成データ" in sample.body  # サンプルである旨・合成データの明記
    assert sample.data == "synthetic"  # 機械可読な印（実データでの verify を拒否するため）

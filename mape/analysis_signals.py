#!/usr/bin/env python3
"""MAPE-K Monitor の中核 — 「株の解析の醸成」シグナル抽出（決定論・stdlib のみ・ネットワーク不使用）。

MAPE-K の主題は**日本株分析そのものの醸成**であり、リポジトリ/システムの健全化ではない。
本スクリプトは分析ドメインのシグナルを2系統、``KEY=VALUE`` 形式で stdout に出す:

  【予測精度（answer-checking）】分析が当たっていたか
    夜間フォーキャストの実績台帳（forecasts/ledger.csv）とリサーチジャーナル（journal/）から、
    方向的中率・Brier・レンジ的中・仮説 hit 率・未採点/検証期日超過を集計する。

  【分析カバレッジ（asset growth）】分析をどれだけ広げ・新鮮に保てているか
    ユニバース（analysis/universe/liquid30.csv）のうち分析記録（台帳/ジャーナル）がある銘柄の割合、
    未分析銘柄、ナレッジ文書数と陳腐化（「20XX年時点」の最新が (今年-2) 以下）を集計する。

価格データ・pandas・yfinance に依存しない（Monitor の「安く読む」フェーズに載せるため）。合成データ
（data=synthetic）の予想・サンプル仮説は track record・カバレッジに数えない（実市況の偽装防止）。

使い方:
    python3 mape/analysis_signals.py [<repo_root>] [<today=YYYY-MM-DD>] [<state_dir>]

<state_dir> を渡すと未分析銘柄一覧（unanalyzed-codes.txt）・陳腐化文書一覧（stale-docs.txt）も書き出す。
"""

from __future__ import annotations

import csv
import datetime as dt
import re
import sys
from pathlib import Path

_TRUE = {"true", "1", "yes", "y", "t"}
_CODE_RE = re.compile(r"\b(\d{4})\b")
_ASOF_YEAR_RE = re.compile(r"(20\d{2})\s*年時点")
_STALE_AGE = 2  # 「今年 - 2」年以下が最新の「〜年時点」なら陳腐化候補


def _is_true(value: str) -> bool:
    return str(value).strip().lower() in _TRUE


# --------------------------------------------------------------------------
# 予測精度（answer-checking）
# --------------------------------------------------------------------------
def forecast_signals(root: Path) -> tuple[dict[str, str], set[str]]:
    """forecasts/ledger.csv を読み、実データ予想の採点実績を集計する。"""
    ledger = root / "forecasts" / "ledger.csv"
    hits: list[bool] = []
    briers: list[float] = []
    in_ranges: list[bool] = []
    pending = 0
    codes: set[str] = set()
    if ledger.is_file():
        with ledger.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                if (row.get("data") or "").strip().lower() != "real":
                    continue
                code = (row.get("code") or "").strip()
                if code:
                    codes.add(code)
                status = (row.get("status") or "").strip().lower()
                if status == "graded":
                    hits.append(_is_true(row.get("dir_hit", "")))
                    in_ranges.append(_is_true(row.get("in_range", "")))
                    try:
                        briers.append(float(row.get("brier", "")))
                    except (TypeError, ValueError):
                        pass
                elif status == "pending":
                    pending += 1
    graded = len(hits)
    sig = {
        "MAPE_FC_GRADED": str(graded),
        "MAPE_FC_PENDING": str(pending),
        "MAPE_FC_HIT": "na" if graded == 0 else str(round(100 * sum(hits) / graded)),
        "MAPE_FC_BRIER": "na" if not briers else f"{sum(briers) / len(briers):.3f}",
        "MAPE_FC_INRANGE": "na" if not in_ranges else str(round(100 * sum(in_ranges) / len(in_ranges))),
    }
    return sig, codes


def _frontmatter(text: str) -> dict[str, str]:
    """先頭の YAML frontmatter から単純な ``key: value`` を拾う（ネスト値は無視）。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line[:1] in (" ", "\t") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        meta[key.strip()] = val.strip()
    return meta


def journal_signals(root: Path, today: dt.date) -> tuple[dict[str, str], set[str]]:
    """journal/ の実データ仮説を読み、検証実績・検証期日超過・対象銘柄を集計する。"""
    journal = root / "journal"
    total = verified = hit = due = 0
    codes: set[str] = set()
    if journal.is_dir():
        for path in sorted(journal.rglob("*.md")):
            if path.name.lower() == "readme.md":
                continue
            meta = _frontmatter(path.read_text(encoding="utf-8"))
            if not meta.get("review_date"):
                continue
            if meta.get("data", "").strip().strip('"').strip("'").lower() == "synthetic":
                continue
            total += 1
            codes.update(_CODE_RE.findall(meta.get("codes", "")))
            outcome = (meta.get("outcome") or "pending").strip().strip('"').strip("'").lower()
            if outcome in ("hit", "miss", "mixed"):
                verified += 1
                if outcome == "hit":
                    hit += 1
            elif outcome == "pending":
                try:
                    rd = dt.date.fromisoformat(meta["review_date"].strip().strip('"').strip("'"))
                except ValueError:
                    rd = None
                if rd is not None and rd <= today:
                    due += 1
    sig = {
        "MAPE_JR_TOTAL": str(total),
        "MAPE_JR_VERIFIED": str(verified),
        "MAPE_JR_HIT": str(hit),
        "MAPE_JR_DUE": str(due),
    }
    return sig, codes


# --------------------------------------------------------------------------
# 分析カバレッジ（asset growth）
# --------------------------------------------------------------------------
def _universe_codes(root: Path) -> list[str]:
    """analysis/universe/liquid30.csv の code 列（コメント/ヘッダを除く）を返す。"""
    path = root / "analysis" / "universe" / "liquid30.csv"
    codes: list[str] = []
    if not path.is_file():
        return codes
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    for row in csv.DictReader(lines):
        code = (row.get("code") or "").strip()
        if code:
            codes.append(code)
    return codes


def coverage_signals(root: Path, analyzed: set[str], state_dir: Path | None) -> dict[str, str]:
    """ユニバースに対する分析記録の網羅率・未分析銘柄を集計する。"""
    universe = _universe_codes(root)
    uset = set(universe)
    covered = sorted(uset & analyzed)
    unanalyzed = [c for c in universe if c not in analyzed]
    n_uni = len(universe)
    pct = "na" if n_uni == 0 else str(round(100 * len(covered) / n_uni))
    if state_dir is not None:
        (state_dir / "unanalyzed-codes.txt").write_text("\n".join(unanalyzed) + ("\n" if unanalyzed else ""), encoding="utf-8")
    return {
        "MAPE_UNIVERSE": str(n_uni),
        "MAPE_COVERED": str(len(covered)),
        "MAPE_COVERAGE": pct,
        "MAPE_UNANALYZED": str(len(unanalyzed)),
    }


def knowledge_signals(root: Path, today: dt.date, state_dir: Path | None) -> dict[str, str]:
    """ナレッジベースの規模と陳腐化（「20XX年時点」の最新が today.year-2 以下）を集計する。"""
    kdir = root / "knowledge"
    docs = 0
    stale: list[str] = []
    if kdir.is_dir():
        for path in sorted(kdir.rglob("*.md")):
            if path.name == "00-index.md":
                continue
            docs += 1
            years = [int(y) for y in _ASOF_YEAR_RE.findall(path.read_text(encoding="utf-8"))]
            if years and max(years) <= today.year - _STALE_AGE:
                stale.append(path.relative_to(kdir).as_posix())
    if state_dir is not None:
        (state_dir / "stale-docs.txt").write_text("\n".join(stale) + ("\n" if stale else ""), encoding="utf-8")
    return {"MAPE_KNOW_DOCS": str(docs), "MAPE_STALE_DOCS": str(len(stale))}


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parents[1]
    today = dt.date.fromisoformat(argv[2]) if len(argv) > 2 else dt.date.today()
    state_dir = Path(argv[3]) if len(argv) > 3 else None
    if state_dir is not None:
        state_dir.mkdir(parents=True, exist_ok=True)

    out: dict[str, str] = {}
    fc, fc_codes = forecast_signals(root)
    jr, jr_codes = journal_signals(root, today)
    out.update(fc)
    out.update(jr)
    out.update(coverage_signals(root, fc_codes | jr_codes, state_dir))
    out.update(knowledge_signals(root, today, state_dir))
    for key, val in out.items():
        print(f"{key}={val}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

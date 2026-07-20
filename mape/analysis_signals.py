#!/usr/bin/env python3
"""MAPE-K Monitor 用「分析の答え合わせ」シグナル抽出（決定論・stdlib のみ・ネットワーク不使用）。

夜間フォーキャストの実績台帳（``forecasts/ledger.csv``）とリサーチジャーナル（``journal/``）を
読み、**分析が当たっていたか**の track record を ``KEY=VALUE`` 形式で stdout に出力する。
価格データ・pandas・yfinance に一切依存しない（Monitor の「安く読む」フェーズに載せるため）。

使い方:
    python3 mape/analysis_signals.py [<repo_root>] [<today=YYYY-MM-DD>]

出力キー（`mape/monitor.sh` がそのまま monitor.env に取り込む）:
    MAPE_FC_GRADED   採点済み（status=graded, data=real）の予想件数
    MAPE_FC_PENDING  未採点（status=pending, data=real）の予想件数
    MAPE_FC_HIT      方向的中率 %（採点済みの dir_hit 平均、整数。標本 0 なら na）
    MAPE_FC_BRIER    平均 Brier（採点済み、小数3桁。標本 0 なら na）
    MAPE_JR_TOTAL    リサーチジャーナルの実データ仮説数（data!=synthetic）
    MAPE_JR_VERIFIED 検証済み（outcome in hit/miss/mixed）件数
    MAPE_JR_HIT      的中（outcome=hit）件数
    MAPE_JR_DUE      検証期日超過かつ未検証（review_date<=today, outcome=pending）件数
"""

from __future__ import annotations

import csv
import datetime as dt
import sys
from pathlib import Path

_TRUE = {"true", "1", "yes", "y", "t"}


def _is_true(value: str) -> bool:
    return str(value).strip().lower() in _TRUE


def forecast_signals(root: Path) -> dict[str, str]:
    """forecasts/ledger.csv を読み、実データ予想の採点実績を集計する。"""
    ledger = root / "forecasts" / "ledger.csv"
    graded_hits: list[bool] = []
    briers: list[float] = []
    pending = 0
    if ledger.is_file():
        with ledger.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                if (row.get("data") or "").strip().lower() != "real":
                    continue  # 合成予想は実績で採点しない（実市況の偽装防止）
                status = (row.get("status") or "").strip().lower()
                if status == "graded":
                    graded_hits.append(_is_true(row.get("dir_hit", "")))
                    try:
                        briers.append(float(row.get("brier", "")))
                    except (TypeError, ValueError):
                        pass
                elif status == "pending":
                    pending += 1
    graded = len(graded_hits)
    hit = "na" if graded == 0 else str(round(100 * sum(graded_hits) / graded))
    brier = "na" if not briers else f"{sum(briers) / len(briers):.3f}"
    return {
        "MAPE_FC_GRADED": str(graded),
        "MAPE_FC_PENDING": str(pending),
        "MAPE_FC_HIT": hit,
        "MAPE_FC_BRIER": brier,
    }


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
            continue  # ネスト（entry_prices の子要素）や非 key 行は飛ばす
        key, _, val = line.partition(":")
        meta[key.strip()] = val.strip().strip('"').strip("'")
    return meta


def journal_signals(root: Path, today: dt.date) -> dict[str, str]:
    """journal/ の実データ仮説を読み、検証実績と検証期日超過を集計する。"""
    journal = root / "journal"
    total = verified = hit = due = 0
    if journal.is_dir():
        for path in sorted(journal.rglob("*.md")):
            if path.name.lower() == "readme.md":
                continue
            meta = _frontmatter(path.read_text(encoding="utf-8"))
            if not meta.get("review_date"):
                continue  # frontmatter を持つ仮説エントリのみ
            if (meta.get("data") or "").strip().lower() == "synthetic":
                continue  # サンプル/合成は track record に数えない
            total += 1
            outcome = (meta.get("outcome") or "pending").strip().lower()
            if outcome in ("hit", "miss", "mixed"):
                verified += 1
                if outcome == "hit":
                    hit += 1
            elif outcome == "pending":
                try:
                    rd = dt.date.fromisoformat(meta["review_date"])
                except ValueError:
                    rd = None
                if rd is not None and rd <= today:
                    due += 1
    return {
        "MAPE_JR_TOTAL": str(total),
        "MAPE_JR_VERIFIED": str(verified),
        "MAPE_JR_HIT": str(hit),
        "MAPE_JR_DUE": str(due),
    }


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parents[1]
    if len(argv) > 2:
        today = dt.date.fromisoformat(argv[2])
    else:
        today = dt.date.today()
    out: dict[str, str] = {}
    out.update(forecast_signals(root))
    out.update(journal_signals(root, today))
    for key, val in out.items():
        print(f"{key}={val}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

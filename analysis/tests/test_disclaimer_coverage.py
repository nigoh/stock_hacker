"""免責文のカバレッジを全 CLI 横断で守る回帰テスト。

**このテストが存在する理由（実際に起きた抜け）**

免責文は長らく :func:`stocklib.report.save_report` の *内部* でのみ追記されており、
各 CLI が ``print(content)`` で stdout に出す本文には付いていなかった。
Python の文字列は不変なので、``save_report`` 内で再束縛しても呼び出し側の
``content`` は変わらない——という素直な見落としである。

その結果、**ファイルには免責が付くが stdout には付かない**という状態が
16箇所で生じていた。しかも ``docs/automation.md`` は cron / Routine での定期実行を
推奨しており、その運用では stdout がそのままメール・ログ・通知に流れる。
つまり実運用で最も人目に触れる経路だけが免責なしだった。

PostToolUse フック（``scripts/check_report_disclaimer.py``）は ``reports/*.md`` の
*ファイル* しか検査しないため、この抜けを検出できなかった。ここで stdout を
実際に走らせて検査する。

**不変条件**: レポート本文（Markdown 見出しで始まる塊）を stdout に出す CLI は、
その出力に免責文を含めなければならない。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from stocklib.report import DISCLAIMER

REPO_ROOT = Path(__file__).resolve().parents[2]
ANALYSIS_DIR = REPO_ROOT / "analysis"

# 免責の有無を判定するための短い代表句（DISCLAIMER 全文は改行で折り返されうるため）。
_DISCLAIMER_MARK = "投資助言ではありません"

# --synthetic だけで完結し、ネットワークも利用者 CSV も要らない CLI と最小引数。
# （portfolio_review・income_report・tax_report・performance_report は利用者 CSV が
#  必須なのでここでは扱わない。fundamentals_report は EDINET 依存があるため除外。）
SELF_CONTAINED_CLIS: list[tuple[str, list[str]]] = [
    ("analyze_stock.py", ["7203", "--period", "6mo", "--no-charts"]),
    ("compare.py", ["7203", "6758", "--period", "6mo", "--no-charts"]),
    ("run_backtest.py", ["--strategy", "ma_cross", "--code", "7203", "--no-charts"]),
    ("screen.py", []),
    ("market_breadth.py", []),
    ("relative_strength.py", ["--no-valuation"]),
    ("sector_rotation.py", ["--universe", "analysis/universe/liquid30.csv"]),
    ("risk_report.py", ["7203", "--period", "1y"]),
    ("seasonality_report.py", ["7203", "--period", "5y"]),
    ("pairs_screen.py", ["--top", "3"]),
    ("daily_brief.py", []),
    ("adr_parity.py", ["7203"]),
]


def _run(script: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ANALYSIS_DIR / script), *args, "--synthetic"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=300,
    )


def _looks_like_report_body(stdout: str) -> bool:
    """stdout がレポート本文を含むか（Markdown の見出し行があるか）。

    サマリ1行 + 出力パスだけを出す CLI（``asset_plan.py`` 等）は本文を
    出していないので、免責の要求対象から外すための判定。
    """
    return any(line.startswith("# ") for line in stdout.splitlines())


@pytest.mark.parametrize("script,args", SELF_CONTAINED_CLIS, ids=lambda v: v if isinstance(v, str) else "")
def test_stdout_report_body_includes_disclaimer(script: str, args: list[str]) -> None:
    """本文を stdout に出す CLI は、その出力に免責文を含む。"""
    proc = _run(script, args)
    assert proc.returncode == 0, (
        f"{script} が異常終了しました（exit {proc.returncode}）:\n{proc.stderr[-800:]}"
    )
    if not _looks_like_report_body(proc.stdout):
        pytest.skip(f"{script} はレポート本文を stdout に出さない（サマリのみ）")
    assert _DISCLAIMER_MARK in proc.stdout, (
        f"{script} の stdout にレポート本文が出ているのに免責文がありません。\n"
        f"report.with_disclaimer() を通してから print してください。\n"
        f"stdout 末尾:\n{proc.stdout[-600:]}"
    )


def test_with_disclaimer_is_idempotent() -> None:
    """``with_disclaimer`` は二重付与しない（print と save で2回通っても1つ）。"""
    from stocklib import report

    once = report.with_disclaimer("# タイトル\n\n本文")
    twice = report.with_disclaimer(once)
    assert once == twice
    assert once.count(_DISCLAIMER_MARK) == 1


def test_disclaimer_states_the_required_elements() -> None:
    """免責文が最低限の4要素を述べている（欠落すると意味が痩せるため固定）。"""
    for phrase in (
        "投資助言ではありません",       # 助言でないこと
        "将来の運用成果を保証しません",  # 過去実績が将来を保証しないこと
        "保証しません",                 # データの正確性
        "責任を負いません",             # 損害責任
    ):
        assert phrase in DISCLAIMER, f"免責文から要素が欠落しています: {phrase}"


def test_every_report_generating_cli_routes_stdout_through_with_disclaimer() -> None:
    """静的検査: ``print(content)`` の裸呼び出しが残っていない。

    実行時テストは --synthetic で回せる CLI しか覆えない（利用者 CSV が要る
    CLI は起動できない）。ソース上の呼び出し形からも守っておく。
    """
    offenders: list[str] = []
    for path in sorted(ANALYSIS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "print(content)" in text:
            offenders.append(path.name)
    assert not offenders, (
        "レポート本文を免責なしで stdout に出しています（print(content) の裸呼び出し）: "
        f"{offenders}。report.with_disclaimer(content) を通してください。"
    )

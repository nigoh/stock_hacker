"""MAPE-K 夜間セルフ改善（mape/）の決定論部分を pytest から検証する。

品質ゲート（pytest）が MAPE-K の M/A/P・サーキットブレーカーの回帰も守れるよう、
決定論の自己テスト ``mape/tests/run.sh`` をサブプロセスで実行し、exit 0 を要求する。
bash が無い環境では skip する。ネットワーク不使用。

再帰ガード: ``mape/tests/run.sh`` は先頭で ``MAPE_NO_GATE=1`` を export し、監視スクリプトの
``--with-gate``（= pytest 実行）を抑止するため、pytest → この test → run.sh → monitor →
pytest の無限再帰は起きない。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAPE_TEST = REPO_ROOT / "mape" / "tests" / "run.sh"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash が無い")
def test_mape_deterministic_selftest() -> None:
    """mape/tests/run.sh（M/A/P・ブレーカー・分類の回帰テスト）が緑であること。"""
    assert MAPE_TEST.is_file(), f"{MAPE_TEST} が無い"
    proc = subprocess.run(
        ["bash", str(MAPE_TEST)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    # 失敗時は self-test の出力をそのまま見せる（どの ok/NG かが分かる）
    assert proc.returncode == 0, (
        "mape/tests/run.sh が失敗しました。\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )

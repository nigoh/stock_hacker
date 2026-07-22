"""ユニバース CSV（code,name,sector）の整合性検証（ネットワーク不使用）。

銘柄コードの形式・重複・必須列の欠損を検出する。実在性（Yahoo での解決）は
CI ではネットワーク非依存のため検証しないが、large70.csv は追加時に手動で検証済み。
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest

UNIVERSE_DIR = Path(__file__).resolve().parents[1] / "universe"
# screen.py 互換（code,name,sector）のユニバース。adr_map.csv は別スキーマのため除く。
SCREEN_UNIVERSES = ["liquid30.csv", "large70.csv"]
_CODE_RE = re.compile(r"^[0-9]{4}$|^[0-9]{3}[A-Z]$")  # 4桁数字 or 2024年以降の英字入り


@pytest.mark.parametrize("filename", SCREEN_UNIVERSES)
def test_universe_well_formed(filename: str) -> None:
    path = UNIVERSE_DIR / filename
    assert path.exists(), f"{filename} が見つかりません"
    df = pd.read_csv(path, comment="#", dtype=str)
    assert list(df.columns[:3]) == ["code", "name", "sector"], f"{filename} の列見出し"
    codes = df["code"].str.strip()
    # コード形式
    for code in codes:
        assert _CODE_RE.match(code), f"{filename}: 不正なコード形式 {code!r}"
    # 重複なし
    assert codes.is_unique, f"{filename}: コードが重複しています"
    # name / sector が空でない
    assert df["name"].str.strip().ne("").all(), f"{filename}: 空の name があります"
    assert df["sector"].str.strip().ne("").all(), f"{filename}: 空の sector があります"


def test_large70_size_and_superset_of_core() -> None:
    df = pd.read_csv(UNIVERSE_DIR / "large70.csv", comment="#", dtype=str)
    assert len(df) >= 60  # 大型株ユニバース（69銘柄想定）
    # liquid30 の主要銘柄が large70 にも含まれる（代表的な数銘柄で確認）
    codes = set(df["code"].str.strip())
    for core in ("7203", "6758", "9984", "8306", "6861"):
        assert core in codes, f"large70 に主要銘柄 {core} が含まれていません"


def test_sectors_are_plausible() -> None:
    df = pd.read_csv(UNIVERSE_DIR / "large70.csv", comment="#", dtype=str)
    # 複数セクターにまたがる（1業種に偏っていない）
    assert df["sector"].nunique() >= 10

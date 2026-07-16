"""Markdown レポート生成ヘルパー。

テーブル整形・数値フォーマット・免責文の定数・``reports/`` への保存関数を提供する。
外部依存なし（tabulate 不要）。
"""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path
from typing import Sequence

import pandas as pd

from stocklib.data import REPO_ROOT

REPORTS_DIR: Path = REPO_ROOT / "reports"

DISCLAIMER: str = (
    "> **免責事項**: 本レポートは情報の整理・分析支援を目的として自動生成されたものであり、"
    "特定の金融商品の売買を推奨する投資助言ではありません。"
    "投資に関する最終判断はご自身の責任で行ってください。"
)


def fmt_num(value: object, digits: int = 2) -> str:
    """数値を桁区切り付き文字列に整形する。NaN・None は ``-``。"""
    if value is None:
        return "-"
    if isinstance(value, float) and math.isnan(value):
        return "-"
    if isinstance(value, (int,)) and not isinstance(value, bool):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return str(value)


def fmt_pct(value: object, digits: int = 2) -> str:
    """比率（0.05 = 5%）をパーセント表記に整形する。NaN・None は ``-``。"""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value * 100:.{digits}f}%"
    return str(value)


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """ヘッダーと行データから Markdown テーブル文字列を生成する。

    セルは :func:`fmt_num` で整形済みの文字列を渡すか、そのまま ``str()`` される。
    """
    header_line = "| " + " | ".join(str(h) for h in headers) + " |"
    sep_line = "| " + " | ".join("---" for _ in headers) + " |"
    body_lines = [
        "| " + " | ".join(str(c) if c is not None else "-" for c in row) + " |"
        for row in rows
    ]
    return "\n".join([header_line, sep_line, *body_lines])


def df_to_markdown(df: pd.DataFrame, digits: int = 2, index_name: str = "") -> str:
    """DataFrame を Markdown テーブルに変換する（インデックス列付き）。"""
    headers = [index_name, *[str(c) for c in df.columns]]
    rows = [
        [str(idx), *[fmt_num(v, digits) for v in row]]
        for idx, row in zip(df.index, df.to_numpy())
    ]
    return markdown_table(headers, rows)


def report_header(title: str) -> str:
    """タイトルと生成日時を含むレポート冒頭部を生成する。"""
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"# {title}\n\n生成日時: {now}\n"


def save_report(content: str, filename: str) -> Path:
    """レポートを ``reports/`` 配下に UTF-8 で保存し、絶対パスを返す。

    免責文（:data:`DISCLAIMER`）が含まれていない場合は末尾に自動追記する。
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    if DISCLAIMER not in content:
        content = content.rstrip() + "\n\n---\n\n" + DISCLAIMER + "\n"
    path = REPORTS_DIR / filename
    path.write_text(content, encoding="utf-8")
    return path

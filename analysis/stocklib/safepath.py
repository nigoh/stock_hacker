"""出力パスの封じ込め（パストラバーサル防止）ヘルパー。

**なぜこれがあるのか（防いでいる事故）**: 本リポジトリの出力ファイル名には
ユーザー入力がそのまま混ざる。``analyze_stock.py`` の ``--code``、
``research_journal.py`` の ``--slug``、``risk_report.py`` / ``compare.py`` の
銘柄コードなどは、``f"analyze-{args.code}-{日付}.md"`` のように
**検証されないまま連結**されてファイル名・チャート名・エントリ ID になる。
``--code ../../../../etc/cron.d/x`` のような値を渡すと、出力先ディレクトリの
外（リポジトリ外・システム領域）へ書き込めてしまう——セキュリティ監査で
``stocklib.report.save_report`` に実際に見つかった欠陥がこれで、同型の書き込み
経路が ``charts`` / ``journal`` にも残っていた。

方針は「**ファイル名からディレクトリ成分を捨て、意図した出力ディレクトリの中に
封じ込める**」こと。パスの正規化（``..`` の解決）に頼った後付けの判定ではなく、
先にベース名だけを取り出してから join するため、``..``・絶対パス・
複数階層のいずれでも出力先の外には出られない。

利用側:
    - :func:`stocklib.report.save_report` → ``reports/``
    - :func:`stocklib.charts.img_path` → ``reports/img/``
    - :func:`stocklib.journal.save_entry` → ``journal/<YYYY>/``
    - :func:`stocklib.forecast.save_ledger` → 封じ込めはせず :func:`safe_name` のみ
      （``--ledger`` で任意パスを指定するのが正当な機能のため）
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["safe_name", "contained_path"]


def safe_name(filename: str | Path, *, what: str = "ファイル名") -> str:
    """``filename`` からディレクトリ成分を捨てたベース名を返す。

    空文字・``.``・``..``・先頭ドット（隠しファイル）・NUL 文字を含む名前は
    :class:`ValueError` で拒否する。``a/b.md`` → ``b.md``、``../../x.md`` → ``x.md``、
    ``/etc/passwd`` → ``passwd`` のように、ディレクトリ成分は常に捨てられる。

    Args:
        filename: 検査するファイル名（ディレクトリ成分を含んでいてもよい）。
        what: エラーメッセージに使う名称（例 ``"レポートファイル名"``）。

    Returns:
        安全なベース名。

    Raises:
        ValueError: ベース名が空・``.``・``..``・先頭ドット・NUL 文字を含む場合。
    """
    raw = str(filename)
    name = Path(raw).name
    if not name or name in (".", "..") or name.startswith(".") or "\x00" in name:
        raise ValueError(f"不正な{what}: {filename!r}")
    return name


def contained_path(
    base_dir: Path,
    filename: str | Path,
    *,
    what: str = "ファイル名",
    where: str | None = None,
) -> Path:
    """``base_dir`` の中に封じ込めた出力パス（絶対パス）を返す。

    ``filename`` は :func:`safe_name` でベース名のみに切り詰められるため、
    ``..`` や絶対パスが混ざっていても ``base_dir`` の外は指さない。
    join 後にも :meth:`Path.is_relative_to` で再確認する（シンボリックリンク等に
    対する多重防御）。ディレクトリの作成は行わない（呼び出し側の責務）。

    Args:
        base_dir: 意図した出力ディレクトリ。
        filename: 出力ファイル名（ユーザー入力を含みうる）。
        what: エラーメッセージに使う名称。
        where: 封じ込め先の表示名（例 ``"reports/"``）。省略時は ``base_dir``。

    Returns:
        ``base_dir`` 配下に解決された絶対パス。

    Raises:
        ValueError: ファイル名が不正、または解決結果が ``base_dir`` の外を指す場合。
    """
    base = Path(base_dir).resolve()
    name = safe_name(filename, what=what)
    path = (base / name).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"{where or base_dir} の外には書き込めません: {filename!r}")
    return path

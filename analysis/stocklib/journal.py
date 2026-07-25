"""リサーチジャーナル（分析仮説の記録と事後検証）モジュール。

「分析のやりっぱなし」をなくすため、分析・仮説を ``journal/<YYYY>/<日付>-<slug>.md``
に記録し、検証予定日（``review_date``）が来たら記録時点の終値スナップショットと
現在値・同期間のベンチマーク（既定 ^N225）騰落を比較して仮説の当たり外れ
（hit / miss / mixed）を機械的に判定する。

エントリは YAML frontmatter + Markdown 本文（``## 仮説`` / ``## 根拠`` /
``## 反証条件`` / ``## 検証結果``）。frontmatter は PyYAML 非依存の自前パーサ
（:func:`parse_frontmatter` / :func:`dump_frontmatter`）で読み書きする。
対応する構文はこのモジュールが生成する範囲（スカラー・フロー形式リスト・
1段ネストのマッピング）に限定した安全なサブセット。

frontmatter の ``data: synthetic|real`` はスナップショット価格のデータ出所を示す
機械可読な印（既存エントリ互換のため欠落時は ``real`` 扱い）。:func:`verify_entry`
はエントリの ``data`` と検証時の ``--synthetic`` 指定が食い違うと :class:`JournalError`
で拒否する——合成スナップショットと実データ（またはその逆）の比較は
hit/miss 判定として無意味なため。

判定ロジックの正は :func:`judge_direction`（docstring 参照）。
CLI は ``analysis/research_journal.py``。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from stocklib.data import REPO_ROOT, fetch_prices
from stocklib.safepath import contained_path, safe_name

JOURNAL_DIR: Path = REPO_ROOT / "journal"

DEFAULT_BENCHMARK: str = "^N225"

VALID_DIRECTIONS: tuple[str, ...] = ("up", "down", "neutral")
VALID_STATUSES: tuple[str, ...] = ("open", "reviewed")
VALID_OUTCOMES: tuple[str, ...] = ("pending", "hit", "miss", "mixed")
VALID_DATA_SOURCES: tuple[str, ...] = ("real", "synthetic")

# ベンチマーク調整後リターンの絶対値がこの閾値未満なら「有意な動きなし」とみなす
MIXED_THRESHOLD: float = 0.02

# 本文テンプレートのセクション見出し
DISCLAIMER: str = (
    "> **免責**: 本エントリは分析仮説の記録と事後検証を目的とした個人的なメモであり、"
    "投資助言ではありません。記載の方向・水準は検証対象の仮説であって、"
    "将来の騰落の予測でも断定でもなく、特定の銘柄の売買を推奨するものではありません。"
)

SECTION_HYPOTHESIS = "## 仮説"
SECTION_RATIONALE = "## 根拠"
SECTION_FALSIFICATION = "## 反証条件"
SECTION_RESULT = "## 検証結果"

_RESULT_PLACEHOLDER = (
    "（未検証。検証予定日以降に `python3 analysis/research_journal.py verify <このファイル>` "
    "を実行すると判定結果が追記される）"
)

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
# ## 検証結果 セクション（次の ## 見出し or 末尾まで）
_RESULT_SECTION_RE = re.compile(
    rf"^{re.escape(SECTION_RESULT)}\s*\n.*?(?=^## |\Z)", re.DOTALL | re.MULTILINE
)


class JournalError(RuntimeError):
    """ジャーナルエントリの解析・検証に失敗したことを示す例外。"""


# ---------------------------------------------------------------------------
# frontmatter の読み書き（PyYAML 非依存の安全なサブセット）
# ---------------------------------------------------------------------------


def _parse_scalar(raw: str) -> object:
    """frontmatter のスカラー値をパースする（引用符付きは常に文字列）。"""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    if raw in ("null", "~", ""):
        return None
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _parse_flow_list(raw: str) -> list[object]:
    """``[a, b, c]`` 形式のフロー形式リストをパースする。"""
    inner = raw.strip()[1:-1].strip()
    if not inner:
        return []
    return [_parse_scalar(item) for item in inner.split(",")]


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Markdown 文字列を frontmatter 辞書と本文に分離する。

    対応構文（このモジュールが生成するサブセット）:

    - ``key: value`` — スカラー（引用符付きは文字列、それ以外は int/float/bool を推定）
    - ``key: [a, b]`` — フロー形式リスト
    - ``key:`` + インデント行 ``  subkey: value`` — 1段ネストのマッピング

    Returns:
        ``(meta, body)``。frontmatter が無い場合は ``({}, text)``。

    Raises:
        JournalError: frontmatter 内に解釈できない行があった場合。
    """
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        return {}, text
    meta: dict[str, object] = {}
    body = text[m.end():]
    lines = m.group(1).split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            raise JournalError(f"frontmatter の解釈に失敗しました（不正なインデント）: {line!r}")
        if ":" not in line:
            raise JournalError(f"frontmatter の解釈に失敗しました（'key: value' 形式でない）: {line!r}")
        key, _, raw = line.partition(":")
        key = key.strip()
        raw = raw.strip()
        if raw.startswith("["):
            meta[key] = _parse_flow_list(raw)
        elif raw:
            meta[key] = _parse_scalar(raw)
        else:
            # 値なし → 続くインデント行を1段ネストのマッピングとして読む
            nested: dict[str, object] = {}
            while i < len(lines) and lines[i].startswith("  ") and lines[i].strip():
                sub = lines[i].strip()
                i += 1
                if ":" not in sub:
                    raise JournalError(
                        f"frontmatter の解釈に失敗しました（ネスト行が 'key: value' 形式でない）: {sub!r}"
                    )
                sub_key, _, sub_raw = sub.partition(":")
                nested[str(_parse_scalar(sub_key))] = _parse_scalar(sub_raw)
            meta[key] = nested if nested else None
    return meta, body


def _dump_scalar(value: object) -> str:
    """スカラー値を frontmatter 用の文字列に整形する（往復可能性を担保）。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    s = str(value)
    # 数値・真偽値・空文字などに誤解釈されうる文字列は引用符で囲む
    needs_quote = (
        s == ""
        or s != s.strip()
        or s in ("null", "~", "true", "false")
        or not isinstance(_parse_scalar(s), str)
        or any(c in s for c in ":#[]{}\"'\n")
    )
    if needs_quote:
        return '"' + s.replace('"', "'") + '"'
    return s


def dump_frontmatter(meta: Mapping[str, object]) -> str:
    """frontmatter 辞書を ``---`` 区切りの文字列に整形する（:func:`parse_frontmatter` と往復可能）。"""
    lines: list[str] = ["---"]
    for key, value in meta.items():
        if isinstance(value, Mapping):
            lines.append(f"{key}:")
            for sub_key, sub_value in value.items():
                lines.append(f"  {_dump_scalar(sub_key)}: {_dump_scalar(sub_value)}")
        elif isinstance(value, (list, tuple)):
            inner = ", ".join(_dump_scalar(v) for v in value)
            lines.append(f"{key}: [{inner}]")
        else:
            lines.append(f"{key}: {_dump_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# エントリのデータモデル
# ---------------------------------------------------------------------------


@dataclass
class JournalEntry:
    """リサーチジャーナルの1エントリ（1仮説）。

    ``direction`` は銘柄コード → ``"up" / "down" / "neutral"`` のマッピング。
    全銘柄で同一方向のときは frontmatter にはスカラー（``direction: up``）で
    書かれるが、内部表現は常にマッピングに正規化する。

    ``data`` はスナップショット価格のデータ出所（``"real"`` / ``"synthetic"``）。
    frontmatter に無い既存エントリは ``real`` として読み込む。
    """

    id: str
    date: dt.date
    title: str
    codes: list[str]
    direction: dict[str, str]
    review_date: dt.date
    status: str = "open"
    outcome: str = "pending"
    data: str = "real"
    entry_prices: dict[str, float] = field(default_factory=dict)
    benchmark: str = DEFAULT_BENCHMARK
    benchmark_entry: float | None = None
    verified_date: dt.date | None = None
    body: str = ""
    path: Path | None = None
    extra: dict[str, object] = field(default_factory=dict)

    def validate(self) -> None:
        """フィールド値の整合性を検査し、不正なら :class:`JournalError` を送出する。"""
        if not self.codes:
            raise JournalError("codes が空です（対象銘柄を1つ以上指定してください）")
        if self.status not in VALID_STATUSES:
            raise JournalError(f"status が不正です: {self.status!r}（{VALID_STATUSES} のいずれか）")
        if self.outcome not in VALID_OUTCOMES:
            raise JournalError(f"outcome が不正です: {self.outcome!r}（{VALID_OUTCOMES} のいずれか）")
        if self.data not in VALID_DATA_SOURCES:
            raise JournalError(f"data が不正です: {self.data!r}（{VALID_DATA_SOURCES} のいずれか）")
        for code in self.codes:
            d = self.direction.get(code)
            if d not in VALID_DIRECTIONS:
                raise JournalError(
                    f"銘柄 {code} の direction が不正です: {d!r}（{VALID_DIRECTIONS} のいずれか）"
                )


@dataclass
class Verdict:
    """1銘柄分の検証結果（判定の内訳）。"""

    code: str
    direction: str
    entry_price: float
    current_price: float
    stock_return: float
    benchmark_return: float
    excess_return: float
    result: str  # "hit" / "miss" / "mixed"


# ---------------------------------------------------------------------------
# 変換（dict ⇔ dataclass ⇔ Markdown）
# ---------------------------------------------------------------------------

_KNOWN_KEYS: tuple[str, ...] = (
    "id", "date", "title", "codes", "direction", "review_date",
    "status", "outcome", "data", "benchmark", "benchmark_entry", "entry_prices",
    "verified_date",
)


def _to_date(value: object, key: str) -> dt.date:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise JournalError(f"{key} を日付（YYYY-MM-DD）として解釈できません: {value!r}") from exc


def entry_from_parts(meta: Mapping[str, object], body: str, path: Path | None = None) -> JournalEntry:
    """frontmatter 辞書と本文から :class:`JournalEntry` を構築する。"""
    try:
        codes = [str(c) for c in meta["codes"]]  # type: ignore[index]
        raw_direction = meta["direction"]
        entry = JournalEntry(
            id=str(meta["id"]),
            date=_to_date(meta["date"], "date"),
            title=str(meta["title"]),
            codes=codes,
            direction={},
            review_date=_to_date(meta["review_date"], "review_date"),
            status=str(meta.get("status", "open")),
            outcome=str(meta.get("outcome", "pending")),
            # 既存エントリ互換: data キーが無い（このフィールド導入前の）エントリは real 扱い
            data=str(meta.get("data", "real")),
            benchmark=str(meta.get("benchmark", DEFAULT_BENCHMARK)),
            body=body,
            path=path,
        )
    except KeyError as exc:
        raise JournalError(f"frontmatter に必須キーがありません: {exc}") from exc
    # direction: スカラー（全銘柄共通）またはマッピング（銘柄ごと）
    if isinstance(raw_direction, Mapping):
        entry.direction = {str(k): str(v) for k, v in raw_direction.items()}
    else:
        entry.direction = {code: str(raw_direction) for code in codes}
    raw_prices = meta.get("entry_prices") or {}
    if not isinstance(raw_prices, Mapping):
        raise JournalError(f"entry_prices はマッピングである必要があります: {raw_prices!r}")
    entry.entry_prices = {str(k): float(v) for k, v in raw_prices.items()}  # type: ignore[arg-type]
    bench_entry = meta.get("benchmark_entry")
    entry.benchmark_entry = float(bench_entry) if bench_entry is not None else None  # type: ignore[arg-type]
    verified = meta.get("verified_date")
    entry.verified_date = _to_date(verified, "verified_date") if verified is not None else None
    entry.extra = {k: v for k, v in meta.items() if k not in _KNOWN_KEYS}
    entry.validate()
    return entry


def entry_to_markdown(entry: JournalEntry) -> str:
    """:class:`JournalEntry` を frontmatter + 本文の Markdown 文字列に変換する。"""
    directions = set(entry.direction.values())
    direction_out: object
    if len(directions) == 1:
        direction_out = next(iter(directions))
    else:
        direction_out = {code: entry.direction[code] for code in entry.codes}
    meta: dict[str, object] = {
        "id": entry.id,
        "date": entry.date,
        "title": entry.title,
        "codes": list(entry.codes),
        "direction": direction_out,
        "review_date": entry.review_date,
        "status": entry.status,
        "outcome": entry.outcome,
        "data": entry.data,
        "benchmark": entry.benchmark,
        "benchmark_entry": entry.benchmark_entry,
        "entry_prices": dict(entry.entry_prices),
    }
    if entry.verified_date is not None:
        meta["verified_date"] = entry.verified_date
    meta.update(entry.extra)
    return dump_frontmatter(meta) + "\n" + entry.body.strip() + "\n"


def load_entry(path: Path) -> JournalEntry:
    """ファイルからエントリを読み込む。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JournalError(f"エントリを読み込めません: {path}: {exc}") from exc
    meta, body = parse_frontmatter(text)
    if not meta:
        raise JournalError(f"frontmatter がありません（'---' で始まっていません）: {path}")
    return entry_from_parts(meta, body, path=path)


def save_entry(entry: JournalEntry, journal_dir: Path = JOURNAL_DIR) -> Path:
    """エントリを ``<journal_dir>/<YYYY>/<id>.md`` に保存し、パスを返す。

    ``entry.path`` が設定済みならそのパスに上書き保存する（``verify`` のように
    ユーザーが指定した既存ファイルへ書き戻す経路があるため、出力先ディレクトリは
    呼び出し側の指定に従う）。ただし **``entry.id`` にはユーザー入力の
    ``--slug`` が入り**、``--slug ../../../etc/x`` のような値ではファイル名に
    ディレクトリ成分が紛れ込むため、パスを組み立てる際は
    :func:`stocklib.safepath.contained_path` で ``<journal_dir>/<YYYY>/`` の中に
    封じ込め、既存パスに書き戻す場合もファイル名の健全性を検査する
    （:mod:`stocklib.safepath` の説明を参照）。

    Raises:
        ValueError: ファイル名が不正、または年ディレクトリの外を指す場合。
    """
    entry.validate()
    if entry.path is None:
        entry.path = contained_path(
            journal_dir / f"{entry.date.year}",
            f"{entry.id}.md",
            what="ジャーナルのファイル名",
            where=f"journal/{entry.date.year}/",
        )
    else:
        safe_name(entry.path.name, what="ジャーナルのファイル名")
    entry.path.parent.mkdir(parents=True, exist_ok=True)
    entry.path.write_text(entry_to_markdown(entry), encoding="utf-8")
    return entry.path


# ---------------------------------------------------------------------------
# 新規エントリ作成
# ---------------------------------------------------------------------------


def make_slug(title: str, codes: Iterable[str]) -> str:
    """タイトルから ASCII スラッグを作る。日本語のみ等で空になる場合は銘柄コードで代替する。"""
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", normalized).strip("-").lower()
    if not slug:
        slug = "-".join(str(c) for c in codes) or "entry"
    return slug[:60].strip("-")


def normalize_direction(direction_arg: str, codes: list[str]) -> dict[str, str]:
    """CLI の ``--direction`` 引数を銘柄コード → 方向のマッピングに正規化する。

    - ``"up"`` のような単一指定 → 全銘柄に適用
    - ``"7203:up,6758:down"`` のようなカンマ区切り → 銘柄ごとに指定
      （全銘柄分を指定する必要がある）
    """
    direction_arg = direction_arg.strip()
    if ":" not in direction_arg:
        if direction_arg not in VALID_DIRECTIONS:
            raise JournalError(
                f"direction が不正です: {direction_arg!r}（{VALID_DIRECTIONS} のいずれか、"
                "または '7203:up,6758:down' 形式）"
            )
        return {code: direction_arg for code in codes}
    mapping: dict[str, str] = {}
    for part in direction_arg.split(","):
        code, _, d = part.strip().partition(":")
        code, d = code.strip(), d.strip()
        if d not in VALID_DIRECTIONS:
            raise JournalError(f"銘柄 {code} の direction が不正です: {d!r}")
        mapping[code] = d
    missing = [c for c in codes if c not in mapping]
    if missing:
        raise JournalError(f"direction が指定されていない銘柄があります: {missing}")
    unknown = [c for c in mapping if c not in codes]
    if unknown:
        raise JournalError(f"--codes に無い銘柄が direction に指定されています: {unknown}")
    return mapping


def _last_close(code: str, *, synthetic: bool) -> float:
    """直近終値を取得する（スナップショット用）。"""
    df = fetch_prices(code, period="3mo", synthetic=synthetic)[code]
    return float(df["Close"].iloc[-1])


def new_entry(
    codes: list[str],
    title: str,
    direction: str,
    review_days: int,
    *,
    synthetic: bool = False,
    benchmark: str = DEFAULT_BENCHMARK,
    slug: str | None = None,
    today: dt.date | None = None,
    journal_dir: Path = JOURNAL_DIR,
) -> JournalEntry:
    """新規エントリの雛形を生成して保存する。

    記録時点の各銘柄終値とベンチマーク終値を frontmatter にスナップショットする
    （検証時にこのスナップショットと比較する）。データ出所は機械可読な
    ``data: synthetic|real`` として frontmatter に書き込まれ、:func:`verify_entry`
    が検証時の指定との整合を強制する。本文はテンプレート
    （## 仮説 / ## 根拠 / ## 反証条件 / ## 検証結果）で、仮説・根拠・反証条件は
    エントリ作成後に必ず記入する。
    """
    if review_days < 0:
        raise JournalError("review_days は 0 以上を指定してください")
    today = today or dt.date.today()
    direction_map = normalize_direction(direction, codes)
    entry_prices = {code: round(_last_close(code, synthetic=synthetic), 4) for code in codes}
    benchmark_entry = round(_last_close(benchmark, synthetic=synthetic), 4)

    slug = slug or make_slug(title, codes)
    # ``--slug`` はユーザー入力がそのままエントリ ID とファイル名になる。
    # ディレクトリ成分（``/``・``..``・絶対パス）が混ざると journal/<YYYY>/ の
    # 外にエントリを書けてしまうため、ここで拒否する（:mod:`stocklib.safepath`）。
    # frontmatter の id とファイル名を一致させたいので、無害化ではなく拒否する。
    if slug.strip() in ("", ".", "..") or slug != Path(slug).name or "\x00" in slug:
        raise JournalError(
            f"不正な slug: {slug!r}（ディレクトリ成分を含まない名前を指定してください）"
        )
    entry_id = f"{today.isoformat()}-{slug}"
    body_lines = [
        # journal/ は git 管理対象で GitHub 上に公開される。銘柄コード + 方向 +
        # 仮説本文という並びは形式上もっとも「助言」に見えやすいため、
        # ディレクトリの README ではなく各エントリ単体に免責を持たせる。
        DISCLAIMER,
        "",
        SECTION_HYPOTHESIS,
        "",
        "（何がどうなると考えるか。検証可能な形で書く）",
        "",
        SECTION_RATIONALE,
        "",
        "（データ・分析レポート・ナレッジ文書など、仮説の根拠。reports/ のパスを引用する）",
        "",
        SECTION_FALSIFICATION,
        "",
        "（何が起きたらこの仮説を捨てるか。反証条件を必ず書く）",
        "",
        SECTION_RESULT,
        "",
        _RESULT_PLACEHOLDER,
    ]
    if synthetic:
        body_lines.insert(0, "> 注: entry_prices は合成データ（--synthetic）によるスナップショットであり実際の株価ではない。\n")
    entry = JournalEntry(
        id=entry_id,
        date=today,
        title=title,
        codes=list(codes),
        direction=direction_map,
        review_date=today + dt.timedelta(days=review_days),
        status="open",
        outcome="pending",
        data="synthetic" if synthetic else "real",
        entry_prices=entry_prices,
        benchmark=benchmark,
        benchmark_entry=benchmark_entry,
        body="\n".join(body_lines),
    )
    path = entry.path = contained_path(
        journal_dir / f"{today.year}",
        f"{entry_id}.md",
        what="ジャーナルのファイル名",
        where=f"journal/{today.year}/",
    )
    if path.exists():
        raise JournalError(f"同名のエントリが既に存在します: {path}（--slug で別名を指定してください）")
    save_entry(entry, journal_dir)
    return entry


# ---------------------------------------------------------------------------
# 一覧・期日
# ---------------------------------------------------------------------------


def iter_entries(journal_dir: Path = JOURNAL_DIR) -> list[JournalEntry]:
    """ジャーナル配下の全エントリを日付昇順で返す。

    frontmatter を持たないファイル（README 等の非エントリ）だけを黙ってスキップする。
    frontmatter があるのに壊れているファイルは stderr に警告を出してスキップする
    （壊れたエントリが due/list から無言で消えて検証ループが静かに切れるのを防ぐ）。
    """
    entries: list[JournalEntry] = []
    if not journal_dir.exists():
        return entries
    for path in sorted(journal_dir.glob("*/*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"警告: ジャーナルエントリを読み込めません（スキップ）: {path}: {exc}", file=sys.stderr)
            continue
        if _FRONTMATTER_RE.match(text) is None:
            continue  # frontmatter を持たないファイル（非エントリ）は黙って無視
        try:
            meta, body = parse_frontmatter(text)
            entries.append(entry_from_parts(meta, body, path=path))
        except JournalError as exc:
            print(f"警告: 壊れたジャーナルエントリをスキップします: {path}: {exc}", file=sys.stderr)
            continue
    entries.sort(key=lambda e: (e.date, e.id))
    return entries


def due_entries(
    entries: Iterable[JournalEntry], today: dt.date | None = None
) -> list[JournalEntry]:
    """``status: open`` かつ ``review_date <= today`` のエントリを返す（当日を含む）。"""
    today = today or dt.date.today()
    return [e for e in entries if e.status == "open" and e.review_date <= today]


# ---------------------------------------------------------------------------
# 検証（verify）
# ---------------------------------------------------------------------------


def judge_direction(direction: str, excess_return: float) -> str:
    """ベンチマーク調整後リターンから仮説方向の当たり外れを判定する。

    判定ロジック（閾値 :data:`MIXED_THRESHOLD` = ±2%）:

    - ``excess_return`` = 銘柄騰落率 − 同期間のベンチマーク（^N225）騰落率
    - ``|excess_return| < 0.02``（±2%未満）→ 有意な動きなしとみなす:
      ``up`` / ``down`` は **mixed**、``neutral`` は「動かない」仮説の的中で **hit**
    - ``|excess_return| >= 0.02`` → 符号と direction を突き合わせる:
      ``up`` は正なら **hit**・負なら **miss**、``down`` は負なら **hit**・正なら **miss**、
      ``neutral`` は（±2%以上動いたので）**miss**

    ベンチマーク対比にするのは「地合いで全体が上がっただけ」の仮説を
    的中扱いしないため（個別仮説の検証には市場要因を除く）。
    """
    if direction not in VALID_DIRECTIONS:
        raise JournalError(f"direction が不正です: {direction!r}")
    if abs(excess_return) < MIXED_THRESHOLD:
        return "hit" if direction == "neutral" else "mixed"
    if direction == "neutral":
        return "miss"
    sign_up = excess_return > 0
    return "hit" if (direction == "up") == sign_up else "miss"


def aggregate_outcome(results: Iterable[str]) -> str:
    """銘柄ごとの判定を総合判定に集約する（全 hit → hit、全 miss → miss、他は mixed）。"""
    results = list(results)
    if not results:
        raise JournalError("判定結果が空です")
    if all(r == "hit" for r in results):
        return "hit"
    if all(r == "miss" for r in results):
        return "miss"
    return "mixed"


def _fmt_pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def verify_entry(
    entry: JournalEntry,
    *,
    synthetic: bool = False,
    today: dt.date | None = None,
    current_prices: Mapping[str, float] | None = None,
    benchmark_price: float | None = None,
) -> list[Verdict]:
    """エントリを検証し、判定結果を本文 ``## 検証結果`` に追記・frontmatter を更新する。

    記録時スナップショット（``entry_prices`` / ``benchmark_entry``）と現在値から

    - 銘柄騰落率 $r_i = P_{\\text{now}} / P_{\\text{entry}} - 1$
    - ベンチマーク騰落率 $r_b$（同期間の ^N225）
    - 超過リターン $r_i - r_b$

    を計算し、:func:`judge_direction` で銘柄ごとに hit/miss/mixed を判定、
    :func:`aggregate_outcome` で総合 outcome を決める。frontmatter は
    ``status: reviewed`` / ``outcome: <総合判定>`` / ``verified_date: <今日>`` に更新し、
    ファイルを上書き保存する（``entry.path`` が必要）。

    エントリのデータ出所（frontmatter の ``data: synthetic|real``、欠落時は real）と
    ``synthetic`` 引数が食い違う場合は :class:`JournalError` で拒否する——
    合成スナップショットと実データ（またはその逆）の比較は判定として無意味なため。
    ``current_prices`` / ``benchmark_price`` を渡して価格取得を完全にスキップする場合
    （テスト・手動検証用）はこの整合チェックを行わない（従来どおり）。
    """
    if entry.status == "reviewed":
        raise JournalError(f"既に検証済みのエントリです: {entry.path}（outcome: {entry.outcome}）")
    if entry.benchmark_entry is None:
        raise JournalError("benchmark_entry が記録されていないため検証できません")
    # データ出所の整合チェック: 価格取得が1件でも発生する場合のみ強制する
    needs_fetch = benchmark_price is None or current_prices is None or any(
        code not in current_prices for code in entry.codes
    )
    requested = "synthetic" if synthetic else "real"
    if needs_fetch and entry.data != requested:
        raise JournalError(
            f"エントリのデータ出所（data: {entry.data}）と検証時の指定"
            f"（{'--synthetic あり' if synthetic else '--synthetic なし＝実データ'}）が一致しません: "
            f"{entry.path or entry.id}。"
            "合成スナップショットと実データ（またはその逆）の比較は hit/miss 判定として無意味です。"
            "data: synthetic のエントリは --synthetic を付けて、"
            "data: real のエントリは --synthetic なしで verify してください。"
        )
    today = today or dt.date.today()

    if benchmark_price is None:
        benchmark_price = _last_close(entry.benchmark, synthetic=synthetic)
    benchmark_return = benchmark_price / entry.benchmark_entry - 1.0

    verdicts: list[Verdict] = []
    for code in entry.codes:
        if code not in entry.entry_prices:
            raise JournalError(f"銘柄 {code} の entry_prices が記録されていません")
        entry_price = entry.entry_prices[code]
        if current_prices is not None and code in current_prices:
            current = float(current_prices[code])
        else:
            current = _last_close(code, synthetic=synthetic)
        stock_return = current / entry_price - 1.0
        excess = stock_return - benchmark_return
        verdicts.append(
            Verdict(
                code=code,
                direction=entry.direction[code],
                entry_price=entry_price,
                current_price=current,
                stock_return=stock_return,
                benchmark_return=benchmark_return,
                excess_return=excess,
                result=judge_direction(entry.direction[code], excess),
            )
        )

    outcome = aggregate_outcome(v.result for v in verdicts)
    elapsed = (today - entry.date).days

    lines = [
        SECTION_RESULT,
        "",
        f"検証日: {today.isoformat()}（記録から{elapsed}日後、予定: {entry.review_date.isoformat()}）",
        "",
        "| 銘柄 | 方向 | 記録時終値 | 検証時終値 | 騰落率 | ベンチ騰落率 | 超過リターン | 判定 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for v in verdicts:
        lines.append(
            f"| {v.code} | {v.direction} | {v.entry_price:,.2f} | {v.current_price:,.2f} "
            f"| {_fmt_pct(v.stock_return)} | {_fmt_pct(v.benchmark_return)} "
            f"| {_fmt_pct(v.excess_return)} | **{v.result}** |"
        )
    lines += [
        "",
        f"総合判定: **{outcome}**",
        "",
        f"判定基準: 超過リターン（銘柄騰落率 − 同期間の {entry.benchmark} 騰落率）の符号と "
        f"direction の一致で hit/miss、絶対値 {MIXED_THRESHOLD:.0%} 未満は mixed"
        "（neutral は ±2%未満で hit、以上で miss）。"
        "詳細は `analysis/stocklib/journal.py` の `judge_direction` docstring を参照。",
    ]
    if synthetic:
        lines += ["", "> 注: 検証時価格は合成データ（--synthetic）であり実際の株価ではない。"]
    result_section = "\n".join(lines) + "\n"

    if _RESULT_SECTION_RE.search(entry.body):
        entry.body = _RESULT_SECTION_RE.sub(lambda _: result_section, entry.body)
    else:
        entry.body = entry.body.rstrip() + "\n\n" + result_section

    entry.status = "reviewed"
    entry.outcome = outcome
    entry.verified_date = today
    if entry.path is not None:
        save_entry(entry)
    return verdicts

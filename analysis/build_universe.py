#!/usr/bin/env python3
"""J-Quants の上場銘柄一覧から screen.py 互換のユニバース CSV を構築する CLI。

使い方（リポジトリルートから）:
    python3 analysis/build_universe.py [--out data/universe/jquants-all.csv]
        [--market プライム] [--sector33 銀行] [--no-exclude-etf-reit]
        [--date 2025-01-06]

環境変数 ``JQUANTS_REFRESH_TOKEN``（https://jpx-jquants.com/ の無料プラン登録で発行、
有効期限約1週間）が必要。未設定・期限切れの場合は ``stocklib.jquants`` の
``JQuantsAuthError`` が導入手順つきのメッセージを表示する。

- ``stocklib.jquants.fetch_listed_info()`` が返す全上場銘柄（5桁コード）を
  screen.py 互換の列（``code,name,sector``）に変換して CSV に書き出し、
  生成した CSV パスと銘柄数を stdout に出力する。
- J-Quants の5桁コードは予備桁（5桁目）が ``"0"`` のものだけ4桁化できる
  （``normalize_jquants_code`` との往復一致で判定）。優先株等（例: ``"25935"``）は
  4桁化できないためスキップし、件数を stdout に報告する。
- ``--exclude-etf-reit``（既定 ON）: 33業種・市場区分が「その他」に分類される銘柄
  （ETF・REIT・インフラファンド等、普通株以外。2025年時点の J-Quants 仕様）を除外する。
- 既定の出力先 ``data/universe/`` は gitignore 範囲（生成物はコミットしない設計）。
  ディレクトリは自動作成する。

生成した CSV は screen.py にそのまま渡せる::

    python3 analysis/screen.py --universe data/universe/jquants-all.csv --rsi-below 30

注意: Free プランの銘柄一覧・株価は12週間遅延（2025年時点）。全銘柄スクリーニングの
価格取得は yfinance 経由のため小型株ではデータ品質・所要時間に注意する
（詳細は knowledge/data-sources/data-apis-and-tools.md の J-Quants 節）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from stocklib.data import REPO_ROOT
from stocklib.jquants import JQuantsError, fetch_listed_info, normalize_jquants_code

DEFAULT_OUT: Path = REPO_ROOT / "data" / "universe" / "jquants-all.csv"

# fetch_listed_info() の応答で使う列名（2025年時点の J-Quants /listed/info 仕様）
CODE_COL: str = "Code"
NAME_COL: str = "CompanyName"
SECTOR33_COL: str = "Sector33CodeName"
MARKET_COL: str = "MarketCodeName"

# ETF・REIT・インフラファンド等の普通株以外は、33業種・市場区分とも「その他」に
# 分類される（2025年時点の J-Quants 仕様）。「その他製品」は実在の33業種なので
# 部分一致ではなく完全一致で判定する。
NON_EQUITY_LABEL: str = "その他"

# screen.py / yfinance が受け付ける4桁コード（数字・英大文字、先頭と3文字目は数字）。
# stocklib.data.normalize_code / stocklib.jquants.normalize_jquants_code と同じパターン。
_FOUR_CHAR_CODE_RE = re.compile(r"[0-9][0-9A-Z][0-9][0-9A-Z]")


@dataclass(frozen=True)
class BuildStats:
    """ユニバース構築の集計。

    Attributes:
        total: fetch_listed_info が返した行数（フィルタ前）。
        market_excluded: ``--market`` の部分一致で除外した行数。
        sector_excluded: ``--sector33`` の部分一致で除外した行数。
        non_equity_excluded: ETF・REIT 等（普通株以外）として除外した行数。
        code_skipped: 4桁コードに変換できずスキップした行数（優先株等）。
        skipped_codes: スキップした元の5桁コード（報告用）。
        kept: 最終的にユニバースに残った銘柄数（重複除去後）。
    """

    total: int
    market_excluded: int
    sector_excluded: int
    non_equity_excluded: int
    code_skipped: int
    skipped_codes: tuple[str, ...]
    kept: int


def to_screen_code(jq_code: str) -> str | None:
    """J-Quants の5桁コードを screen.py が使う4桁形式に変換する。

    既存の正規化 :func:`stocklib.jquants.normalize_jquants_code`（4桁 → 5桁）との
    往復が一致するものだけ4桁化する（例: ``"72030"`` → ``"7203"``、
    ``"130A0"`` → ``"130A"``）。予備桁（5桁目）が ``"0"`` 以外の優先株等
    （例: ``"25935"``）や不正なコードは ``None`` を返す（スキップ対象）。
    """
    code = str(jq_code).strip().upper()
    if _FOUR_CHAR_CODE_RE.fullmatch(code):
        return code  # 既に4桁形式
    candidate = code[:4]
    try:
        if normalize_jquants_code(candidate) == code:
            return candidate
    except ValueError:
        pass
    return None


def _sector_value(value: object) -> str:
    """Sector33CodeName の値をユニバース CSV の sector 列文字列にする（欠損は ``"-"``）。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    text = str(value).strip()
    return text if text else "-"


def build_universe(
    listed: pd.DataFrame,
    *,
    market: str | None = None,
    sector33: str | None = None,
    exclude_etf_reit: bool = True,
) -> tuple[pd.DataFrame, BuildStats]:
    """上場銘柄一覧 DataFrame から screen.py 互換のユニバース表を構築する。

    Args:
        listed: :func:`stocklib.jquants.fetch_listed_info` の返り値
            （``Code`` / ``CompanyName`` 列必須、``Sector33CodeName`` /
            ``MarketCodeName`` 列は任意）。
        market: 市場区分名の部分一致フィルタ（例: ``"プライム"``）。``None`` で無効。
        sector33: 33業種名の部分一致フィルタ（例: ``"銀行"``）。``None`` で無効。
        exclude_etf_reit: True なら33業種・市場区分が「その他」の銘柄
            （ETF・REIT 等の普通株以外）を除外する。

    Returns:
        ``(universe, stats)``。``universe`` は ``code,name,sector`` 列の
        DataFrame（code 昇順・重複除去済み）。

    Raises:
        ValueError: 必須列が無い、またはフィルタに必要な列が応答に無い場合。
    """
    missing = [c for c in (CODE_COL, NAME_COL) if c not in listed.columns]
    if missing:
        raise ValueError(f"上場銘柄一覧に必要な列がありません: {missing}")
    df = listed.copy()
    total = len(df)

    market_excluded = 0
    if market is not None:
        if MARKET_COL not in df.columns:
            raise ValueError(f"--market フィルタに必要な列 {MARKET_COL} が応答にありません")
        mask = df[MARKET_COL].astype(str).str.contains(market, case=False, regex=False)
        market_excluded = int((~mask).sum())
        df = df[mask]

    sector_excluded = 0
    if sector33 is not None:
        if SECTOR33_COL not in df.columns:
            raise ValueError(f"--sector33 フィルタに必要な列 {SECTOR33_COL} が応答にありません")
        mask = df[SECTOR33_COL].astype(str).str.contains(sector33, case=False, regex=False)
        sector_excluded = int((~mask).sum())
        df = df[mask]

    non_equity_excluded = 0
    if exclude_etf_reit:
        mask = pd.Series(False, index=df.index)
        for col in (SECTOR33_COL, MARKET_COL):
            if col in df.columns:
                mask |= df[col].astype(str).str.strip() == NON_EQUITY_LABEL
        non_equity_excluded = int(mask.sum())
        df = df[~mask]

    rows: list[dict[str, str]] = []
    skipped: list[str] = []
    for rec in df.to_dict("records"):
        code4 = to_screen_code(str(rec[CODE_COL]))
        if code4 is None:
            skipped.append(str(rec[CODE_COL]).strip())
            continue
        rows.append(
            {
                "code": code4,
                "name": str(rec[NAME_COL]).strip(),
                "sector": _sector_value(rec.get(SECTOR33_COL)),
            }
        )
    universe = pd.DataFrame(rows, columns=["code", "name", "sector"])
    universe = universe.drop_duplicates(subset="code").sort_values("code").reset_index(drop=True)
    stats = BuildStats(
        total=total,
        market_excluded=market_excluded,
        sector_excluded=sector_excluded,
        non_equity_excluded=non_equity_excluded,
        code_skipped=len(skipped),
        skipped_codes=tuple(skipped),
        kept=len(universe),
    )
    return universe, stats


def write_universe_csv(universe: pd.DataFrame, out_path: Path, *, header_comment: str) -> Path:
    """ユニバース表を screen.py 互換の CSV（先頭に ``#`` コメント行）に書き出す。

    出力先ディレクトリは自動作成する。``header_comment`` は1行（改行不可）。
    """
    if "\n" in header_comment:
        raise ValueError("header_comment に改行は使えません（screen.py の comment='#' 読み込み互換のため）")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        f.write(f"# {header_comment}\n")
        universe.to_csv(f, index=False)
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="J-Quants の上場銘柄一覧から screen.py 互換のユニバース CSV"
        "（列: code,name,sector）を構築する（要 JQUANTS_REFRESH_TOKEN）"
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT,
        help="出力先 CSV（既定: data/universe/jquants-all.csv。data/ は gitignore 範囲）",
    )
    parser.add_argument(
        "--market", default=None,
        help="市場区分名の部分一致で絞る（例: プライム / スタンダード / グロース）",
    )
    parser.add_argument(
        "--sector33", default=None,
        help="33業種名の部分一致で絞る（例: 銀行 / 電気機器 / 医薬品）",
    )
    parser.add_argument(
        "--exclude-etf-reit", action=argparse.BooleanOptionalAction, default=True,
        help="33業種・市場区分が「その他」の銘柄（ETF・REIT 等の普通株以外）を除外する"
        "（既定 ON。--no-exclude-etf-reit で含める）",
    )
    parser.add_argument(
        "--date", default=None,
        help="銘柄一覧の基準日（YYYY-MM-DD。省略時は最新。Free プランは12週間遅延、2025年時点）",
    )
    args = parser.parse_args(argv)

    try:
        listed = fetch_listed_info(date=args.date)
    except JQuantsError as exc:
        # JQuantsAuthError（トークン未設定・期限切れ）の導入手順つきメッセージを
        # そのまま表示する（握りつぶさない）。
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    try:
        universe, stats = build_universe(
            listed,
            market=args.market,
            sector33=args.sector33,
            exclude_etf_reit=args.exclude_etf_reit,
        )
    except ValueError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    if universe.empty:
        print(
            "エラー: 条件に合致する銘柄が0件のため CSV を生成しません"
            "（--market / --sector33 の指定を確認してください）",
            file=sys.stderr,
        )
        return 1

    conds: list[str] = []
    if args.market is not None:
        conds.append(f"市場区分〜{args.market}")
    if args.sector33 is not None:
        conds.append(f"33業種〜{args.sector33}")
    if args.exclude_etf_reit:
        conds.append("ETF・REIT等除外")
    cond_text = "、".join(conds) if conds else "なし（全上場銘柄）"
    header_comment = (
        f"J-Quants 上場銘柄一覧から build_universe.py が生成"
        f"（生成日: {dt.date.today().isoformat()}、条件: {cond_text}。"
        "Free プランは12週間遅延データ、2025年時点の仕様）"
    )
    path = write_universe_csv(universe, args.out, header_comment=header_comment)

    print(f"上場銘柄一覧: {stats.total} 件（J-Quants /listed/info）")
    if args.market is not None:
        print(f"市場区分フィルタ（--market {args.market}）で除外: {stats.market_excluded} 件")
    if args.sector33 is not None:
        print(f"33業種フィルタ（--sector33 {args.sector33}）で除外: {stats.sector_excluded} 件")
    if args.exclude_etf_reit:
        print(f"ETF・REIT等（普通株以外）を除外: {stats.non_equity_excluded} 件")
    if stats.code_skipped:
        preview = "、".join(stats.skipped_codes[:5])
        suffix = "…" if stats.code_skipped > 5 else ""
        print(f"4桁コードに変換できずスキップ: {stats.code_skipped} 件（優先株等。例: {preview}{suffix}）")
    print(f"ユニバース: {stats.kept} 銘柄")
    print(f"CSV: {path}")
    print(f"\n次の例: python3 analysis/screen.py --universe {path} --rsi-below 30")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

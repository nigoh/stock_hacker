#!/usr/bin/env python3
"""セクターローテーション（セクター別相対強度）CLI。

「いま相対的に強い / 弱いセクターはどこか」を、ユニバースの各銘柄のモメンタムを
セクター単位に集約して機械的に順位づけする。個別銘柄の強弱だけでは見えない
「資金がどのセクターに向かっているか」の輪郭を、クロスセクションの相対比較で与える。

使い方（リポジトリルートから）:

    python3 analysis/sector_rotation.py                        # 既定: large70
    python3 analysis/sector_rotation.py --universe <CSV>       # 任意ユニバース
    python3 analysis/sector_rotation.py --synthetic            # 合成データ（ネット不要）

自動実行（Routine / cron）向けの機械可読な契約（market_breadth.py に準拠）:

- stdout の最終行に
  ``RESULT sectors=<セクター数> covered=<取得成功数>/<総数> data=<real|synthetic|unavailable>``
- 実データが1件も取れなかった場合は exit code 2 / ``data=unavailable``（レポート非生成）。
- ユニバース CSV の不正等は exit code 1（RESULT 行なし）。

**セクターローテーションはクロスセクションの機械的な相対比較であり、将来の騰落の
予測でも売買助言でもない。** セクター分類はユニバース CSV の sector 列に依存する。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

from stocklib import report, sector
from stocklib.data import (
    REPO_ROOT,
    DataFetchError,
    add_source_argument,
    fetch_prices,
    set_default_source,
)

LARGE70: Path = REPO_ROOT / "analysis" / "universe" / "large70.csv"


def load_universe(path: Path) -> list[tuple[str, str, str]]:
    """ユニバース CSV から (code, name, sector) を読む（code 列必須、sector 列推奨）。"""
    df = pd.read_csv(path, comment="#", dtype=str)
    if "code" not in df.columns:
        raise ValueError(f"ユニバース CSV には code 列が必要です: {path}")
    out: list[tuple[str, str, str]] = []
    for rec in df.to_dict("records"):
        code = str(rec["code"]).strip()
        if not code:
            continue
        name = str(rec.get("name", "") or rec.get("note", "") or "").strip()
        sec = str(rec.get("sector", "") or "").strip()
        out.append((code, name, sec))
    return out


def _fetch_universe(
    items: list[tuple[str, str, str]], period: str, synthetic: bool
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    prices: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    for code, _name, _sector in items:
        try:
            prices[code] = fetch_prices(code, period=period, synthetic=synthetic)[code]
        except DataFetchError as exc:
            errors.append(f"{code}: {exc}")
    return prices, errors


def build_report(
    rows: list[sector.SectorRow], universe_path: Path, n_covered: int, n_total: int,
    errors: list[str], synthetic: bool,
) -> str:
    today = dt.date.today().isoformat()
    lines = [report.report_header(f"セクターローテーション（{today}）")]
    lines.append(f"- ユニバース: {universe_path}（{n_covered}/{n_total} 銘柄で集計、{len(rows)} セクター）")
    lines.append(f"- データ出所: {'合成データ' if synthetic else 'yfinance'}")
    if synthetic:
        lines.append("- **データ: 合成データ（--synthetic）による手法デモであり実際の市況ではありません**")
    lines.append("")

    lines.append("## セクター別モメンタム順位")
    lines.append("")
    lines.append(
        f"各銘柄の複数期間リターンをセクター内で**中央値**集約し、"
        f"代表窓（{sector.RANK_WINDOW}営業日 ≈ 3ヶ月）モメンタムの降順で順位づけ"
        "（上＝相対的に強いリーダー、下＝ラガード）。"
    )
    lines.append("")
    header = ["順位", "セクター", "銘柄数"]
    header += [label for _w, label in sector.MOMENTUM_WINDOWS]
    table_rows = []
    for r in rows:
        row = [str(r.rank), r.sector, str(r.n)]
        for w, _label in sector.MOMENTUM_WINDOWS:
            m = r.momentum.get(w)
            row.append(report.fmt_pct(m) if m is not None else "-")
        table_rows.append(row)
    lines.append(report.markdown_table(header, table_rows))
    lines.append("")

    lines.append("## セクター内ブレッシュ（終値 > SMA50 の割合）")
    lines.append("")
    lines.append(
        f"各セクターで終値が直近{sector.BREADTH_SMA_WINDOW}日移動平均以上の銘柄割合。"
        "セクター内での値上がりの広がり（上昇に参加している銘柄の多さ）を測る。"
    )
    lines.append("")
    breadth_rows = []
    for r in rows:
        breadth_rows.append([
            str(r.rank),
            r.sector,
            report.fmt_pct(r.breadth_above_sma),
            f"{r.breadth_base}/{r.n} 銘柄で判定",
        ])
    lines.append(report.markdown_table(
        ["順位", "セクター", ">SMA50 割合", "母数"], breadth_rows
    ))
    lines.append("")

    lines.append(
        "注: セクター分類はユニバース CSV の sector 列に由来する。"
        "モメンタムはセクター内中央値のため外れ値に頑健だが、**構成銘柄が少ないセクター"
        "（銘柄数1〜2）は中央値・ブレッシュとも統計的に不安定**で、順位の入れ替わりも起きやすい。"
        "セクターローテーションは景気・金利局面での主導セクターの循環という経験則に基づく見方だが、"
        "ここでの集計は過去リターンの**クロスセクションの機械的な相対比較であり、将来の騰落の予測でも"
        "投資助言でもない。** RS の高いセクター＝過去に強かった、であって将来を保証しない。"
    )
    lines.append("")
    if errors:
        lines.append("## 取得失敗")
        lines.extend(f"- {e}" for e in errors)
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ユニバースのセクター別相対強度（セクターローテーション）を集計する"
    )
    parser.add_argument("--universe", type=Path, default=LARGE70,
                        help=f"ユニバース CSV（code 列必須、sector 列推奨。既定: {LARGE70}）")
    parser.add_argument("--period", default="2y",
                        help="取得期間（既定: 2y。12ヶ月モメンタムに 1y 以上を推奨）")
    parser.add_argument("--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）")
    add_source_argument(parser)
    args = parser.parse_args(argv)
    set_default_source(args.source)

    try:
        items = load_universe(args.universe)
    except (ValueError, OSError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    n_total = len(items)

    prices, errors = _fetch_universe(items, args.period, args.synthetic)
    if not prices:
        print("エラー: ユニバースの実データを1件も取得できませんでした。"
              "Yahoo（query1/2.finance.yahoo.com）への到達性を確認してください。", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        print(f"RESULT sectors=0 covered=0/{n_total} data=unavailable")
        return 2

    sectors = {c: s for c, _n, s in items}
    rows = sector.compute_sector_rotation(prices, sectors)
    n_covered = len(prices)
    content = build_report(rows, args.universe, n_covered, n_total, errors, args.synthetic)
    print(content)
    path = report.save_report(content, f"sector-{dt.date.today().isoformat()}.md")
    print(f"レポート: {path}")
    print(f"RESULT sectors={len(rows)} covered={n_covered}/{n_total} "
          f"data={'synthetic' if args.synthetic else 'real'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

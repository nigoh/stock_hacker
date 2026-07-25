#!/usr/bin/env python3
"""相対強度（RS）＆セクター相対バリュエーション CLI。

ユニバースの中で「相対的に強い / 割安な銘柄はどれか」を横断（クロスセクション）で
順位づけする。個別の絶対水準では見えない相対の文脈を与え、銘柄選別の入口にする。

使い方（リポジトリルートから）:

    python3 analysis/relative_strength.py                     # 既定: liquid30
    python3 analysis/relative_strength.py --universe <CSV>    # 任意ユニバース
    python3 analysis/relative_strength.py --top 10            # 上位/下位の表示件数
    python3 analysis/relative_strength.py --no-valuation      # PER/PBR 取得を省く（高速）
    python3 analysis/relative_strength.py --synthetic         # 合成データ（ネット不要）

自動実行向けの機械可読な契約:

- 最終行に ``RESULT rs=<算出数>/<総数> valuation=<PER取得数> data=<real|synthetic|unavailable>``
- 実データが1件も取れなければ exit 2 / ``data=unavailable``。CSV 不正等は exit 1。

**相対強度・相対バリュエーションはクロスセクションの機械的比較であり、将来の騰落の
予測でも売買助言でもない。** RS が高い＝過去に強かった、であって将来を保証しない。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

from stocklib import relative, report
from stocklib.data import (
    REPO_ROOT,
    DataFetchError,
    add_source_argument,
    fetch_info,
    fetch_prices,
    set_default_source,
)

LIQUID30: Path = REPO_ROOT / "analysis" / "universe" / "liquid30.csv"


def load_universe(path: Path) -> list[tuple[str, str, str]]:
    """ユニバース CSV から (code, name, sector) を読む（code 列必須）。"""
    df = pd.read_csv(path, comment="#", dtype=str)
    if "code" not in df.columns:
        raise ValueError(f"ユニバース CSV には code 列が必要です: {path}")
    out: list[tuple[str, str, str]] = []
    for rec in df.to_dict("records"):
        code = str(rec["code"]).strip()
        if not code:
            continue
        name = str(rec.get("name", "") or rec.get("note", "") or "").strip()
        sector = str(rec.get("sector", "") or "").strip()
        out.append((code, name, sector))
    return out


def _fmt_head(code: str, name: str) -> str:
    return f"{code}" + (f"（{name}）" if name else "")


def build_rs_section(rows: list[relative.RSRow], top: int) -> list[str]:
    lines = ["## 相対強度（RS）ランキング", ""]
    lines.append(
        "RS ランクはモメンタム（3/6/9/12ヶ月リターンの加重合成）のユニバース内"
        "パーセンタイル（1〜99、高いほど相対的に強い）。"
    )
    lines.append("")

    def _table(subset: list[relative.RSRow]) -> str:
        table_rows = []
        for r in subset:
            table_rows.append([
                report.fmt_num(round(r.rs_rank)),
                _fmt_head(r.code, r.name),
                report.fmt_pct(r.blended_return),
                report.fmt_pct(r.components.get(63, float("nan"))),
                report.fmt_pct(r.components.get(126, float("nan"))),
                report.fmt_pct(r.components.get(252, float("nan"))),
            ])
        return report.markdown_table(
            ["RS", "銘柄", "合成モメンタム", "3ヶ月", "6ヶ月", "12ヶ月"], table_rows
        )

    if len(rows) <= 2 * top:
        lines.append(_table(rows))
    else:
        lines.append(f"**上位 {top}**")
        lines.append("")
        lines.append(_table(rows[:top]))
        lines.append("")
        lines.append(f"**下位 {top}**")
        lines.append("")
        lines.append(_table(rows[-top:]))
    lines.append("")
    return lines


def build_valuation_section(rows: list[relative.ValationRow]) -> list[str]:
    lines = ["## セクター相対バリュエーション", ""]
    lines.append(
        "各銘柄の PER / PBR を同セクター中央値と比較（プレミアム = 銘柄 ÷ セクター中央値 − 1。"
        "− が相対的に割安、+ が割高）。中央値はこのユニバース内の同セクター銘柄から算出。"
    )
    lines.append("")
    table_rows = []
    for r in rows:
        table_rows.append([
            _fmt_head(r.code, r.name),
            r.sector or "-",
            report.fmt_num(r.per) if r.per is not None else "-",
            report.fmt_pct(r.per_premium) if r.per_premium is not None else "-",
            report.fmt_num(r.pbr) if r.pbr is not None else "-",
            report.fmt_pct(r.pbr_premium) if r.pbr_premium is not None else "-",
        ])
    lines.append(report.markdown_table(
        ["銘柄", "セクター", "PER", "PER乖離", "PBR", "PBR乖離"], table_rows
    ))
    lines.append("")
    lines.append(
        "注: 低PER・低PBR が割安を意味するとは限らない（成長率・ROE・リスクの差の反映で"
        "ありうる。`knowledge/fundamental/valuation-metrics.md` の PBR=PER×ROE 参照）。"
        "少数銘柄のセクター中央値は不安定。"
    )
    lines.append("")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ユニバース内の相対強度（RS）とセクター相対バリュエーションを集計する"
    )
    parser.add_argument("--universe", type=Path, default=LIQUID30,
                        help=f"ユニバース CSV（code 列必須、sector 列推奨。既定: {LIQUID30}）")
    parser.add_argument("--period", default="2y",
                        help="取得期間（既定: 2y。12ヶ月モメンタムに 1y 以上を推奨）")
    parser.add_argument("--top", type=int, default=10, help="RS 上位/下位の表示件数（既定: 10）")
    parser.add_argument("--no-valuation", action="store_true",
                        help="PER/PBR の取得（セクター相対バリュエーション）を省いて高速化する")
    parser.add_argument("--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）")
    add_source_argument(parser)
    args = parser.parse_args(argv)
    set_default_source(args.source)
    if args.top < 1:
        parser.error("--top には 1 以上を指定してください")

    try:
        items = load_universe(args.universe)
    except (ValueError, OSError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    n_total = len(items)
    names = {c: n for c, n, _s in items}
    sectors = {c: s for c, _n, s in items}

    prices: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    for code, _n, _s in items:
        try:
            prices[code] = fetch_prices(code, period=args.period, synthetic=args.synthetic)[code]
        except DataFetchError as exc:
            errors.append(f"{code}: {exc}")

    if not prices and not args.synthetic:
        # 実データ全滅のみ発火（合成は取得失敗しないため空ユニバースでは落とさない）。
        print("エラー: ユニバースの実データを1件も取得できませんでした。"
              "Yahoo への到達性を確認してください。", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        print(f"RESULT rs=0/{n_total} valuation=0 data=unavailable")
        return 2

    rs_rows = relative.compute_relative_strength(prices, names)

    today = dt.date.today().isoformat()
    lines = [report.report_header(f"相対強度・相対バリュエーション（{today}）")]
    lines.append(f"- ユニバース: {args.universe}（{len(rs_rows)}/{n_total} 銘柄で RS 算出）")
    lines.append(f"- データ出所: {'合成データ' if args.synthetic else 'yfinance'}")
    if args.synthetic:
        lines.append("- **データ: 合成データ（--synthetic）による手法デモであり実データではありません**")
    lines.append("")
    lines.extend(build_rs_section(rs_rows, args.top))

    n_valuation = 0
    if not args.no_valuation:
        infos: dict[str, dict[str, object]] = {}
        for code in prices:
            try:
                infos[code] = fetch_info(code, synthetic=args.synthetic)
            except DataFetchError:
                continue
        val_rows = relative.sector_relative_valuation(infos, sectors, names)
        n_valuation = sum(1 for r in val_rows if r.per is not None)
        lines.extend(build_valuation_section(val_rows))

    if errors:
        lines.append("## 取得失敗")
        lines.extend(f"- {e}" for e in errors)
        lines.append("")

    content = "\n".join(lines)
    print(report.with_disclaimer(content))
    path = report.save_report(content, f"relative-{today}.md")
    print(f"レポート: {path}")
    print(f"RESULT rs={len(rs_rows)}/{n_total} valuation={n_valuation} "
          f"data={'synthetic' if args.synthetic else 'real'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""ペアトレード共和分スクリーナ CLI。

ユニバースの全ペアについて、対数価格の連動性とスプレッドの平均回帰の強さを測り、
統計的裁定（ペアトレード）の候補を並べる。

使い方（リポジトリルートから）:

    python3 analysis/pairs_screen.py                       # 既定: liquid30
    python3 analysis/pairs_screen.py --universe <CSV>      # 任意ユニバース
    python3 analysis/pairs_screen.py --same-sector         # 同セクターのペアのみ
    python3 analysis/pairs_screen.py --top 20              # 表示件数
    python3 analysis/pairs_screen.py --synthetic           # 合成データ（ネット不要）

自動実行向けの機械可読な契約:

- 最終行に ``RESULT pairs=<評価ペア数> mean_reverting=<平均回帰候補数> data=<real|synthetic|unavailable>``
- 実データが2銘柄も取れなければ exit 2 / ``data=unavailable``。CSV 不正等は exit 1。

**インサンプルの統計的スクリーニングであり将来の平均回帰も売買助言でもない。** N銘柄で
N(N-1)/2 ペアを検定する多重比較のため偽陽性が出やすい（`docs`・knowledge 参照）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

from stocklib import pairs, report
from stocklib.data import (
    REPO_ROOT,
    DataFetchError,
    add_source_argument,
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
        if code:
            name = str(rec.get("name", "") or rec.get("note", "") or "").strip()
            sector = str(rec.get("sector", "") or "").strip()
            out.append((code, name, sector))
    return out


def build_report(
    results: list[pairs.PairResult], universe_path: Path, n_codes: int,
    top: int, same_sector: bool, synthetic: bool,
) -> str:
    today = dt.date.today().isoformat()
    lines = [report.report_header(f"ペアトレード候補スクリーニング（{today}）")]
    lines.append(f"- ユニバース: {universe_path}（{n_codes} 銘柄、{len(results)} ペアを評価）")
    lines.append(f"- データ出所: {'合成データ' if synthetic else 'yfinance'}")
    if same_sector:
        lines.append("- 対象: 同セクターのペアのみ")
    if synthetic:
        lines.append("- **データ: 合成データ（--synthetic）による手法デモであり実データではありません**")
    lines.append("")

    n_mr = sum(1 for r in results if r.is_mean_reverting())
    lines.append(
        f"平均回帰候補（DF統計量 < {pairs.DF_CRIT_5PCT:g}、目安5%水準）: {n_mr} ペア。"
        "DF統計量が小さいほどスプレッドは定常的（平均回帰的）。半減期は乖離が半分に戻る"
        "平均営業日数。z スコアは直近スプレッドの標準偏差単位の乖離（|z|≥2 が伝統的なエントリー目安）。"
    )
    lines.append("")
    lines.append("## 上位候補（平均回帰の強い順）")
    lines.append("")
    rows = []
    for r in results[:top]:
        pair = f"{r.code_a}" + (f"（{r.name_a}）" if r.name_a else "") + " / " + \
               f"{r.code_b}" + (f"（{r.name_b}）" if r.name_b else "")
        rows.append([
            pair,
            f"{r.sector_a}" + ("＝同" if r.same_sector else f"/{r.sector_b}") if r.sector_a else "-",
            report.fmt_num(r.corr, 2),
            report.fmt_num(r.beta, 3),
            report.fmt_num(r.df_stat, 2),
            report.fmt_num(r.half_life, 1) if r.half_life == r.half_life else "-",
            report.fmt_num(r.zscore, 2),
        ])
    if rows:
        lines.append(report.markdown_table(
            ["ペア（A / B）", "セクター", "相関", "β", "DF統計量", "半減期", "現z"], rows
        ))
    else:
        lines.append("（評価できるペアがありませんでした。期間・銘柄数を確認してください）")
    lines.append("")
    lines.append(
        "注: インサンプルの統計であり将来の平均回帰を保証しない。N銘柄で N(N-1)/2 ペアを"
        "検定する多重比較のため、DF統計量が臨界値を割るペアが偶然に現れやすい"
        "（`knowledge/strategies/pairs-trading-and-arbitrage.md` の多重検定・見せかけの回帰の注意参照）。"
        "実運用には貸株調達・逆日歩・空売り規制・取引コストの検討が要る。**売買助言ではない。**"
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ユニバースの全ペアから共和分（平均回帰）候補をスクリーニングする"
    )
    parser.add_argument("--universe", type=Path, default=LIQUID30,
                        help=f"ユニバース CSV（code 列必須、sector 列推奨。既定: {LIQUID30}）")
    parser.add_argument("--period", default="3y",
                        help="取得期間（既定: 3y。共和分の推定には長めを推奨）")
    parser.add_argument("--top", type=int, default=15, help="表示するペア数（既定: 15）")
    parser.add_argument("--same-sector", action="store_true",
                        help="同セクターのペアのみ評価する（経済的な連動の裏付けがあるペアに絞る）")
    parser.add_argument("--min-overlap", type=int, default=pairs.DEFAULT_MIN_OVERLAP,
                        help=f"評価に必要な最小の重なり営業日数（既定: {pairs.DEFAULT_MIN_OVERLAP}）")
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

    names = {c: n for c, n, _s in items}
    sectors = {c: s for c, _n, s in items}
    prices: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    for code, _n, _s in items:
        try:
            prices[code] = fetch_prices(code, period=args.period, synthetic=args.synthetic)[code]
        except DataFetchError as exc:
            errors.append(f"{code}: {exc}")

    if len(prices) < 2 and not args.synthetic:
        # 実データ全滅のみ発火（合成は取得失敗しないため、薄い入力では落とさない）。
        print("エラー: ペア評価には最低2銘柄の実データが必要ですが取得できませんでした。"
              "Yahoo への到達性を確認してください。", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        print("RESULT pairs=0 mean_reverting=0 data=unavailable")
        return 2

    results = pairs.find_pairs(
        prices, names, sectors,
        min_overlap=args.min_overlap, same_sector_only=args.same_sector,
    )
    n_mr = sum(1 for r in results if r.is_mean_reverting())
    content = build_report(results, args.universe, len(prices), args.top,
                           args.same_sector, args.synthetic)
    print(content)
    path = report.save_report(content, f"pairs-{dt.date.today().isoformat()}.md")
    print(f"レポート: {path}")
    print(f"RESULT pairs={len(results)} mean_reverting={n_mr} "
          f"data={'synthetic' if args.synthetic else 'real'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

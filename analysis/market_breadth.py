#!/usr/bin/env python3
"""市場ブレッシュ CLI — ユニバース全体の内部（マーケット・インターナルズ）を集計する。

指数の水準だけでは見えない「市場全体の値上がりの広がり」を、ユニバースの銘柄群から
機械的に集計する。移動平均超の銘柄割合・騰落数・騰落レシオ（25日）・新高値/新安値を
1本のレポートにまとめる。

使い方（リポジトリルートから）:

    python3 analysis/market_breadth.py                          # 既定: liquid30
    python3 analysis/market_breadth.py --universe <CSV>         # 任意ユニバース
    python3 analysis/market_breadth.py --synthetic              # 合成データ（ネット不要）

自動実行（Routine / cron）向けの機械可読な契約（daily_brief.py に準拠）:

- stdout の最終行に
  ``RESULT breadth=<集計成功数>/<総数> ad_ratio=<騰落レシオ|na> data=<real|synthetic|unavailable>``
- 実データが1件も取れなかった場合は exit code 2 / ``data=unavailable``（レポート非生成）。
- ユニバース CSV の不正等は exit code 1（RESULT 行なし）。

**ブレッシュは機械的な内部状態の記述であり、将来の騰落の予測でも売買助言でもない。**
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

from stocklib import breadth, report
from stocklib.data import (
    REPO_ROOT,
    DataFetchError,
    add_source_argument,
    fetch_prices,
    set_default_source,
)

LIQUID30: Path = REPO_ROOT / "analysis" / "universe" / "liquid30.csv"


def load_universe(path: Path) -> list[tuple[str, str]]:
    """ユニバース CSV（``code`` 列必須、``name``/``note`` 任意）から (code, name) を読む。"""
    df = pd.read_csv(path, comment="#", dtype=str)
    if "code" not in df.columns:
        raise ValueError(f"ユニバース CSV には code 列が必要です: {path}")
    name_col = "name" if "name" in df.columns else ("note" if "note" in df.columns else None)
    items: list[tuple[str, str]] = []
    for rec in df.to_dict("records"):
        code = str(rec["code"]).strip()
        if code:
            items.append((code, str(rec.get(name_col, "") or "").strip() if name_col else ""))
    return items


def _fetch_universe(
    items: list[tuple[str, str]], period: str, synthetic: bool
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    prices: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    for code, _name in items:
        try:
            prices[code] = fetch_prices(code, period=period, synthetic=synthetic)[code]
        except DataFetchError as exc:
            errors.append(f"{code}: {exc}")
    return prices, errors


def build_report(
    result: breadth.BreadthResult, universe_path: Path, n_total: int,
    errors: list[str], synthetic: bool,
) -> str:
    today = dt.date.today().isoformat()
    lines = [report.report_header(f"市場ブレッシュ（{today}）")]
    lines.append(f"- ユニバース: {universe_path}（{result.n}/{n_total} 銘柄で集計）")
    lines.append(f"- データ出所: {'合成データ' if synthetic else 'yfinance'}")
    if synthetic:
        lines.append("- **データ: 合成データ（--synthetic）による手法デモであり実際の市況ではありません**")
    lines.append("")

    lines.append("## 騰落（前日比）")
    lines.append("")
    lines.append(report.markdown_table(
        ["区分", "銘柄数", "割合"],
        [
            ["値上がり", str(result.advancers), report.fmt_pct(result.advance_pct)],
            ["値下がり", str(result.decliners),
             report.fmt_pct(result.decliners / result.n if result.n else float("nan"))],
            ["変わらず", str(result.unchanged),
             report.fmt_pct(result.unchanged / result.n if result.n else float("nan"))],
        ],
    ))
    lines.append("")

    lines.append("## 移動平均超の銘柄割合")
    lines.append("")
    rows = [
        [f"SMA{w}以上", report.fmt_pct(result.pct_above_sma.get(w, float("nan"))),
         f"{result.sma_base.get(w, 0)} 銘柄で判定"]
        for w in breadth.SMA_WINDOWS
    ]
    lines.append(report.markdown_table(["指標", "割合", "母数"], rows))
    lines.append("")

    lines.append("## 騰落レシオ・新高値/新安値")
    lines.append("")
    ad = result.ad_ratio_25
    lines.append(report.markdown_table(
        ["指標", "値", "備考"],
        [
            [f"騰落レシオ（{breadth.AD_RATIO_WINDOW}日）",
             report.fmt_num(ad, 1) if ad is not None else "-", result.ad_ratio_label()],
            ["新高値（52週）", str(result.new_highs), "直近252営業日の高値更新"],
            ["新安値（52週）", str(result.new_lows), "直近252営業日の安値更新"],
        ],
    ))
    lines.append("")
    lines.append(
        "解釈の枠組みは `knowledge/technical/volume-and-market-internals.md` を参照。"
        "騰落レシオは一般に120以上で過熱・70以下で売られすぎとされる（経験則の目安、2025年時点）。"
        "移動平均超の割合が高い＝広く上昇、指数高値でもブレッシュが伴わない（新高値が細い等）ときは"
        "上昇の裾野が狭い可能性を示す。**いずれも機械的な内部状態の記述であり将来予測ではない。**"
    )
    lines.append("")
    if errors:
        lines.append("## 取得失敗")
        lines.extend(f"- {e}" for e in errors)
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ユニバース全体の市場ブレッシュ（マーケット・インターナルズ）を集計する"
    )
    parser.add_argument("--universe", type=Path, default=LIQUID30,
                        help=f"ユニバース CSV（code 列必須。既定: {LIQUID30}）")
    parser.add_argument("--period", default="2y",
                        help="取得期間（既定: 2y。SMA200・騰落レシオ25日の算出に1y以上を推奨）")
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
    if not prices and not args.synthetic:
        # 実データ全滅（＝ネットワーク障害シグナル）。合成では発火させない
        # （合成は取得失敗しないため、ここに来るのは空ユニバース＝入力が薄いだけ）。
        print("エラー: ユニバースの実データを1件も取得できませんでした。"
              "Yahoo（query1/2.finance.yahoo.com）への到達性を確認してください。", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        print(f"RESULT breadth=0/{n_total} ad_ratio=na data=unavailable")
        return 2

    result = breadth.compute_breadth(prices)
    content = build_report(result, args.universe, n_total, errors, args.synthetic)
    print(content)
    path = report.save_report(content, f"breadth-{dt.date.today().isoformat()}.md")
    print(f"レポート: {path}")
    ad = f"{result.ad_ratio_25:.1f}" if result.ad_ratio_25 is not None else "na"
    print(f"RESULT breadth={result.n}/{n_total} ad_ratio={ad} "
          f"data={'synthetic' if args.synthetic else 'real'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""カタリスト・レーダー CLI — 直近の決算発表・配当落ち予定を洗い出す。

ウォッチリスト（既定 data/watchlist.csv、無ければ liquid30）またはユニバースの各銘柄に
ついて、Yahoo Finance のカレンダーイベント（次の決算発表日・配当落ち日）を取得し、
指定日数以内に到来するイベントを近い順に並べる。決算後ドリフト（PEAD）や権利落ちなど、
イベントドリブンの起点を機械的に把握するためのツール。

使い方（リポジトリルートから）:

    python3 analysis/catalyst_radar.py                     # 既定ユニバース、今後30日
    python3 analysis/catalyst_radar.py --within 14         # 今後14日以内
    python3 analysis/catalyst_radar.py --universe <CSV>
    python3 analysis/catalyst_radar.py --synthetic         # 合成データ（ネット不要）

自動実行向けの機械可読な契約:

- 最終行に ``RESULT events=<接近イベント数> covered=<取得成功>/<総数> data=<real|synthetic|unavailable>``
- 実データが1件も取れなければ exit 2 / ``data=unavailable``。CSV 不正等は exit 1。

**イベント日は取得日時点の予定であり変更されうる（確定ではない）。発表内容の予測でも
売買助言でもない。**
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

from stocklib import events, report
from stocklib.data import (
    REPO_ROOT,
    DataFetchError,
    add_source_argument,
    set_default_source,
)

DEFAULT_WATCHLIST: Path = REPO_ROOT / "data" / "watchlist.csv"
LIQUID30: Path = REPO_ROOT / "analysis" / "universe" / "liquid30.csv"


def resolve_universe(explicit: Path | None) -> tuple[Path, list[tuple[str, str]]]:
    """(採用パス, [(code, name), ...])。優先: --universe > watchlist > liquid30。"""
    if explicit is not None:
        path = explicit
    elif DEFAULT_WATCHLIST.exists():
        path = DEFAULT_WATCHLIST
    else:
        path = LIQUID30
    if not path.exists():
        raise ValueError(f"ユニバース CSV が見つかりません: {path}")
    df = pd.read_csv(path, comment="#", dtype=str)
    if "code" not in df.columns:
        raise ValueError(f"ユニバース CSV には code 列が必要です: {path}")
    name_col = "name" if "name" in df.columns else ("note" if "note" in df.columns else None)
    items: list[tuple[str, str]] = []
    for rec in df.to_dict("records"):
        code = str(rec["code"]).strip()
        if code:
            items.append((code, str(rec.get(name_col, "") or "").strip() if name_col else ""))
    return path, items


def build_report(
    hits: list[tuple[events.CalendarEvents, str, dt.date, int]],
    universe_path: Path, n_ok: int, n_total: int, within: int,
    errors: list[str], synthetic: bool,
) -> str:
    today = dt.date.today()
    lines = [report.report_header(f"カタリスト・レーダー（{today.isoformat()}、今後{within}日）")]
    lines.append(f"- ユニバース: {universe_path}（{n_ok}/{n_total} 銘柄の予定を取得）")
    lines.append(f"- データ出所: {'合成データ' if synthetic else 'yfinance カレンダー'}")
    if synthetic:
        lines.append("- **データ: 合成データ（--synthetic）による手法デモであり実在の予定ではありません**")
    lines.append("")
    lines.append(f"## 接近中のカタリスト（{within}日以内、近い順）")
    lines.append("")
    if hits:
        rows = [
            [
                d.isoformat(),
                f"あと{days}日" if days > 0 else "本日",
                label,
                f"{ev.code}" + (f"（{ev.name}）" if ev.name else ""),
            ]
            for ev, label, d, days in hits
        ]
        lines.append(report.markdown_table(["予定日", "残", "種別", "銘柄"], rows))
    else:
        lines.append(f"（{within}日以内に到来する決算・配当落ち予定は検出されませんでした）")
    lines.append("")
    lines.append(
        "決算発表は決算後ドリフト（PEAD）の起点、配当落ちは権利取り・優待クロスの起点になる"
        "（`knowledge/strategies/event-driven-japan.md`）。**予定日は取得日時点のもので変更され"
        "うる（確定ではない）。発表内容の予測でも売買助言でもない。** 会社発表・適時開示（TDnet）で"
        "最終確認すること。"
    )
    lines.append("")
    if errors:
        lines.append("## 取得失敗")
        lines.extend(f"- {e}" for e in errors)
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="直近の決算発表・配当落ち予定を洗い出すカタリスト・レーダー"
    )
    parser.add_argument("--universe", type=Path, default=None,
                        help="対象 CSV（code 列必須。既定: data/watchlist.csv→無ければ liquid30）")
    parser.add_argument("--within", type=int, default=30,
                        help="何日以内のイベントを対象にするか（既定: 30）")
    parser.add_argument("--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）")
    add_source_argument(parser)
    args = parser.parse_args(argv)
    set_default_source(args.source)
    if args.within < 1:
        parser.error("--within には 1 以上を指定してください")

    try:
        universe_path, items = resolve_universe(args.universe)
    except (ValueError, OSError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    today = dt.date.today()
    hits: list[tuple[events.CalendarEvents, str, dt.date, int]] = []
    errors: list[str] = []
    n_ok = 0
    for code, name in items:
        try:
            ev = events.fetch_calendar_events(code, name, synthetic=args.synthetic, asof=today)
        except DataFetchError as exc:
            errors.append(f"{code}: {exc}")
            continue
        n_ok += 1
        for label, date, days in ev.upcoming_events(today, args.within):
            hits.append((ev, label, date, days))

    if n_ok == 0 and not args.synthetic:
        print("エラー: カレンダーイベントを1件も取得できませんでした。"
              "Yahoo への到達性を確認してください。", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        print(f"RESULT events=0 covered=0/{len(items)} data=unavailable")
        return 2

    hits.sort(key=lambda t: t[2])
    content = build_report(hits, universe_path, n_ok, len(items), args.within, errors, args.synthetic)
    print(report.with_disclaimer(content))
    path = report.save_report(content, f"catalyst-{today.isoformat()}.md")
    print(f"レポート: {path}")
    print(f"RESULT events={len(hits)} covered={n_ok}/{len(items)} "
          f"data={'synthetic' if args.synthetic else 'real'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

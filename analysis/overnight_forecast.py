#!/usr/bin/env python3
"""夜間フォーキャスト CLI — 翌営業日の機械予想 → 翌日答え合わせ → 台帳蓄積。

「寝ている間に翌営業日へ向けて予想を出し、翌日その予想の答え合わせをして、
実績を台帳（``forecasts/ledger.csv``）に貯めていく」ループを回すための CLI。
サブコマンド:

    # 翌営業日の予想を生成して台帳に追加（+ reports/forecast-<日付>.md）
    python3 analysis/overnight_forecast.py forecast [--universe CSV] [--synthetic]

    # 台帳の pending 予想を、翌営業日の実績が出たものから採点
    python3 analysis/overnight_forecast.py grade [--synthetic]

    # grade → forecast をまとめて実行（夜間自動実行の本体）
    python3 analysis/overnight_forecast.py run [--universe CSV]

    # 蓄積した台帳から的中率・Brier・較正・銘柄別成績を集計
    python3 analysis/overnight_forecast.py calibration

対象ユニバースの既定は「``data/watchlist.csv`` があればそれ、無ければ
``analysis/universe/liquid30.csv``」。``--universe`` で任意 CSV（``code`` 列必須、
``name`` / ``note`` 列は任意）を指定できる。

自動実行（Routine / cron）向けの機械可読な契約（daily_brief.py に準拠）:

- ``forecast``: 最終行に ``RESULT forecasts=<件数> universe=<成功>/<総数> data=<real|synthetic|unavailable>``。
- ``grade``:    最終行に ``RESULT graded=<採点数> pending=<残保留数> data=<real|synthetic|unavailable>``。
- ``run``:      最終行に ``RESULT graded=<g> forecasts=<f> universe=<成功>/<総数> data=<...>``。
- 実データが1件も取れなかった場合は exit code 2 / ``data=unavailable``（予想・レポート非生成）。
- その他のエラー（CSV 不正等）は exit code 1（RESULT 行なし）。

**予想は機械的なベースラインであり売買助言でも将来の断定でもない**。``--synthetic``
で作った予想は台帳に ``data=synthetic`` と記録され、実データの実績とは採点されない
（自動実行で ``--synthetic`` を使って市況を偽装しないこと。docs/overnight-forecast.md 参照）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

from stocklib import forecast, report
from stocklib.data import (
    REPO_ROOT,
    DataFetchError,
    add_source_argument,
    fetch_prices,
    set_default_source,
)
from stocklib.forecast import DEFAULT_LEDGER, Forecast, ForecastError

DEFAULT_WATCHLIST: Path = REPO_ROOT / "data" / "watchlist.csv"
LIQUID30: Path = REPO_ROOT / "analysis" / "universe" / "liquid30.csv"


# --------------------------------------------------------------------------
# ユニバース解決
# --------------------------------------------------------------------------

def resolve_universe(explicit: Path | None) -> tuple[Path, list[tuple[str, str]]]:
    """(採用したユニバースのパス, [(code, name), ...]) を返す。

    優先順位: ``--universe`` > ``data/watchlist.csv`` > ``analysis/universe/liquid30.csv``。
    CSV は ``code`` 列必須、表示名は ``name`` または ``note`` 列（あれば）を使う。
    """
    if explicit is not None:
        path = explicit
    elif DEFAULT_WATCHLIST.exists():
        path = DEFAULT_WATCHLIST
    else:
        path = LIQUID30
    if not path.exists():
        raise ForecastError(f"ユニバース CSV が見つかりません: {path}")

    df = pd.read_csv(path, comment="#", dtype=str)
    if "code" not in df.columns:
        raise ForecastError(f"ユニバース CSV には code 列が必要です: {path}")
    name_col = "name" if "name" in df.columns else ("note" if "note" in df.columns else None)
    items: list[tuple[str, str]] = []
    for rec in df.to_dict("records"):
        code = str(rec["code"]).strip()
        if not code:
            continue
        name = str(rec.get(name_col, "") or "").strip() if name_col else ""
        items.append((code, name))
    return path, items


# --------------------------------------------------------------------------
# forecast
# --------------------------------------------------------------------------

def _generate_forecasts(
    items: list[tuple[str, str]], period: str, synthetic: bool
) -> tuple[list[Forecast], list[str]]:
    """ユニバース各銘柄の予想を生成する。(予想リスト, 失敗メッセージ) を返す。"""
    forecasts: list[Forecast] = []
    errors: list[str] = []
    data_label = "synthetic" if synthetic else "real"
    for code, name in items:
        try:
            df = fetch_prices(code, period=period, synthetic=synthetic)[code]
        except DataFetchError as exc:
            errors.append(f"{code}: {exc}")
            continue
        try:
            fc = forecast.make_forecast(code, df, name=name, data=data_label)
        except ForecastError as exc:
            errors.append(f"{code}: {exc}")
            continue
        forecasts.append(fc)
    return forecasts, errors


def _forecast_report(
    forecasts: list[Forecast], errors: list[str], universe_path: Path, synthetic: bool
) -> str:
    today = dt.date.today().isoformat()
    lines = [report.report_header(f"翌営業日フォーキャスト（{today}）")]
    lines.append(f"- ユニバース: {universe_path}（{len(forecasts)} 銘柄で予想生成）")
    lines.append(f"- データ出所: {'合成データ' if synthetic else 'yfinance'}")
    if synthetic:
        lines.append(
            "- **データ: 合成データ（--synthetic）による手法デモであり、実際の株価ではありません**"
        )
    if forecasts:
        lines.append(f"- 予想対象日（目安）: {forecasts[0].target_date.isoformat()}")
    lines.append("")
    lines.append("## 翌営業日の機械予想")
    lines.append("")
    rows = []
    for fc in sorted(forecasts, key=lambda f: -f.confidence):
        head = f"{fc.code}" + (f"（{fc.name}）" if fc.name else "")
        rows.append([
            head,
            fc.direction_jp,
            report.fmt_pct(fc.prob_up),
            report.fmt_pct(fc.pred_return),
            f"{report.fmt_num(fc.pred_low)} 〜 {report.fmt_num(fc.pred_high)}",
            report.fmt_pct(fc.confidence),
        ])
    if rows:
        lines.append(report.markdown_table(
            ["銘柄", "方向", "上昇確率", "予想リターン", "予想レンジ(終値)", "信頼度"], rows
        ))
    else:
        lines.append("（予想を生成できた銘柄がありません）")
    lines.append("")
    lines.append(
        "予想は RSI・移動平均の並び・モメンタムを固定重みで合成した機械的ベースライン"
        "（`analysis/stocklib/forecast.py` の `make_forecast` 参照）。重みは過去データに"
        "フィットしておらず、当たり外れは翌日の `grade` と蓄積後の `calibration` で測定する。"
        "**方向・確率は将来の騰落の断定ではない。**"
    )
    lines.append("")
    if errors:
        lines.append("## 取得・生成失敗")
        lines.extend(f"- {e}" for e in errors)
        lines.append("")
    return "\n".join(lines)


def cmd_forecast(args: argparse.Namespace) -> int:
    try:
        universe_path, items = resolve_universe(args.universe)
    except (ForecastError, ValueError, OSError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    forecasts, errors = _generate_forecasts(items, args.period, args.synthetic)

    if not forecasts and not args.synthetic:
        # 実データが1件も予想に至らなかった → data=unavailable / exit 2
        print(f"エラー: 実データを1件も取得・予想できませんでした（ユニバース {len(items)} 銘柄）。"
              "ネットワーク/Yahoo Finance 到達性を確認してください。", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        print(f"RESULT forecasts=0 universe=0/{len(items)} data=unavailable")
        return 2

    made_on = dt.date.today()
    ledger = forecast.load_ledger(args.ledger)
    for fc in forecasts:
        ledger = forecast.upsert_forecast(ledger, fc, made_on)
    forecast.save_ledger(ledger, args.ledger)

    content = _forecast_report(forecasts, errors, universe_path, args.synthetic)
    print(content)
    path = report.save_report(content, f"forecast-{made_on.isoformat()}.md")
    print(f"レポート: {path}")
    print(f"台帳: {args.ledger}")
    data = "synthetic" if args.synthetic else "real"
    print(f"RESULT forecasts={len(forecasts)} universe={len(forecasts)}/{len(items)} data={data}")
    return 0


# --------------------------------------------------------------------------
# grade
# --------------------------------------------------------------------------

def _grade_pending(
    ledger: pd.DataFrame, period: str, synthetic: bool
) -> tuple[pd.DataFrame, list[forecast.GradeResult], list[str], int, int, int]:
    """pending 行を採点する。

    Returns:
        (更新後台帳, 採点結果, 失敗メッセージ, 残pending数,
         価格取得を試みた銘柄数, 価格取得に成功した銘柄数)。
        末尾2つは「実データ全滅（data=unavailable）」の判定に使う。
    """
    data = "synthetic" if synthetic else "real"
    pend = forecast.pending_rows(ledger, data=data)
    graded: list[forecast.GradeResult] = []
    errors: list[str] = []
    graded_on = dt.date.today()
    # 銘柄ごとに1回だけ価格取得（同銘柄の複数 asof をまとめて採点）。
    price_cache: dict[str, pd.DataFrame | None] = {}
    for row in pend.to_dict("records"):
        code = str(row["code"])
        if code not in price_cache:
            try:
                price_cache[code] = fetch_prices(code, period=period, synthetic=synthetic)[code]
            except DataFetchError as exc:
                price_cache[code] = None
                errors.append(f"{code}: {exc}")
        future_df = price_cache[code]
        if future_df is None:
            continue
        fc = forecast.row_to_forecast(row)
        try:
            result = forecast.grade_forecast(fc, future_df)
        except ForecastError as exc:
            errors.append(f"{fc.forecast_id}: {exc}")
            continue
        if result is None:
            continue  # 翌営業日の実データがまだ無い → 保留のまま
        ledger = forecast.apply_grade(ledger, result, graded_on)
        graded.append(result)
    remaining = len(forecast.pending_rows(ledger, data=data))
    n_codes = len(price_cache)
    n_fetch_ok = sum(1 for v in price_cache.values() if v is not None)
    return ledger, graded, errors, remaining, n_codes, n_fetch_ok


def _grade_report(
    ledger: pd.DataFrame, graded: list[forecast.GradeResult], errors: list[str], synthetic: bool
) -> str:
    today = dt.date.today().isoformat()
    lines = [report.report_header(f"フォーキャスト答え合わせ（{today}）")]
    lines.append(f"- データ出所: {'合成データ' if synthetic else 'yfinance'}")
    if synthetic:
        lines.append("- **データ: 合成データによる手法デモであり実際の株価ではありません**")
    lines.append(f"- 今回の採点件数: {len(graded)}")
    lines.append("")
    if graded:
        by_id = {g.forecast_id: g for g in graded}
        gmap = ledger.set_index("forecast_id")
        rows = []
        for gid, g in by_id.items():
            r = gmap.loc[gid]
            head = f"{r['code']}" + (f"（{r['name']}）" if isinstance(r["name"], str) and r["name"] else "")
            rows.append([
                head,
                forecast._DIRECTION_JP.get(str(r["direction"]), str(r["direction"])),
                report.fmt_pct(float(r["prob_up"])),
                report.fmt_pct(g.actual_return),
                "○" if g.dir_hit else "×",
                "○" if g.in_range else "×",
                report.fmt_num(g.brier, 3),
            ])
        lines.append("## 今回採点した予想")
        lines.append("")
        lines.append(report.markdown_table(
            ["銘柄", "予想方向", "上昇確率", "実績リターン", "方向的中", "レンジ的中", "Brier"], rows
        ))
        lines.append("")
    else:
        lines.append("（翌営業日の実績が揃った未採点予想はありませんでした）")
        lines.append("")

    # 実行モード（real/synthetic）と同じ data の採点行のみを集計する（混在防止）。
    summary = forecast.summarize(ledger, data="synthetic" if synthetic else "real")
    if summary.n_graded:
        lines.append(f"## 累積成績（採点済み全件・{'合成' if synthetic else '実データ'}）")
        lines.append("")
        lines.append(_summary_lines(summary))
        lines.append("")
    if errors:
        lines.append("## 取得・採点失敗")
        lines.extend(f"- {e}" for e in errors)
        lines.append("")
    return "\n".join(lines)


def cmd_grade(args: argparse.Namespace) -> int:
    ledger = forecast.load_ledger(args.ledger)
    data = "synthetic" if args.synthetic else "real"
    n_pending_before = len(forecast.pending_rows(ledger, data=data))
    if n_pending_before == 0:
        print("採点対象（pending）の予想がありません。先に forecast を実行してください。")
        print(f"RESULT graded=0 pending=0 data={data}")
        return 0

    ledger, graded, errors, remaining, n_codes, n_fetch_ok = _grade_pending(
        ledger, args.period, args.synthetic
    )

    # pending 銘柄の価格を1件も取得できなかった（実データ全滅）→ unavailable / exit 2
    if not args.synthetic and n_codes > 0 and n_fetch_ok == 0:
        print("エラー: 採点用の実データを取得できませんでした。", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        print(f"RESULT graded=0 pending={remaining} data=unavailable")
        return 2

    forecast.save_ledger(ledger, args.ledger)
    content = _grade_report(ledger, graded, errors, args.synthetic)
    print(content)
    path = report.save_report(content, f"forecast-grade-{dt.date.today().isoformat()}.md")
    print(f"レポート: {path}")
    print(f"RESULT graded={len(graded)} pending={remaining} data={data}")
    return 0


# --------------------------------------------------------------------------
# run（grade → forecast）
# --------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    # 1) 前回予想の答え合わせ（実データが無ければ静かに保留のまま進む）
    ledger = forecast.load_ledger(args.ledger)
    data = "synthetic" if args.synthetic else "real"
    ledger, graded, grade_errors, remaining, _n_codes, _n_ok = _grade_pending(
        ledger, args.period, args.synthetic
    )
    forecast.save_ledger(ledger, args.ledger)

    # 2) 翌営業日の予想生成
    try:
        universe_path, items = resolve_universe(args.universe)
    except (ForecastError, ValueError, OSError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    forecasts, fc_errors = _generate_forecasts(items, args.period, args.synthetic)

    if not forecasts and not args.synthetic:
        print("エラー: 実データを1件も予想できませんでした（run）。ネットワーク到達性を確認してください。",
              file=sys.stderr)
        for e in fc_errors:
            print(f"  {e}", file=sys.stderr)
        print(f"RESULT graded={len(graded)} forecasts=0 universe=0/{len(items)} data=unavailable")
        return 2

    made_on = dt.date.today()
    for fc in forecasts:
        ledger = forecast.upsert_forecast(ledger, fc, made_on)
    forecast.save_ledger(ledger, args.ledger)

    # レポート（答え合わせ + 新規予想を1本にまとめる）
    grade_content = _grade_report(ledger, graded, grade_errors, args.synthetic)
    fc_content = _forecast_report(forecasts, fc_errors, universe_path, args.synthetic)
    content = grade_content + "\n\n---\n\n" + fc_content
    print(content)
    path = report.save_report(content, f"forecast-run-{made_on.isoformat()}.md")
    print(f"レポート: {path}")
    print(f"台帳: {args.ledger}")
    print(
        f"RESULT graded={len(graded)} forecasts={len(forecasts)} "
        f"universe={len(forecasts)}/{len(items)} data={data}"
    )
    return 0


# --------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------

def _summary_lines(summary: forecast.Summary) -> str:
    rows = [
        ["採点済み件数", str(summary.n_graded)],
        ["方向予想件数（flat除く）", str(summary.n_directional)],
        ["方向的中率", report.fmt_pct(summary.dir_hit_rate)],
        ["レンジ的中率", report.fmt_pct(summary.range_hit_rate)],
        ["平均 Brier（低いほど良い）", report.fmt_num(summary.mean_brier, 4)],
        ["無情報 Brier（p=0.5 基準）", report.fmt_num(summary.baseline_brier, 4)],
        ["平均予想リターン誤差(MAE)", report.fmt_pct(summary.mean_abs_error)],
    ]
    return report.markdown_table(["指標", "値"], rows)


def cmd_calibration(args: argparse.Namespace) -> int:
    ledger = forecast.load_ledger(args.ledger)
    summary = forecast.summarize(ledger)
    today = dt.date.today().isoformat()
    lines = [report.report_header(f"フォーキャスト較正レポート（{today}）")]
    lines.append(f"- 台帳: {args.ledger}")
    lines.append("")
    if summary.n_graded == 0:
        lines.append("採点済みの予想がまだありません。forecast → （翌日）grade を回して蓄積してください。")
        content = "\n".join(lines)
        print(content)
        path = report.save_report(content, f"forecast-calibration-{today}.md")
        print(f"レポート: {path}")
        print("RESULT graded=0 data=real")
        return 0

    lines.append("## 累積成績")
    lines.append("")
    lines.append(_summary_lines(summary))
    lines.append("")

    if summary.per_direction:
        lines.append("## 予想方向別の方向的中率")
        lines.append("")
        rows = [
            [forecast._DIRECTION_JP.get(k, k), str(n), report.fmt_pct(hit)]
            for k, (n, hit) in summary.per_direction.items()
        ]
        lines.append(report.markdown_table(["予想方向", "件数", "的中率"], rows))
        lines.append("")

    calib = forecast.calibration_table(ledger)
    if calib:
        lines.append("## 上昇確率の較正（予想確率 vs 実際の上昇頻度）")
        lines.append("")
        rows = [
            [c["bucket"], str(c["n"]), report.fmt_pct(c["mean_prob_up"]),
             report.fmt_pct(c["realized_up_freq"])]
            for c in calib
        ]
        lines.append(report.markdown_table(
            ["上昇確率ビン", "件数", "平均予想確率", "実際の上昇頻度"], rows
        ))
        lines.append("")
        lines.append(
            "較正が取れていれば「平均予想確率 ≒ 実際の上昇頻度」になる。"
            "件数が少ないビンの数値は偶然で大きく振れる点に注意。"
        )
        lines.append("")

    per_code = forecast.per_code_hit_rate(ledger)
    if per_code:
        lines.append("## 銘柄別の方向的中率（flat除く、件数降順）")
        lines.append("")
        rows = [
            [c["code"], str(c["n"]), report.fmt_pct(c["dir_hit_rate"]),
             report.fmt_num(c["mean_brier"], 4)]
            for c in per_code
        ]
        lines.append(report.markdown_table(["銘柄", "件数", "方向的中率", "平均Brier"], rows))
        lines.append("")

    lines.append(
        "注: サンプルが少ないうちの的中率・較正は統計的に不安定（偶然の範囲が広い）。"
        "方向的中率は方向予想（up/down）のみを母数とし、flat 予想は方向的中率の分母に含めない。"
        "予想はテクニカルの機械合成であり、市場環境が変われば過去の的中率は将来を保証しない。"
    )
    lines.append("")
    content = "\n".join(lines)
    print(content)
    path = report.save_report(content, f"forecast-calibration-{today}.md")
    print(f"レポート: {path}")
    print(f"RESULT graded={summary.n_graded} data=real")
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="翌営業日の機械予想・答え合わせ・実績台帳（夜間フォーキャスト）",
    )
    parser.add_argument(
        "--ledger", type=Path, default=DEFAULT_LEDGER,
        help=f"実績台帳 CSV（既定: {DEFAULT_LEDGER}。主にテスト用）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_common(p: argparse.ArgumentParser, *, with_universe: bool) -> None:
        p.add_argument("--period", default="1y",
                       help="価格取得期間（既定: 1y。75日線・モメンタムに 1y 以上を推奨）")
        p.add_argument("--synthetic", action="store_true",
                       help="合成データで実行（ネットワーク不要。台帳に data=synthetic と記録）")
        add_source_argument(p)
        if with_universe:
            p.add_argument("--universe", type=Path, default=None,
                           help="対象ユニバース CSV（code 列必須。既定: data/watchlist.csv"
                                "→無ければ liquid30.csv）")

    p_fc = sub.add_parser("forecast", help="翌営業日の予想を生成し台帳に追加する")
    _add_common(p_fc, with_universe=True)
    p_fc.set_defaults(func=cmd_forecast)

    p_gr = sub.add_parser("grade", help="台帳の pending 予想を実績で採点する")
    _add_common(p_gr, with_universe=False)
    p_gr.set_defaults(func=cmd_grade)

    p_run = sub.add_parser("run", help="grade → forecast をまとめて実行（夜間自動実行の本体）")
    _add_common(p_run, with_universe=True)
    p_run.set_defaults(func=cmd_run)

    p_cal = sub.add_parser("calibration", help="蓄積台帳から的中率・Brier・較正を集計する")
    p_cal.set_defaults(func=cmd_calibration)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    set_default_source(getattr(args, "source", None))
    try:
        return int(args.func(args))
    except (ForecastError, DataFetchError, ValueError, OSError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

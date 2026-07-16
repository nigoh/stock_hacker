#!/usr/bin/env python3
"""ウォッチリストのデイリーブリーフ CLI。

使い方（リポジトリルートから）:
    python3 analysis/daily_brief.py [--watchlist data/watchlist.csv] [--period 1y] [--synthetic]

市況（^N225・1306.T・USDJPY=X・^GSPC の前日比・5日・1ヶ月リターン）と、
ウォッチリスト各銘柄の現在値・前日比・検出シグナル（stocklib.signals）をまとめて
stdout に出力し、reports/brief-<日付>.md にも保存する。
ウォッチリスト CSV（列: code,note）が無い場合は
analysis/templates/watchlist-example.csv を案内して市況のみで続行する。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

from stocklib import report, signals
from stocklib.data import REPO_ROOT, DataFetchError, fetch_prices

DEFAULT_WATCHLIST: Path = REPO_ROOT / "data" / "watchlist.csv"
TEMPLATE_WATCHLIST: Path = Path(__file__).resolve().parent / "templates" / "watchlist-example.csv"

# 市況セクションの対象（ティッカー, 表示名）
MARKET_TICKERS: tuple[tuple[str, str], ...] = (
    ("^N225", "日経平均"),
    ("1306", "TOPIX連動ETF"),
    ("USDJPY=X", "ドル円"),
    ("^GSPC", "S&P500"),
)

_DIRECTION_LABEL: dict[str, str] = {"bullish": "強気", "bearish": "弱気", "neutral": "中立"}


def _lagged_return(close: pd.Series, lag: int) -> float:
    """直近値の ``lag`` 営業日前比リターン。データ不足は ``nan``。"""
    if len(close) <= lag:
        return float("nan")
    return float(close.iloc[-1] / close.iloc[-1 - lag] - 1.0)


def build_market_section(period: str, synthetic: bool) -> tuple[list[str], list[str], int]:
    """市況セクションの行リスト・取得失敗リスト・取得成功数を返す。"""
    rows: list[list[object]] = []
    errors: list[str] = []
    for ticker, label in MARKET_TICKERS:
        try:
            df = fetch_prices(ticker, period=period, synthetic=synthetic)[ticker]
        except DataFetchError as exc:
            errors.append(f"{ticker}: {exc}")
            continue
        close = df["Close"]
        rows.append([
            f"{label}（{ticker}）",
            report.fmt_num(float(close.iloc[-1])),
            report.fmt_pct(_lagged_return(close, 1)),
            report.fmt_pct(_lagged_return(close, 5)),
            report.fmt_pct(_lagged_return(close, 21)),
        ])
    lines: list[str] = ["## 市況", ""]
    if rows:
        lines.append(report.markdown_table(["指標", "直近値", "前日比", "5日", "1ヶ月"], rows))
        lines.append("")
        lines.append(
            "注: TOPIX そのものは yfinance で取得しづらいため 1306.T（TOPIX連動ETF）で代替。"
            "^GSPC（S&P500）は日本時間から見て前営業日終値ベース。「1ヶ月」は21営業日前比。"
        )
    else:
        lines.append("（市況データを取得できませんでした）")
    lines.append("")
    return lines, errors, len(rows)


def load_watchlist(path: Path) -> pd.DataFrame:
    """ウォッチリスト CSV（列: code,note。``#`` 行はコメント、note 列は省略可）を読み込む。"""
    df = pd.read_csv(path, comment="#", dtype={"code": str})
    if "code" not in df.columns:
        raise ValueError(f"ウォッチリスト CSV には code 列が必要です: {path}")
    if "note" not in df.columns:
        df["note"] = ""
    df["note"] = df["note"].fillna("")
    return df


def build_watchlist_section(
    watchlist: pd.DataFrame, period: str, synthetic: bool
) -> tuple[list[str], list[str], int]:
    """ウォッチリストセクションの行リスト・取得失敗リスト・取得成功数を返す。

    シグナルの無い銘柄は1行、シグナルのある銘柄は詳細をサブ項目で列挙する。
    """
    lines: list[str] = ["## ウォッチリスト", ""]
    errors: list[str] = []
    n_ok = 0
    for rec in watchlist.to_dict("records"):
        code = str(rec["code"]).strip()
        note = str(rec.get("note", "") or "").strip()
        try:
            df = fetch_prices(code, period=period, synthetic=synthetic)[code]
        except DataFetchError as exc:
            errors.append(f"{code}: {exc}")
            continue
        n_ok += 1
        close = df["Close"]
        head = f"**{code}**" + (f"（{note}）" if note else "")
        summary = (
            f"終値 {report.fmt_num(float(close.iloc[-1]))}、"
            f"前日比 {report.fmt_pct(_lagged_return(close, 1))}"
        )
        detected = signals.detect_signals(df)
        if not detected:
            lines.append(f"- {head}: {summary} — シグナルなし")
            continue
        lines.append(f"- {head}: {summary} — シグナル {len(detected)} 件")
        for sig in detected:
            lines.append(f"  - [{_DIRECTION_LABEL.get(sig.direction, sig.direction)}] {sig.detail}")
    lines.append("")
    lines.append(
        "シグナル定義: RSI(14) 30以下/70以上、25日/75日線クロス（5営業日以内）、"
        "出来高が20日平均の2倍超、52週高値/安値から3%以内、前日比±3%超。"
        "数式・閾値の詳細は `analysis/stocklib/signals.py` を参照。"
        "方向ラベルは教科書的な解釈であり、将来の騰落の予測ではない。"
    )
    lines.append("")
    return lines, errors, n_ok


def build_report(watchlist_path: Path, period: str, synthetic: bool) -> tuple[str, list[str]]:
    """ブリーフ本文（Markdown）と会話向けの通知メッセージのリストを構築する。"""
    notices: list[str] = []
    lines: list[str] = [report.report_header(f"デイリーブリーフ（{dt.date.today().isoformat()}）")]
    lines.append(f"- 期間データ: {period}（出所: {'合成データ' if synthetic else 'yfinance'}）")
    if synthetic:
        lines.append(
            "- **データ: 合成データ（--synthetic）による手法デモであり、実際の市況・株価ではありません**"
        )
    lines.append("")

    market_lines, errors, n_market = build_market_section(period, synthetic)
    lines.extend(market_lines)

    n_watch = 0
    if watchlist_path.exists():
        watchlist = load_watchlist(watchlist_path)
        watch_lines, watch_errors, n_watch = build_watchlist_section(watchlist, period, synthetic)
        lines.extend(watch_lines)
        errors.extend(watch_errors)
    else:
        guidance = (
            f"ウォッチリスト {watchlist_path} が見つかりません。"
            f"{TEMPLATE_WATCHLIST.relative_to(REPO_ROOT)} を data/watchlist.csv に"
            "コピーして編集してください（data/ は git 管理外）。市況のみで続行します。"
        )
        notices.append(guidance)
        lines.extend(["## ウォッチリスト", "", f"（未設定）{guidance}", ""])

    if errors:
        lines.append("## 取得失敗")
        lines.extend(f"- {e}" for e in errors)
        lines.append("")

    if n_market + n_watch == 0:
        raise DataFetchError(
            "市況・ウォッチリストとも1件もデータを取得できませんでした"
            "（ネットワークを確認するか、--synthetic を付けて再実行してください）"
        )
    return "\n".join(lines), notices


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="市況とウォッチリスト銘柄のシグナルをまとめたデイリーブリーフを作成する"
    )
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST,
                        help=f"ウォッチリスト CSV（列: code,note。既定: {DEFAULT_WATCHLIST}）")
    parser.add_argument("--period", default="1y",
                        help="取得期間（既定: 1y。52週高安・75日線の判定には 1y 以上を推奨）")
    parser.add_argument("--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）")
    args = parser.parse_args(argv)

    try:
        content, notices = build_report(args.watchlist, args.period, args.synthetic)
    except (DataFetchError, ValueError, OSError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    for notice in notices:
        print(f"注意: {notice}")
    print(content)
    filename = f"brief-{dt.date.today().isoformat()}.md"
    path = report.save_report(content, filename)
    print(f"レポート: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

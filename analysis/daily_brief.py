#!/usr/bin/env python3
"""ウォッチリストのデイリーブリーフ CLI。

使い方（リポジトリルートから）:
    python3 analysis/daily_brief.py [--watchlist data/watchlist.csv] [--period 1y]
                                    [--synthetic] [--max-alerts N]
                                    [--in-currency USD|EUR|GBP]

市況（^N225・1306.T・USDJPY=X・^GSPC の前日比・5日・1ヶ月リターン）と、
ウォッチリスト各銘柄の現在値・前日比・検出シグナル（stocklib.signals）をまとめて
stdout に出力し、reports/brief-<日付>.md にも保存する。
ウォッチリスト CSV（列: code,note）が無い場合は
analysis/templates/watchlist-example.csv を案内して市況のみで続行する。

--in-currency USD|EUR|GBP（--in-usd は --in-currency USD の後方互換エイリアス）を
指定すると、市況テーブルに基準通貨建て ^N225 の行（前日比・5日・1ヶ月を基準通貨建てで
算出）を併記する（海外投資家の定点観測向け。円建て終値を同日のクロス円終値で除した
同日終値換算・為替ヘッジなしの近似。stocklib.currency を利用）。
このオプションは下記の自動実行契約（RESULT 行・exit code）を一切変更しない。
為替レートの取得に失敗した場合は基準通貨建て行を省略して「取得失敗」節に記録するのみで、
``data=`` 判定・``watch=`` の分子分母・exit code には影響しない。

自動実行（Routine / cron）向けの機械可読な契約:

- stdout の最終行に
  ``RESULT signals=<検出シグナル総数> watch=<取得成功数>/<ウォッチリスト総数> data=<real|synthetic|unavailable>``
  を出力する（signals は --max-alerts による表示絞り込み前の総数）。
- ``watch`` はウォッチリスト銘柄の取得成功数/総数。``signals=0`` が
  「全銘柄を監視して変化なし」なのか「銘柄を取得できず監視に穴が開いた」のかを
  区別するためのフィールド。ウォッチリスト未設定（ファイルなし）は ``watch=0/0``、
  部分失敗（成功数 < 総数）でも市況か銘柄のどれかが取得できていれば
  ``data=real`` のまま継続する（失敗銘柄はレポートの「取得失敗」節に列挙）。
- 実データ取得が全滅した場合（--synthetic なし）は exit code 2 /
  ``data=unavailable``（このとき ``watch=0/<総数>``）。部分的にでも取得できれば
  取得分で継続し exit 0 / ``data=real``。
- その他のエラー（CSV 不正等）は従来どおり exit 1（RESULT 行なし）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

from stocklib import currency, report, signals
from stocklib.data import (
    REPO_ROOT,
    DataFetchError,
    add_source_argument,
    fetch_prices,
    set_default_source,
)

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

# --max-alerts の絞り込みで用いるシグナル種別の優先度（小さいほど優先）。
# 急変動・出来高急増は「その日に何かが起きた」ことを示すため、
# 継続的な状態を示す 52週高安・RSI より優先する。
_ALERT_PRIORITY: dict[str, int] = {
    "price_move": 0,
    "volume": 1,
    "ma_cross": 2,
    "macd": 3,
    "week52": 4,
    "bollinger": 5,
    "rsi": 6,
    "adx": 7,
}
_ALERT_PRIORITY_LABEL: str = (
    "急変動 > 出来高急増 > 移動平均クロス > MACDクロス > 52週高安 > ボリンジャー > RSI > ADX"
)


class BriefUnavailableError(DataFetchError):
    """実データが1件も取得できなかったことを示すエラー。

    ``watch_total`` にウォッチリスト総数を保持し、呼び出し側が
    ``RESULT ... watch=0/<総数> data=unavailable`` を組み立てられるようにする。
    """

    def __init__(self, message: str, watch_total: int) -> None:
        super().__init__(message)
        self.watch_total = watch_total


def _lagged_return(close: pd.Series, lag: int) -> float:
    """直近値の ``lag`` 営業日前比リターン。データ不足は ``nan``。"""
    if len(close) <= lag:
        return float("nan")
    return float(close.iloc[-1] / close.iloc[-1 - lag] - 1.0)


def build_market_section(
    period: str, synthetic: bool, in_currency: str | None = None
) -> tuple[list[str], list[str], int]:
    """市況セクションの行リスト・取得失敗リスト・取得成功数を返す。

    ``in_currency``（``"USD"`` / ``"EUR"`` / ``"GBP"``）を指定すると、円建て ^N225 の
    直下に基準通貨建て ^N225 の行（前日比・5日・1ヶ月を基準通貨建て系列で算出）を
    併記する（``stocklib.currency.to_base_series``、同日終値換算・為替ヘッジなしの近似）。
    為替の取得に失敗した場合は行を省略して取得失敗リストに記録するのみで、
    戻り値の取得成功数（自動実行契約の ``data=`` 判定に使われる）には為替の成否を含めない。
    """
    rows: list[list[object]] = []
    errors: list[str] = []
    n225_close: pd.Series | None = None
    n225_row_idx: int | None = None
    for ticker, label in MARKET_TICKERS:
        try:
            df = fetch_prices(ticker, period=period, synthetic=synthetic)[ticker]
        except DataFetchError as exc:
            errors.append(f"{ticker}: {exc}")
            continue
        close = df["Close"]
        if ticker == "^N225":
            n225_close = close
            n225_row_idx = len(rows)
        rows.append([
            f"{label}（{ticker}）",
            report.fmt_num(float(close.iloc[-1])),
            report.fmt_pct(_lagged_return(close, 1)),
            report.fmt_pct(_lagged_return(close, 5)),
            report.fmt_pct(_lagged_return(close, 21)),
        ])
    n_market = len(rows)  # 基準通貨建て行を数える前に確定（RESULT 契約の data= 判定用）

    fx_note: str | None = None
    if in_currency is not None and n225_close is not None and n225_row_idx is not None:
        ccy_label = currency.currency_label(in_currency)
        fx_ticker = currency.get_fx_ticker(in_currency)
        try:
            fx_df = currency.fetch_fx(in_currency, period, synthetic=synthetic)
            base_close = currency.to_base_series(n225_close, fx_df["Close"])
        except (DataFetchError, ValueError) as exc:
            errors.append(f"{fx_ticker}: {exc}（{ccy_label}建て行は省略）")
        else:
            rows.insert(n225_row_idx + 1, [
                f"日経平均（^N225、{ccy_label}建て）",
                report.fmt_num(float(base_close.iloc[-1])),
                report.fmt_pct(_lagged_return(base_close, 1)),
                report.fmt_pct(_lagged_return(base_close, 5)),
                report.fmt_pct(_lagged_return(base_close, 21)),
            ])
            fx_note = (
                f"{ccy_label}建て ^N225 は円建て終値を同日の {fx_ticker} 終値"
                f"（1{ccy_label}あたり円）で除した換算（同日終値換算・為替ヘッジなしの近似。"
                f"恒等式 (1+r_{in_currency})=(1+r_JPY)/(1+r_FX) に従う）。"
            )

    lines: list[str] = ["## 市況", ""]
    if rows:
        lines.append(report.markdown_table(["指標", "直近値", "前日比", "5日", "1ヶ月"], rows))
        lines.append("")
        note = (
            "注: TOPIX そのものは yfinance で取得しづらいため 1306.T（TOPIX連動ETF）で代替。"
            "^GSPC（S&P500）は日本時間から見て前営業日終値ベース。「1ヶ月」は21営業日前比。"
        )
        if fx_note is not None:
            note += fx_note
        lines.append(note)
    else:
        lines.append("（市況データを取得できませんでした）")
    lines.append("")
    return lines, errors, n_market


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
    watchlist: pd.DataFrame, period: str, synthetic: bool, max_alerts: int | None = None
) -> tuple[list[str], list[str], int, int]:
    """ウォッチリストセクションの行リスト・取得失敗リスト・取得成功数・検出シグナル総数を返す。

    シグナルの無い銘柄は1行、シグナルのある銘柄は詳細をサブ項目で列挙する。
    ``max_alerts`` を指定し検出シグナル総数がそれを超えた場合は、種別優先度
    （:data:`_ALERT_PRIORITY`）の上位 ``max_alerts`` 件のみ詳細を表示する
    （各銘柄の見出しの件数と戻り値の総数は絞り込み前の値のまま）。
    """
    errors: list[str] = []
    entries: list[tuple[str, str, list[signals.Signal]]] = []
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
        entries.append((head, summary, signals.detect_signals(df)))

    n_signals = sum(len(detected) for _, _, detected in entries)
    shown: set[tuple[int, int]] | None = None
    if max_alerts is not None and n_signals > max_alerts:
        ranked = sorted(
            (
                (i, j, sig)
                for i, (_, _, detected) in enumerate(entries)
                for j, sig in enumerate(detected)
            ),
            key=lambda item: (_ALERT_PRIORITY.get(item[2].kind, 99), item[0], item[1]),
        )
        shown = {(i, j) for i, j, _ in ranked[:max_alerts]}

    lines: list[str] = ["## ウォッチリスト", ""]
    for i, (head, summary, detected) in enumerate(entries):
        if not detected:
            lines.append(f"- {head}: {summary} — シグナルなし")
            continue
        lines.append(f"- {head}: {summary} — シグナル {len(detected)} 件")
        n_omitted = 0
        for j, sig in enumerate(detected):
            if shown is not None and (i, j) not in shown:
                n_omitted += 1
                continue
            lines.append(f"  - [{_DIRECTION_LABEL.get(sig.direction, sig.direction)}] {sig.detail}")
        if n_omitted:
            lines.append(f"  - （他 {n_omitted} 件は --max-alerts により表示省略）")
    lines.append("")
    if shown is not None:
        lines.append(
            f"注: --max-alerts {max_alerts} 指定のため、検出シグナル {n_signals} 件のうち"
            f"優先度上位 {max_alerts} 件のみ詳細表示（優先度: {_ALERT_PRIORITY_LABEL}）。"
        )
        lines.append("")
    lines.append(
        "シグナル定義: RSI(14) 30以下/70以上、25日/75日線クロス（5営業日以内）、"
        "出来高が20日平均の2倍超、52週高値/安値から3%以内、前日比±3%超、"
        "MACD(12,26,9)のシグナル線クロス（5営業日以内）、ボリンジャー±2σ(20日)逸脱、"
        "ADX(14)≥25のトレンド方向。"
        "数式・閾値の詳細は `analysis/stocklib/signals.py` を参照。"
        "方向ラベルは教科書的な解釈であり、将来の騰落の予測ではない。"
    )
    lines.append("")
    return lines, errors, n_ok, n_signals


def build_report(
    watchlist_path: Path,
    period: str,
    synthetic: bool,
    max_alerts: int | None = None,
    in_currency: str | None = None,
) -> tuple[str, list[str], int, int, int]:
    """ブリーフ本文（Markdown）・会話向け通知メッセージ・検出シグナル総数・
    ウォッチリスト取得成功数・ウォッチリスト総数を構築する。

    ウォッチリスト未設定（ファイルなし）のとき成功数・総数はともに 0。
    ``in_currency`` は市況テーブルに基準通貨建て ^N225 行を併記するのみで、
    RESULT 行・exit code の自動実行契約には影響しない
    （:func:`build_market_section` を参照）。

    Raises:
        BriefUnavailableError: 市況・ウォッチリストとも1件もデータを取得できなかった場合
            （呼び出し側は ``data=unavailable`` / exit code 2 として扱う。
            ``watch_total`` 属性にウォッチリスト総数を保持する）。
    """
    notices: list[str] = []
    lines: list[str] = [report.report_header(f"デイリーブリーフ（{dt.date.today().isoformat()}）")]
    lines.append(f"- 期間データ: {period}（出所: {'合成データ' if synthetic else 'yfinance'}）")
    if synthetic:
        lines.append(
            "- **データ: 合成データ（--synthetic）による手法デモであり、実際の市況・株価ではありません**"
        )
    if in_currency is not None:
        lines.append(
            f"- 基準通貨併記: {in_currency}"
            f"（市況の ^N225 を{currency.currency_label(in_currency)}建てでも表示。海外投資家視点）"
        )
    lines.append("")

    market_lines, errors, n_market = build_market_section(period, synthetic, in_currency)
    lines.extend(market_lines)

    n_watch = 0
    n_watch_total = 0
    n_signals = 0
    if watchlist_path.exists():
        watchlist = load_watchlist(watchlist_path)
        n_watch_total = len(watchlist)
        watch_lines, watch_errors, n_watch, n_signals = build_watchlist_section(
            watchlist, period, synthetic, max_alerts
        )
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
        raise BriefUnavailableError(
            "市況・ウォッチリストとも実データを1件も取得できませんでした。対処:\n"
            "  価格・基本情報は標準 requests による Yahoo API 直叩き（stocklib.data）で取得します。"
            "取れない場合は Yahoo（query1/2.finance.yahoo.com）への到達がネットワークポリシーで"
            "許可されているか、ローカルなら接続を確認してください"
            "（J-Quants Free は12週間遅延のため当日シグナル検出の代替にはなりません）。\n"
            "  手法デモが目的の場合のみ --synthetic で動作しますが、その出力は実際の市況・"
            "株価ではありません。",
            watch_total=n_watch_total,
        )
    return "\n".join(lines), notices, n_signals, n_watch, n_watch_total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="市況とウォッチリスト銘柄のシグナルをまとめたデイリーブリーフを作成する"
    )
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST,
                        help=f"ウォッチリスト CSV（列: code,note。既定: {DEFAULT_WATCHLIST}）")
    parser.add_argument("--period", default="1y",
                        help="取得期間（既定: 1y。52週高安・75日線の判定には 1y 以上を推奨）")
    parser.add_argument("--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）")
    add_source_argument(parser)
    parser.add_argument("--max-alerts", type=int, default=None, metavar="N",
                        help="シグナル詳細の表示を種別優先度の上位 N 件に絞る"
                             f"（優先度: {_ALERT_PRIORITY_LABEL}。既定: 制限なし）")
    parser.add_argument(
        "--in-currency",
        type=str.upper,
        choices=sorted(currency.SUPPORTED_CURRENCIES),
        default=None,
        help="市況テーブルに基準通貨建て ^N225 行を併記する（海外投資家視点、クロス円レートで"
        "換算。RESULT 行・exit code の自動実行契約には影響しない）",
    )
    parser.add_argument(
        "--in-usd",
        action="store_true",
        help="--in-currency USD のエイリアス（後方互換）",
    )
    args = parser.parse_args(argv)
    set_default_source(args.source)
    if args.max_alerts is not None and args.max_alerts < 1:
        parser.error("--max-alerts には 1 以上の整数を指定してください")
    in_currency: str | None = args.in_currency or ("USD" if args.in_usd else None)

    try:
        content, notices, n_signals, n_watch, n_watch_total = build_report(
            args.watchlist, args.period, args.synthetic, args.max_alerts,
            in_currency=in_currency,
        )
    except DataFetchError as exc:
        # 実データ全滅（--synthetic では発生しない）。自動実行側が判別できるよう
        # RESULT 行に data=unavailable を出し、exit code 2 で区別する。
        print(f"エラー: {exc}", file=sys.stderr)
        watch_total = exc.watch_total if isinstance(exc, BriefUnavailableError) else 0
        print(f"RESULT signals=0 watch=0/{watch_total} data=unavailable")
        return 2
    except (ValueError, OSError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    for notice in notices:
        print(f"注意: {notice}")
    print(content)
    filename = f"brief-{dt.date.today().isoformat()}.md"
    path = report.save_report(content, filename)
    print(f"レポート: {path}")
    print(
        f"RESULT signals={n_signals} watch={n_watch}/{n_watch_total} "
        f"data={'synthetic' if args.synthetic else 'real'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

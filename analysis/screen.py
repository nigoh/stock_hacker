#!/usr/bin/env python3
"""ユニバース銘柄のスクリーニング CLI。

使い方（リポジトリルートから）:
    python3 analysis/screen.py [--universe analysis/universe/liquid30.csv]
        [--rsi-below 30] [--rsi-above 70]
        [--price-above-sma 200] [--price-below-sma 200]
        [--volume-surge 2.0] [--return-below -10] [--return-above 10]
        [--per-below 15] [--pbr-below 1.0] [--dividend-yield-above 3.0]
        [--period 1y] [--synthetic] [--in-currency USD|EUR|GBP]

結果テーブルを stdout に出力し、reports/screen-<日付>.md も生成する。

- --in-currency USD|EUR|GBP（--in-usd は --in-currency USD の後方互換エイリアス）:
  各銘柄の円建て O/H/L/C を同日のクロス円レート終値（USDJPY=X 等、1基準通貨あたり円）で
  除して基準通貨建てに換算してから、RSI・SMA・リターン条件を評価する（海外投資家視点。
  stocklib.currency.to_base_currency、同日終値換算・為替ヘッジなしの近似）。
  PER/PBR/配当利回りは通貨に依存しない比率のため円建てのまま、出来高（株数）も無換算。
  リターンの関係は恒等式 (1+r_B)=(1+r_JPY)/(1+r_FX) に従う。
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from stocklib import currency, indicators, metrics, report
from stocklib.data import (
    DataFetchError,
    add_source_argument,
    fetch_info,
    fetch_prices,
    set_default_source,
)

DEFAULT_UNIVERSE = Path(__file__).resolve().parent / "universe" / "liquid30.csv"

RSI_WINDOW: int = 14
DEFAULT_SMA_WINDOW: int = 200
VOLUME_AVG_WINDOW: int = 20


@dataclass(frozen=True)
class ScreenCriteria:
    """スクリーニング条件。``None`` の項目は適用しない。

    Attributes:
        rsi_below: RSI(14) がこの値未満（売られすぎ）。
        rsi_above: RSI(14) がこの値超（買われすぎ）。
        price_above_sma: 終値が SMA(N) 超（上昇トレンド）。
        price_below_sma: 終値が SMA(N) 未満（下降トレンド）。
        volume_surge: 直近出来高が過去20日平均のこの倍率以上（出来高急増）。
        return_below: 期間リターン（%）がこの値未満。
        return_above: 期間リターン（%）がこの値超。
        per_below: PER（実績）がこの値未満（割安）。値なしは条件不成立。
        pbr_below: PBR がこの値未満（割安）。値なしは条件不成立。
        dividend_yield_above: 配当利回り（%）がこの値超（高配当）。値なしは条件不成立。
    """

    rsi_below: float | None = None
    rsi_above: float | None = None
    price_above_sma: int | None = None
    price_below_sma: int | None = None
    volume_surge: float | None = None
    return_below: float | None = None
    return_above: float | None = None
    per_below: float | None = None
    pbr_below: float | None = None
    dividend_yield_above: float | None = None

    def needs_info(self) -> bool:
        """バリュエーション条件（fetch_info が必要な条件）が指定されているか。"""
        return any(
            v is not None
            for v in (self.per_below, self.pbr_below, self.dividend_yield_above)
        )

    def sma_windows(self) -> list[int]:
        """指標計算に必要な SMA 期間のリスト（未指定なら既定の200のみ）。"""
        windows = {w for w in (self.price_above_sma, self.price_below_sma) if w is not None}
        return sorted(windows) if windows else [DEFAULT_SMA_WINDOW]

    def describe(self) -> list[str]:
        """使用中の全条件を人間可読な文字列リストで返す。"""
        conds: list[str] = []
        if self.rsi_below is not None:
            conds.append(f"RSI({RSI_WINDOW}) < {self.rsi_below:g}")
        if self.rsi_above is not None:
            conds.append(f"RSI({RSI_WINDOW}) > {self.rsi_above:g}")
        if self.price_above_sma is not None:
            conds.append(f"終値 > SMA({self.price_above_sma})")
        if self.price_below_sma is not None:
            conds.append(f"終値 < SMA({self.price_below_sma})")
        if self.volume_surge is not None:
            conds.append(f"直近出来高 >= 過去{VOLUME_AVG_WINDOW}日平均 x {self.volume_surge:g}")
        if self.return_below is not None:
            conds.append(f"期間リターン < {self.return_below:g}%")
        if self.return_above is not None:
            conds.append(f"期間リターン > {self.return_above:g}%")
        if self.per_below is not None:
            conds.append(f"PER < {self.per_below:g}")
        if self.pbr_below is not None:
            conds.append(f"PBR < {self.pbr_below:g}")
        if self.dividend_yield_above is not None:
            conds.append(f"配当利回り > {self.dividend_yield_above:g}%")
        return conds


def load_universe(path: Path) -> pd.DataFrame:
    """ユニバース CSV（列: code,name,sector、``#`` 行はコメント）を読み込む。"""
    df = pd.read_csv(path, comment="#", dtype={"code": str})
    required = {"code", "name", "sector"}
    if not required.issubset(df.columns):
        raise ValueError(f"ユニバース CSV には {sorted(required)} 列が必要です: {path}")
    return df


def _last_value(series: pd.Series) -> float:
    """系列の末尾値を float で返す。NaN・空系列は ``nan``。"""
    if series.empty or pd.isna(series.iloc[-1]):
        return float("nan")
    return float(series.iloc[-1])


def volume_surge_ratio(volume: pd.Series | None, window: int = VOLUME_AVG_WINDOW) -> float:
    """直近出来高 / 過去 ``window`` 日（直近日を除く）平均出来高の倍率。

    データ不足（``window + 1`` 日未満）や平均ゼロの場合は ``nan``。
    """
    if volume is None or len(volume) <= window:
        return float("nan")
    avg = float(volume.iloc[-(window + 1):-1].mean())
    if not math.isfinite(avg) or avg <= 0:
        return float("nan")
    return float(volume.iloc[-1]) / avg


def _as_float(value: object) -> float:
    """fetch_info の値を float に変換する。数値以外・欠損は ``nan``。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        f = float(value)
        return f if math.isfinite(f) else float("nan")
    return float("nan")


def valuation_values(info: dict[str, object]) -> dict[str, float]:
    """fetch_info の辞書から PER・PBR・配当利回り（%）を取り出す。

    取得できない項目は ``nan``（＝バリュエーション条件は不成立扱い）。
    配当利回りは fetch_info が返す比率（0.03 = 3%）を % に換算する。
    """
    return {
        "per": _as_float(info.get("PER（実績）")),
        "pbr": _as_float(info.get("PBR")),
        "div_yield_pct": _as_float(info.get("配当利回り")) * 100.0,
    }


def _passes(
    criteria: ScreenCriteria,
    last: float,
    rsi_last: float,
    sma_last: dict[int, float],
    vol_surge: float,
    ret_period: float,
    valuation: dict[str, float] | None = None,
) -> bool:
    """全条件を満たすか判定する。指標が NaN の条件は不合格扱い。"""
    c = criteria
    if c.rsi_below is not None and not (rsi_last < c.rsi_below):
        return False
    if c.rsi_above is not None and not (rsi_last > c.rsi_above):
        return False
    if c.price_above_sma is not None:
        s = sma_last[c.price_above_sma]
        if math.isnan(s) or not (last > s):
            return False
    if c.price_below_sma is not None:
        s = sma_last[c.price_below_sma]
        if math.isnan(s) or not (last < s):
            return False
    if c.volume_surge is not None and not (vol_surge >= c.volume_surge):
        return False
    if c.return_below is not None and not (ret_period * 100.0 < c.return_below):
        return False
    if c.return_above is not None and not (ret_period * 100.0 > c.return_above):
        return False
    v = valuation or {}
    if c.per_below is not None and not (v.get("per", float("nan")) < c.per_below):
        return False
    if c.pbr_below is not None and not (v.get("pbr", float("nan")) < c.pbr_below):
        return False
    if c.dividend_yield_above is not None and not (
        v.get("div_yield_pct", float("nan")) > c.dividend_yield_above
    ):
        return False
    return True


def screen(
    universe: pd.DataFrame,
    period: str,
    criteria: ScreenCriteria,
    synthetic: bool,
    in_currency: str | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """各銘柄の指標を計算し、条件を満たす銘柄の表と取得失敗リストを返す。

    Args:
        in_currency: 基準通貨コード（``"USD"`` / ``"EUR"`` / ``"GBP"``）。指定すると
            各銘柄の円建て O/H/L/C を同日のクロス円終値で基準通貨建てに換算してから
            RSI・SMA・リターン条件を評価する（``stocklib.currency.to_base_currency``、
            同日終値換算・為替ヘッジなしの近似）。PER/PBR/配当利回りは通貨に依存しない
            比率のため無変換、出来高（株数）も無変換。``None`` なら円建てのまま評価する。

    Raises:
        DataFetchError: ``in_currency`` 指定時に為替レートを取得できなかった場合
            （為替なしでは全銘柄の換算が不可能なため、銘柄単位の失敗と違い全体を中断する）。
    """
    rows: list[dict[str, object]] = []
    errors: list[str] = []
    sma_windows = criteria.sma_windows()
    use_valuation = criteria.needs_info()
    fx_df: pd.DataFrame | None = None
    if in_currency is not None:
        fx_df = currency.fetch_fx(in_currency, period, synthetic=synthetic)
    for rec in universe.to_dict("records"):
        code = str(rec["code"])
        try:
            df = fetch_prices(code, period=period, synthetic=synthetic)[code]
        except DataFetchError as exc:
            errors.append(f"{code}: {exc}")
            continue
        if fx_df is not None:
            try:
                df = currency.to_base_currency(df, fx_df)
            except ValueError as exc:  # 為替と株価に共通の日付が無い等
                errors.append(f"{code}: {exc}")
                continue
        close = df["Close"]
        volume = df["Volume"] if "Volume" in df.columns else None
        last = float(close.iloc[-1])
        rsi_last = _last_value(indicators.rsi(close, RSI_WINDOW))
        sma_last = {w: _last_value(indicators.sma(close, w)) for w in sma_windows}
        ret_1m = float(last / close.iloc[-21] - 1.0) if len(close) > 21 else float("nan")
        ret_period = float(last / float(close.iloc[0]) - 1.0)
        vol_surge = volume_surge_ratio(volume)

        valuation: dict[str, float] | None = None
        if use_valuation:
            try:
                info = fetch_info(code, synthetic=synthetic)
            except DataFetchError:
                info = {}  # 取得失敗は「値なし＝条件不成立」として扱う
            valuation = valuation_values(info)

        if not _passes(criteria, last, rsi_last, sma_last, vol_surge, ret_period, valuation):
            continue

        row: dict[str, object] = {
            "code": code,
            "name": rec["name"],
            "sector": rec["sector"],
            "close": last,
            f"rsi{RSI_WINDOW}": rsi_last,
        }
        for w in sma_windows:
            row[f"sma{w}"] = sma_last[w]
        row["ret_1m"] = ret_1m
        row["ret_period"] = ret_period
        row["vol_surge"] = vol_surge
        row["ann_vol"] = metrics.ann_vol(metrics.daily_returns(close))
        if use_valuation and valuation is not None:
            row["per"] = valuation["per"]
            row["pbr"] = valuation["pbr"]
            row["div_yield"] = valuation["div_yield_pct"] / 100.0  # fmt_pct 用に比率へ戻す
        rows.append(row)
    return pd.DataFrame(rows), errors


def _fmt_cell(column: str, value: object) -> str:
    """テーブル列名に応じてセル値を整形する。"""
    if column in ("code", "name", "sector"):
        return str(value)
    if column.startswith("ret_") or column in ("ann_vol", "div_yield"):
        return report.fmt_pct(value)
    return report.fmt_num(value)


def result_table(result: pd.DataFrame) -> str:
    """スクリーニング結果 DataFrame を Markdown テーブル文字列にする。"""
    if result.empty:
        return "（条件に合致する銘柄はありませんでした）"
    headers = [str(c) for c in result.columns]
    rows = [
        [_fmt_cell(col, rec[col]) for col in headers]
        for rec in result.to_dict("records")
    ]
    return report.markdown_table(headers, rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ユニバース銘柄をテクニカル・バリュエーション条件でスクリーニングする"
    )
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE,
                        help="ユニバース CSV（列: code,name,sector）")
    parser.add_argument("--period", default="1y", help="取得期間（既定: 1y）")
    parser.add_argument("--rsi-below", type=float, default=None,
                        help="RSI(14) がこの値未満の銘柄に絞る（売られすぎ。例: 30）")
    parser.add_argument("--rsi-above", type=float, default=None,
                        help="RSI(14) がこの値超の銘柄に絞る（買われすぎ。例: 70）")
    parser.add_argument("--price-above-sma", type=int, default=None, metavar="N",
                        help="終値が SMA(N) を上回る銘柄に絞る（上昇トレンド。例: 200）")
    parser.add_argument("--price-below-sma", type=int, default=None, metavar="N",
                        help="終値が SMA(N) を下回る銘柄に絞る（下降トレンド。例: 200）")
    parser.add_argument("--volume-surge", type=float, default=None, metavar="X",
                        help=f"直近出来高が過去{VOLUME_AVG_WINDOW}日平均の X 倍以上の銘柄に絞る（出来高急増。例: 2.0）")
    parser.add_argument("--return-below", type=float, default=None, metavar="PCT",
                        help="期間リターンがこの値（%%）未満の銘柄に絞る（例: -10）")
    parser.add_argument("--return-above", type=float, default=None, metavar="PCT",
                        help="期間リターンがこの値（%%）超の銘柄に絞る（例: 10）")
    parser.add_argument("--per-below", type=float, default=None, metavar="X",
                        help="PER（実績）がこの値未満の銘柄に絞る（割安。例: 15）。値が取得できない銘柄は除外")
    parser.add_argument("--pbr-below", type=float, default=None, metavar="X",
                        help="PBR がこの値未満の銘柄に絞る（割安。例: 1.0）。値が取得できない銘柄は除外")
    parser.add_argument("--dividend-yield-above", type=float, default=None, metavar="PCT",
                        help="配当利回りがこの値（%%）超の銘柄に絞る（高配当。例: 3.0）。値が取得できない銘柄は除外")
    parser.add_argument("--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）")
    add_source_argument(parser)
    parser.add_argument(
        "--in-currency",
        type=str.upper,
        choices=sorted(currency.SUPPORTED_CURRENCIES),
        default=None,
        help="基準通貨建てで条件評価する（海外投資家視点。円建て O/H/L/C を同日のクロス円"
        "終値で換算してから RSI・SMA・リターン条件を評価。PER/PBR/配当利回り・出来高は無変換）",
    )
    parser.add_argument(
        "--in-usd",
        action="store_true",
        help="--in-currency USD のエイリアス（後方互換）",
    )
    args = parser.parse_args(argv)
    set_default_source(args.source)
    in_currency: str | None = args.in_currency or ("USD" if args.in_usd else None)

    try:
        universe = load_universe(args.universe)
    except (OSError, ValueError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    criteria = ScreenCriteria(
        rsi_below=args.rsi_below,
        rsi_above=args.rsi_above,
        price_above_sma=args.price_above_sma,
        price_below_sma=args.price_below_sma,
        volume_surge=args.volume_surge,
        return_below=args.return_below,
        return_above=args.return_above,
        per_below=args.per_below,
        pbr_below=args.pbr_below,
        dividend_yield_above=args.dividend_yield_above,
    )
    try:
        result, errors = screen(
            universe, args.period, criteria, args.synthetic, in_currency=in_currency
        )
    except DataFetchError as exc:
        print(f"エラー: 為替レートを取得できないため基準通貨建て評価を中断します: {exc}",
              file=sys.stderr)
        return 1

    conditions = criteria.describe()
    cond_text = "、".join(conditions) if conditions else "なし（全銘柄の指標一覧）"
    table = result_table(result)

    if in_currency is not None:
        ccy_label = currency.currency_label(in_currency)
        title = f"スクリーニング結果（{ccy_label}建て）"
    else:
        ccy_label = None
        title = "スクリーニング結果"
    lines = [
        report.report_header(title),
        f"- ユニバース: {args.universe}（{len(universe)} 銘柄）",
        f"- 期間: {args.period}",
        f"- 条件: {cond_text}",
        f"- 合致: {len(result)} 銘柄",
    ]
    if in_currency is not None:
        fx_ticker = currency.get_fx_ticker(in_currency)
        lines.append(
            f"- 基準通貨: {in_currency}（{ccy_label}建て）。円建て O/H/L/C を同日の"
            f" {fx_ticker} 終値（1{ccy_label}あたり円）で除して換算し、RSI・SMA・リターン条件を"
            f"{ccy_label}建て系列で評価（同日終値換算・為替ヘッジなしの近似）。"
            "表の close/sma 列も同換算。PER/PBR/配当利回りは通貨に依存しない比率のため無変換、"
            "出来高（株数）も無変換。"
        )
    if args.synthetic:
        lines.append("- **データ: 合成データ（--synthetic、実在の株価ではありません）**")
        if criteria.needs_info():
            lines.append(
                "- **バリュエーション指標（PER/PBR/配当利回り）は合成ダミー値です"
                "（実在の企業指標ではありません）**"
            )
    lines += ["", table, ""]
    lines.append(
        "注: 本結果は指定した条件に**機械的に合致した銘柄の一覧**であり、"
        "推奨銘柄でも「買い候補」でもない。条件合致は深掘りすべき調査対象を絞り込む"
        "入口にすぎず、将来の騰落の予測でも投資助言でもない。RSI・PER 等の水準は"
        "単体で割安・割高を意味しない（低PER は成長率・ROE・リスクの差の反映で"
        "ありうる。`knowledge/fundamental/valuation-metrics.md` 参照）。"
        "また同梱ユニバースは現存する大型高流動性銘柄を後から選んだリストのため、"
        "生存者バイアスがある。"
    )
    lines.append("")
    if errors:
        lines.append("## 取得失敗")
        lines.extend(f"- {e}" for e in errors)
        lines.append("")
    content = "\n".join(lines)

    if in_currency is not None:
        print(f"基準通貨: {in_currency}（{ccy_label}建てで条件評価、同日終値換算・為替ヘッジなしの近似）")
    print(f"条件: {cond_text}")
    print(table)
    ccy_part = f"-{in_currency.lower()}" if in_currency is not None else ""
    filename = f"screen{ccy_part}-{dt.date.today().isoformat()}.md"
    path = report.save_report(content, filename)
    print(f"\nレポート: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

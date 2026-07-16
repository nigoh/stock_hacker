#!/usr/bin/env python3
"""バックテスト実行 CLI。

使い方（リポジトリルートから）:
    python3 analysis/run_backtest.py --strategy ma_cross --code 7203
        [--fast 25 --slow 75] [--period 2y] [--cost-bps 10]
        [--split 0.7] [--sweep] [--synthetic]
    python3 analysis/run_backtest.py --strategy rsi_reversal --code 7203
        [--rsi-window 14 --rsi-lower 30 --rsi-upper 50] [--split 0.7] [--sweep] [--synthetic]

戦略とバイ&ホールドの統計を stdout に出力し、
reports/backtest-<strategy>-<code>-<日付>.md に保存する。

- --split R: 時間順で前 R（例 0.7 = 70%）をインサンプル（IS）、残りをアウトオブサンプル
  （OOS）とし、両区間の統計を並記する。
- --sweep: 指定パラメータの近傍グリッドを走らせ、試行回数 N と成績分布を表化する
  （--split 指定時は IS 区間のみでスイープする）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections.abc import Callable

import pandas as pd

from stocklib import metrics, report
from stocklib.backtest import (
    BacktestResult,
    ma_cross_signal,
    parameter_sweep,
    rsi_reversal_signal,
    run_backtest,
    split_series,
)
from stocklib.data import DataFetchError, fetch_prices

STRATEGIES: tuple[str, ...] = ("ma_cross", "rsi_reversal")

METRIC_LABELS: tuple[str, ...] = (
    "トータルリターン",
    "年率リターン",
    "年率ボラティリティ",
    "シャープレシオ",
    "最大ドローダウン",
    "取引回数（エントリー）",
    "勝率（トレード単位）",
    "t統計量",
    "対象営業日数",
)


def result_values(result: BacktestResult) -> list[object]:
    """BacktestResult を METRIC_LABELS と同順の表示値リストに変換する。"""
    return [
        report.fmt_pct(result.total_return),
        report.fmt_pct(result.ann_return),
        report.fmt_pct(result.ann_vol),
        report.fmt_num(result.sharpe),
        report.fmt_pct(result.max_drawdown),
        result.n_trades,
        report.fmt_pct(result.win_rate),
        report.fmt_num(result.t_stat),
        result.n_days,
    ]


def result_rows(result: BacktestResult) -> list[list[object]]:
    """BacktestResult をレポート用の行リストに変換する。"""
    return [[label, value] for label, value in zip(METRIC_LABELS, result_values(result))]


def build_strategy(
    args: argparse.Namespace,
) -> tuple[Callable[..., pd.Series], dict[str, float | int], str]:
    """引数から (シグナル関数, パラメータ辞書, 戦略ラベル) を組み立てる。"""
    if args.strategy == "ma_cross":
        params: dict[str, float | int] = {"fast": args.fast, "slow": args.slow}
        label = f"移動平均クロス（SMA{args.fast} / SMA{args.slow}）"
        return ma_cross_signal, params, label
    if args.strategy == "rsi_reversal":
        params = {
            "window": args.rsi_window,
            "lower": args.rsi_lower,
            "upper": args.rsi_upper,
        }
        label = (
            f"RSI逆張り（RSI{args.rsi_window} < {args.rsi_lower:g} で買い、"
            f"> {args.rsi_upper:g} で手仕舞い）"
        )
        return rsi_reversal_signal, params, label
    raise ValueError(f"未対応の戦略です: {args.strategy}（対応: {', '.join(STRATEGIES)}）")


def build_grid(strategy: str, params: dict[str, float | int]) -> list[dict[str, float | int]]:
    """--sweep 用の近傍パラメータグリッドを構築する（無効な組み合わせは除外）。"""
    if strategy == "ma_cross":
        fast, slow = int(params["fast"]), int(params["slow"])
        fasts = sorted({max(2, fast + d) for d in (-5, 0, 5)})
        slows = sorted({max(3, slow + d) for d in (-15, 0, 15)})
        return [{"fast": f, "slow": s} for f in fasts for s in slows if f < s]
    if strategy == "rsi_reversal":
        window = int(params["window"])
        lower, upper = float(params["lower"]), float(params["upper"])
        lowers = sorted({lower + d for d in (-5.0, 0.0, 5.0)})
        uppers = sorted({upper + d for d in (-5.0, 0.0, 5.0)})
        return [
            {"window": window, "lower": lo, "upper": up}
            for lo in lowers
            for up in uppers
            if 0.0 < lo < up < 100.0
        ]
    raise ValueError(f"未対応の戦略です: {strategy}")


def format_params(strategy: str, params: dict[str, float | int]) -> str:
    """パラメータ辞書をテーブル表示用の短い文字列に整形する。"""
    if strategy == "ma_cross":
        return f"fast={params['fast']}, slow={params['slow']}"
    return f"window={params['window']}, lower={params['lower']:g}, upper={params['upper']:g}"


def _span(prices: pd.Series) -> str:
    """価格系列の期間を「開始〜終了、N営業日」形式で整形する。"""
    return f"{prices.index[0].date()} 〜 {prices.index[-1].date()}、{len(prices)} 営業日"


def build_report(args: argparse.Namespace) -> str:
    """バックテストレポート本文（Markdown）を構築する。"""
    code: str = args.code
    close = fetch_prices(code, period=args.period, synthetic=args.synthetic)[code]["Close"]
    signal_fn, params, strategy_label = build_strategy(args)

    positions = signal_fn(close, **params)
    result = run_backtest(close, positions, cost_bps=args.cost_bps)

    # 比較用バイ&ホールド（コストなし・常時ロング）
    bh_rets = metrics.daily_returns(close)
    bh_rows = [
        ["トータルリターン", report.fmt_pct(float(close.iloc[-1] / close.iloc[0] - 1.0))],
        ["年率リターン", report.fmt_pct(metrics.ann_return(bh_rets))],
        ["年率ボラティリティ", report.fmt_pct(metrics.ann_vol(bh_rets))],
        ["シャープレシオ", report.fmt_num(metrics.sharpe(bh_rets))],
        ["最大ドローダウン", report.fmt_pct(metrics.max_drawdown(close))],
    ]

    lines: list[str] = [report.report_header(f"バックテストレポート: {code} × {strategy_label}")]
    lines.append(f"- 期間: {args.period}（{_span(close)}）")
    lines.append(f"- 取引コスト: 片道 {args.cost_bps} bps")
    lines.append("- 執行: シグナル判定当日の終値で約定し、翌営業日のリターンから反映（先読みバイアス回避）")
    if args.synthetic:
        lines.append("- **データ: 合成データ（--synthetic、実在の株価ではありません）**")
    lines.append("")
    lines.append("## 戦略成績（全期間）")
    lines.append("")
    lines.append(report.markdown_table(["指標", "値"], result_rows(result)))
    lines.append("")
    lines.append(f"**t統計量の解釈**: {result.t_stat_interpretation}")
    lines.append("")

    # --- IS/OOS 分割 ---
    if args.split is not None:
        is_close, oos_close = split_series(close, ratio=args.split)
        is_result = run_backtest(is_close, signal_fn(is_close, **params), cost_bps=args.cost_bps)
        oos_result = run_backtest(oos_close, signal_fn(oos_close, **params), cost_bps=args.cost_bps)
        lines.append(f"## IS/OOS 分割検証（--split {args.split:g}）")
        lines.append("")
        lines.append(f"- インサンプル（IS）: {_span(is_close)}")
        lines.append(f"- アウトオブサンプル（OOS）: {_span(oos_close)}")
        lines.append("- 指標は各区間の先頭から独立に再計算する（区間冒頭に指標のウォームアップ期間が生じる）")
        lines.append("")
        split_rows = [
            [label, is_val, oos_val]
            for label, is_val, oos_val in zip(
                METRIC_LABELS, result_values(is_result), result_values(oos_result)
            )
        ]
        lines.append(report.markdown_table(["指標", "IS", "OOS"], split_rows))
        lines.append("")
        lines.append(f"**IS の t統計量**: {is_result.t_stat_interpretation}")
        lines.append("")
        lines.append(f"**OOS の t統計量**: {oos_result.t_stat_interpretation}")
        lines.append("")
        lines.append(
            "**IS/OOS の読み方**: OOS の成績（シャープレシオ・最大ドローダウン）が IS より"
            "大きく劣化していれば、IS での見かけの成績は過剰適合（データスヌーピング）の"
            "強い証拠である。パラメータ調整は IS のみで行い、OOS は最終確定した戦略に対して"
            "一度だけ評価すること。OOS の結果を見てパラメータを修正した場合、その OOS は"
            "もはや検証データとして無効である。"
        )
        lines.append("")

    # --- パラメータ近傍スイープ ---
    if args.sweep:
        if args.split is not None:
            sweep_close, _ = split_series(close, ratio=args.split)
            sweep_scope = f"IS 区間のみ（{_span(sweep_close)}。OOS をパラメータ選択に使わないため）"
        else:
            sweep_close = close
            sweep_scope = f"全期間（{_span(sweep_close)}）"
        grid = build_grid(args.strategy, params)
        sweep_results = parameter_sweep(sweep_close, signal_fn, grid, cost_bps=args.cost_bps)
        n_trials = len(sweep_results)
        p_false = 1.0 - 0.95**n_trials

        lines.append(f"## パラメータ頑健性（--sweep、試行回数 N={n_trials}）")
        lines.append("")
        lines.append(f"- 対象区間: {sweep_scope}")
        lines.append(f"- 指定パラメータ: {format_params(args.strategy, params)}")
        lines.append("")
        sweep_rows = [
            [
                format_params(args.strategy, p) + ("（指定値）" if p == params else ""),
                report.fmt_pct(r.ann_return),
                report.fmt_num(r.sharpe),
                report.fmt_pct(r.max_drawdown),
                r.n_trades,
                report.fmt_num(r.t_stat),
            ]
            for p, r in sweep_results
        ]
        lines.append(
            report.markdown_table(
                ["パラメータ", "年率リターン", "シャープ", "最大DD", "取引回数", "t統計量"],
                sweep_rows,
            )
        )
        lines.append("")
        lines.append(
            f"**多重検定に関する注意**: 本スイープの試行回数は N={n_trials}。無価値な戦略でも "
            f"N 個試せば少なくとも1つが5%有意になる確率は約 {p_false * 100:.0f}% に達する。"
            "最良の組み合わせの成績は額面どおり解釈せず、ボンフェローニ的に有意水準を "
            "α/N に引き下げるか、t > 3 を目安とすること。"
        )
        lines.append("")
        lines.append(
            "**頑健性の読み方**: 近傍パラメータで成績が指定値から激変（符号反転・シャープの"
            "大幅低下）している場合は過剰適合を疑う。近傍でも成績がおおむね維持されることが、"
            "パラメータ頑健性の最低条件である。"
        )
        lines.append("")

    lines.append("## 参考: バイ&ホールド（コストなし）")
    lines.append("")
    lines.append(report.markdown_table(["指標", "値"], bh_rows))
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ルールベース戦略のバックテストを実行する")
    parser.add_argument("--strategy", choices=STRATEGIES, default="ma_cross", help="戦略名")
    parser.add_argument("--code", required=True, help="銘柄コード（4桁数字、例: 7203）")
    parser.add_argument("--period", default="2y", help="取得期間（既定: 2y）")
    parser.add_argument("--fast", type=int, default=25, help="[ma_cross] 短期 SMA 期間（既定: 25）")
    parser.add_argument("--slow", type=int, default=75, help="[ma_cross] 長期 SMA 期間（既定: 75）")
    parser.add_argument(
        "--rsi-window", type=int, default=14, help="[rsi_reversal] RSI 期間（既定: 14）"
    )
    parser.add_argument(
        "--rsi-lower", type=float, default=30.0,
        help="[rsi_reversal] エントリー閾値（RSI がこの値未満で買い。既定: 30）",
    )
    parser.add_argument(
        "--rsi-upper", type=float, default=50.0,
        help="[rsi_reversal] イグジット閾値（RSI がこの値超で手仕舞い。既定: 50）",
    )
    parser.add_argument("--cost-bps", type=float, default=10.0, help="片道取引コスト bps（既定: 10）")
    parser.add_argument(
        "--split", type=float, default=None,
        help="IS/OOS 分割比率（例: 0.7 = 時間順で前70%%を IS、残りを OOS として並記）",
    )
    parser.add_argument(
        "--sweep", action="store_true",
        help="パラメータ近傍グリッドをスイープし、試行回数 N と成績分布を表化する",
    )
    parser.add_argument("--synthetic", action="store_true", help="合成データで実行（ネットワーク不要）")
    args = parser.parse_args(argv)

    try:
        content = build_report(args)
    except (DataFetchError, ValueError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    print(content)
    filename = f"backtest-{args.strategy}-{args.code}-{dt.date.today().isoformat()}.md"
    path = report.save_report(content, filename)
    print(f"レポート: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""バックテスト実行 CLI。

使い方（リポジトリルートから）:
    python3 analysis/run_backtest.py --strategy ma_cross --code 7203
        [--fast 25 --slow 75] [--period 2y] [--cost-bps 10]
        [--split 0.7] [--sweep] [--synthetic]
    python3 analysis/run_backtest.py --strategy rsi_reversal --code 7203
        [--rsi-window 14 --rsi-lower 30 --rsi-upper 50] [--split 0.7] [--sweep] [--synthetic]
    python3 analysis/run_backtest.py --strategy dca --code 7203
        [--monthly 30000 --day-of-month 1] [--period 5y] [--cost-bps 10] [--synthetic]

戦略とバイ&ホールドの統計を stdout に出力し、
reports/backtest-<strategy>-<code>-<日付>.md に保存する。

- --split R: 時間順で前 R（例 0.7 = 70%）をインサンプル（IS）、残りをアウトオブサンプル
  （OOS）とし、両区間の統計を並記する。
- --sweep: 指定パラメータの近傍グリッドを走らせ、試行回数 N と成績分布を表化する
  （--split 指定時は IS 区間のみでスイープする）。
- --in-currency USD|EUR|GBP（--in-usd は --in-currency USD の後方互換エイリアス）:
  全期間の戦略成績とバイ&ホールドを円建て・基準通貨建て併記にする（海外投資家視点）。
  **売買シグナルは常に円建て（現地）価格で計算する**——実際の東証での執行は円建て価格で
  行われるため、基準通貨建て換算後の価格でシグナルを出すと現実には観測できない為替込みの
  系列に基づく判定となり、執行と乖離したルックアヘッド類似の歪みが生じる。基準通貨建て換算は
  確定した円建て日次リターンに対して恒等式 (1+r_B)=(1+r_JPY)/(1+r_FX) を適用する
  （stocklib.currency.to_base_returns、同日終値換算・為替ヘッジなしの近似）。
- --strategy dca: 売買シグナル戦略ではなく毎月定額買付（ドルコスト平均法）の積立
  シミュレーション。--monthly（月額円）・--day-of-month（買付日、非営業日なら翌営業日に
  繰越）を取り、累計投資額と同額の期初一括投資との比較表・チャートを出力する。
  --split / --sweep / --in-currency とは併用できない。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from stocklib import charts, currency, metrics, report
from stocklib.backtest import (
    BacktestResult,
    DCAComparison,
    bollinger_reversal_signal,
    compare_dca_lump_sum,
    ma_cross_signal,
    macd_signal,
    parameter_sweep,
    rsi_reversal_signal,
    run_backtest,
    split_series,
)
from stocklib.data import (
    DataFetchError,
    add_source_argument,
    fetch_prices,
    set_default_source,
)

STRATEGIES: tuple[str, ...] = ("ma_cross", "rsi_reversal", "macd", "bollinger_reversal", "dca")

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
    if args.strategy == "macd":
        params = {"fast": args.macd_fast, "slow": args.macd_slow, "signal": args.macd_signal}
        label = (
            f"MACDトレンドフォロー（MACD({args.macd_fast},{args.macd_slow},{args.macd_signal}) が"
            "シグナル線超でロング）"
        )
        return macd_signal, params, label
    if args.strategy == "bollinger_reversal":
        params = {"window": args.bb_window, "num_std": args.bb_std}
        label = (
            f"ボリンジャー逆張り（{args.bb_window}日 ±{args.bb_std:g}σ、下限割れで買い・"
            "中心線回帰で手仕舞い）"
        )
        return bollinger_reversal_signal, params, label
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
    if strategy == "macd":
        fast, slow, signal = int(params["fast"]), int(params["slow"]), int(params["signal"])
        fasts = sorted({max(2, fast + d) for d in (-4, 0, 4)})
        slows = sorted({max(3, slow + d) for d in (-8, 0, 8)})
        return [
            {"fast": f, "slow": s, "signal": signal}
            for f in fasts for s in slows if f < s
        ]
    if strategy == "bollinger_reversal":
        window = int(params["window"])
        windows = sorted({max(2, window + d) for d in (-5, 0, 5)})
        stds = sorted({round(float(params["num_std"]) + d, 1) for d in (-0.5, 0.0, 0.5)})
        return [{"window": w, "num_std": s} for w in windows for s in stds if s > 0]
    raise ValueError(f"未対応の戦略です: {strategy}")


def format_params(strategy: str, params: dict[str, float | int]) -> str:
    """パラメータ辞書をテーブル表示用の短い文字列に整形する。"""
    if strategy == "ma_cross":
        return f"fast={params['fast']}, slow={params['slow']}"
    if strategy == "macd":
        return f"fast={params['fast']}, slow={params['slow']}, signal={params['signal']}"
    if strategy == "bollinger_reversal":
        return f"window={params['window']}, num_std={params['num_std']:g}"
    return f"window={params['window']}, lower={params['lower']:g}, upper={params['upper']:g}"


def _span(prices: pd.Series) -> str:
    """価格系列の期間を「開始〜終了、N営業日」形式で整形する。"""
    return f"{prices.index[0].date()} 〜 {prices.index[-1].date()}、{len(prices)} 営業日"


def _chart_lines(equity: pd.Series, title: str, img_stem: str) -> list[str]:
    """資産曲線+ドローダウン図 PNG を生成し、埋め込み用 Markdown 行を返す（失敗時は警告して空リスト）。"""
    if not charts.charts_available():
        print("警告: matplotlib が利用できないため、チャートなしで続行します", file=sys.stderr)
        return []
    try:
        path = charts.plot_drawdown(
            equity, charts.img_path(f"{img_stem}-equity.png"), title=title
        )
    except Exception as exc:  # チャートは補助情報。失敗してもレポート生成は続行する
        print(f"警告: チャート生成に失敗しました（チャートなしで続行します）: {exc}", file=sys.stderr)
        return []
    return [
        f"![chart](img/{path.name})",
        "",
        "（上段: 戦略エクイティカーブ（期首=1.0、コスト控除後）、下段: ドローダウン）",
        "",
    ]


def _equity_returns(equity: pd.Series) -> pd.Series:
    """エクイティカーブ（初期値 1.0 基準）から日次リターン系列を復元する。"""
    rets = equity.pct_change()
    if len(rets):
        rets.iloc[0] = float(equity.iloc[0]) - 1.0
    return rets


def _jpy_base_rows(rets_jpy: pd.Series, rets_base: pd.Series) -> list[list[object]]:
    """円建て・基準通貨建てリターン系列から「指標 | 円建て | <通貨>建て」の行リストを作る。"""
    eq_jpy = (1.0 + rets_jpy).cumprod()
    eq_base = (1.0 + rets_base).cumprod()
    return [
        [
            "トータルリターン",
            report.fmt_pct(float(eq_jpy.iloc[-1] - 1.0)),
            report.fmt_pct(float(eq_base.iloc[-1] - 1.0)),
        ],
        [
            "年率リターン（CAGR）",
            report.fmt_pct(metrics.ann_return(rets_jpy)),
            report.fmt_pct(metrics.ann_return(rets_base)),
        ],
        [
            "年率ボラティリティ",
            report.fmt_pct(metrics.ann_vol(rets_jpy)),
            report.fmt_pct(metrics.ann_vol(rets_base)),
        ],
        [
            "シャープレシオ",
            report.fmt_num(metrics.sharpe(rets_jpy)),
            report.fmt_num(metrics.sharpe(rets_base)),
        ],
        [
            "最大ドローダウン",
            report.fmt_pct(metrics.max_drawdown(eq_jpy)),
            report.fmt_pct(metrics.max_drawdown(eq_base)),
        ],
    ]


def _fx_section(
    result: BacktestResult, close: pd.Series, period: str, synthetic: bool, ccy: str
) -> list[str]:
    """「<基準通貨>建て評価（海外投資家視点）」節の Markdown 行を構築する。

    設計上の重要点: **売買シグナル・執行は円建て（現地）価格で確定済み**であり、
    本節はその確定した円建て日次リターン系列を恒等式
    $(1 + r^{B}) = (1 + r^{JPY}) / (1 + r^{FX})$（$B$: 基準通貨）で基準通貨建てに
    換算した再計算に過ぎない。基準通貨建て換算後の価格系列でシグナルを再計算することは
    しない——東証での実際の執行は円建て価格に対して行われるため、為替込みの系列で
    シグナルを出すと現実の執行と乖離したルックアヘッド類似の歪みが生じる。
    """
    label = currency.currency_label(ccy)
    ticker = currency.get_fx_ticker(ccy)
    pair = ticker.removesuffix("=X")
    fx_df = currency.fetch_fx(ccy, period, synthetic=synthetic)
    fx = currency.align_fx(result.equity_curve.index, fx_df["Close"])
    fx_change = float(fx.iloc[-1] / fx.iloc[0] - 1.0)

    strat_rets_jpy = _equity_returns(result.equity_curve)
    strat_rets_base = currency.to_base_returns(strat_rets_jpy, fx_df["Close"])

    bh_rets_jpy = metrics.daily_returns(close)
    bh_rets_base = metrics.daily_returns(currency.to_base_series(close, fx_df["Close"]))

    headers = ["指標", "円建て", f"{label}建て"]
    lines: list[str] = []
    lines.append(f"## {label}建て評価（海外投資家視点）")
    lines.append("")
    lines.append(
        "- **売買シグナルは円建て（現地）価格で計算・執行済み**。本節はその確定した"
        f"円建て日次リターンを恒等式 $(1 + r^{{{ccy}}}) = (1 + r^{{JPY}}) / (1 + r^{{FX}})$ で"
        f"{label}建てに換算した再計算であり、シグナル自体は変わらない"
        f"（{label}建て価格でシグナルを出すと実際の東証での執行と乖離し、"
        "ルックアヘッド類似の歪みが生じるため行わない）。"
    )
    lines.append(
        f"- 為替は {ticker}（1{label}あたり円）の**同日終値換算・ヘッジなしの近似**。"
        "日中の為替変動・ヘッジコスト・両替コストは考慮しない。"
    )
    lines.append(
        f"- 為替（{pair}）期間変動: {report.fmt_pct(fx_change)}"
        f"（{float(fx.iloc[0]):.2f} → {float(fx.iloc[-1]):.2f} 円/{label}。"
        f"円安は{label}建てリターンの押し下げ、円高は押し上げ要因）"
    )
    lines.append("")
    lines.append("### 戦略成績（全期間、コスト控除後）")
    lines.append("")
    lines.append(report.markdown_table(headers, _jpy_base_rows(strat_rets_jpy, strat_rets_base)))
    lines.append("")
    lines.append("### 参考: バイ&ホールド（コストなし）")
    lines.append("")
    lines.append(report.markdown_table(headers, _jpy_base_rows(bh_rets_jpy, bh_rets_base)))
    lines.append("")
    return lines


def _dca_chart_lines(cmp: DCAComparison, code: str, img_stem: str) -> list[str]:
    """累計投資額 vs 評価額（積立・一括）のチャート PNG を生成し、埋め込み用 Markdown 行を返す。

    チャート生成は補助情報のため、matplotlib 未導入・描画失敗時は警告して空リストを返す。
    """
    if not charts.charts_available():
        print("警告: matplotlib が利用できないため、チャートなしで続行します", file=sys.stderr)
        return []
    try:
        import matplotlib.pyplot as plt  # charts の import 時に Agg バックエンド設定済み

        fig, ax = plt.subplots(figsize=(10, 5), constrained_layout=True)
        dca = cmp.dca
        ax.plot(
            dca.value_curve.index, dca.value_curve,
            color="#2a6fdb", linewidth=1.6, label="DCA value",
        )
        ax.plot(
            dca.invested_curve.index, dca.invested_curve,
            color="#6b7280", linewidth=1.4, linestyle="--", label="DCA invested (cum.)",
        )
        ax.plot(
            cmp.lump_sum.value_curve.index, cmp.lump_sum.value_curve,
            color="#e8a33d", linewidth=1.4, alpha=0.9, label="Lump-sum value",
        )
        ax.scatter(
            dca.buy_prices.index,
            dca.value_curve.loc[dca.buy_prices.index],
            color="#2a6fdb", s=12, zorder=3, label="Buy dates",
        )
        ax.set_ylabel("JPY")
        ax.set_xlabel("Date")
        ax.set_title(f"{code} DCA: Invested vs Value (vs Lump-sum)")
        ax.legend(loc="upper left", fontsize=8, frameon=False)
        ax.grid(True, alpha=0.3, linewidth=0.5)
        ax.set_axisbelow(True)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        path = charts.img_path(f"{img_stem}-dca.png")
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, dpi=110)
        plt.close(fig)
    except Exception as exc:
        print(f"警告: チャート生成に失敗しました（チャートなしで続行します）: {exc}", file=sys.stderr)
        return []
    return [
        f"![chart](img/{path.name})",
        "",
        "（実線: 評価額（青=積立、橙=期初一括）、破線: 積立の累計投資額、点: 買付日）",
        "",
    ]


def build_dca_report(args: argparse.Namespace, img_stem: str | None = None) -> str:
    """積立（ドルコスト平均法）バックテストのレポート本文（Markdown）を構築する。"""
    if args.split is not None or args.sweep:
        raise ValueError("--split / --sweep は --strategy dca では使用できません")
    if args.in_currency is not None:
        raise ValueError("--in-currency は --strategy dca では未対応です（円建てのみ）")

    code: str = args.code
    df = fetch_prices(code, period=args.period, synthetic=args.synthetic)[code]
    cmp = compare_dca_lump_sum(
        df, args.monthly, day_of_month=args.day_of_month, cost_bps=args.cost_bps
    )
    dca, lump = cmp.dca, cmp.lump_sum
    close = df["Close"].dropna()

    lines: list[str] = [
        report.report_header(f"積立（ドルコスト平均法）バックテスト: {code}")
    ]
    lines.append(f"- 期間: {args.period}（{_span(close)}）")
    lines.append(
        f"- 買付ルール: 毎月 {args.day_of_month} 日（非営業日・月に無い日は翌営業日に繰越）の"
        f"終値で {report.fmt_num(float(args.monthly), 0)} 円を定額買付（端株許容の金額ベース）"
    )
    lines.append(
        f"- 取引コスト: 片道 {args.cost_bps} bps（買付金額から控除して株数換算。"
        "平均取得単価はコスト込み）"
    )
    lines.append("- 配当は考慮しない（無配再投資なしの価格リターンのみ）")
    if args.synthetic:
        lines.append("- **データ: 合成データ（--synthetic、実在の株価ではありません）**")
    lines.append("")

    lines.append("## 積立シミュレーション結果")
    lines.append("")
    price_discount = dca.avg_cost / dca.avg_buy_price - 1.0
    below_avg = int((dca.buy_prices < dca.avg_buy_price).sum())
    dca_rows: list[list[object]] = [
        ["買付回数", dca.n_buys],
        ["累計投資額", f"{report.fmt_num(dca.total_invested, 0)} 円"],
        ["最終評価額", f"{report.fmt_num(dca.final_value, 0)} 円"],
        ["トータル損益率", report.fmt_pct(dca.total_return)],
        ["最終保有株数（端株許容）", report.fmt_num(dca.total_shares, 2)],
        ["平均取得単価（コスト込み）", f"{report.fmt_num(dca.avg_cost, 2)} 円"],
        ["買付日終値の単純平均", f"{report.fmt_num(dca.avg_buy_price, 2)} 円"],
        ["期間中の最低損益率", report.fmt_pct(dca.min_pnl)],
        ["損益率の最大下落幅（%pt）", report.fmt_pct(dca.max_drawdown_pp)],
    ]
    lines.append(report.markdown_table(["指標", "値"], dca_rows))
    lines.append("")

    lines.append("## 積立 vs 期初一括の比較（同一総投資額）")
    lines.append("")
    lines.append(
        f"- 一括側は積立の累計投資額と同額（{report.fmt_num(dca.total_invested, 0)} 円）を"
        "**期初の終値で一括投資**した参照値。「期初に全額を用意できた」という後知恵の前提であり、"
        "毎月の収入から拠出する現実の積立投資家には選べないことが多い点に注意。"
    )
    lines.append("")
    cmp_rows: list[list[object]] = [
        [
            "最終評価額",
            f"{report.fmt_num(dca.final_value, 0)} 円",
            f"{report.fmt_num(lump.final_value, 0)} 円",
        ],
        ["トータル損益率", report.fmt_pct(dca.total_return), report.fmt_pct(lump.total_return)],
        [
            "投資効率（最終評価額÷投資額）",
            report.fmt_num(dca.final_value / dca.total_invested, 3),
            report.fmt_num(lump.final_value / lump.amount, 3),
        ],
        [
            "平均取得単価（コスト込み）",
            f"{report.fmt_num(dca.avg_cost, 2)} 円",
            f"{report.fmt_num(lump.avg_cost, 2)} 円",
        ],
        ["期間中の最低損益率", report.fmt_pct(dca.min_pnl), report.fmt_pct(lump.min_pnl)],
        [
            "損益率の最大下落幅（%pt）",
            report.fmt_pct(dca.max_drawdown_pp),
            report.fmt_pct(lump.max_drawdown_pp),
        ],
    ]
    lines.append(report.markdown_table(["指標", "積立（DCA）", "期初一括"], cmp_rows))
    lines.append("")
    lines.append(
        "- 損益率の最大下落幅は「損益率のピークからの下落幅（パーセントポイント）」で、"
        "積立・一括を同じ物差しで比較するための定義。積立側は追加拠出が損益率を"
        "希薄化する効果を含むため保守的な値になる。"
    )
    lines.append("")

    lines.append("## 下落局面での買付効果")
    lines.append("")
    lines.append(
        f"- 平均取得単価 {report.fmt_num(dca.avg_cost, 2)} 円は買付日終値の単純平均 "
        f"{report.fmt_num(dca.avg_buy_price, 2)} 円に対して {report.fmt_pct(price_discount)}。"
        "定額買付は安値で多くの株数・高値で少ない株数を買うため、取得単価は買付価格の"
        "調和平均 $\\bar{P}_{HM} = n / \\sum_i (1/P_i)$ に近づき、"
        "単純平均を上回らない（取引コスト分を除く）。"
    )
    lines.append(
        f"- 単純平均を下回る価格での買付: {below_avg} / {dca.n_buys} 回。"
        "下落局面の買付が多いほど取得単価の平準化効果は大きいが、"
        "それは下落局面を経験したことの裏返しでもある。"
    )
    lines.append("")

    if img_stem is not None:
        lines.extend(_dca_chart_lines(cmp, code, img_stem))

    lines.append("## 積立と一括をどう読むか（判断の枠組み）")
    lines.append("")
    lines.append(
        "- **実証知見**: 株式市場は長期では上昇してきた期間が多いため、「期初一括が積立を"
        "上回る確率が歴史的には高い」ことが複数の実証研究で知られている（資金を早く・長く"
        "市場に晒すほど期待リターンを取り込めるため）。本レポートの結果が一括優位でも、"
        "それ自体は歴史的傾向の再確認にすぎない。"
    )
    lines.append(
        "- **積立の価値はリターン最大化ではない**: (1) 毎月の収入から拠出する現実の資金制約に"
        "合致する、(2) 高値掴みの後悔やタイミング判断の心理的負担を減らし**行動の継続性**を"
        "高める、(3) 取得単価を平準化し特定時点への依存を減らす**リスク平準化**——という"
        "運用の継続装置としての価値が本質である。整理は "
        "`knowledge/strategies/long-term-wealth-building.md` を参照。"
    )
    lines.append(
        "- **期間依存性**: 本結果は対象期間のレジームに強く依存する（右肩上がりの期間は一括"
        "優位、期初に下落しその後回復する期間は積立優位になりやすい）。単一銘柄・単一期間の"
        "比較で手法の優劣を結論しないこと。"
    )
    lines.append(
        "- **個別株の積立は分散投資ではない**: 時間分散はしても銘柄集中リスクは残る。"
        "指数連動商品の積立とは別物として読むこと。"
    )
    lines.append("")
    return "\n".join(lines)


def build_report(args: argparse.Namespace, img_stem: str | None = None) -> str:
    """バックテストレポート本文（Markdown）を構築する。

    Args:
        img_stem: チャート PNG のファイル名接頭辞（``reports/img/<img_stem>-equity.png``）。
            ``None`` の場合はチャートを生成しない。
    """
    if args.strategy == "dca":
        return build_dca_report(args, img_stem=img_stem)
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

    if args.in_currency is not None:
        lines.extend(_fx_section(result, close, args.period, args.synthetic, args.in_currency))

    if img_stem is not None:
        lines.extend(
            _chart_lines(
                result.equity_curve,
                f"{code} {args.strategy}: Equity & Drawdown",
                img_stem,
            )
        )

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
    parser.add_argument(
        "--macd-fast", type=int, default=12, help="[macd] 短期 EMA 期間（既定: 12）"
    )
    parser.add_argument(
        "--macd-slow", type=int, default=26, help="[macd] 長期 EMA 期間（既定: 26）"
    )
    parser.add_argument(
        "--macd-signal", type=int, default=9, help="[macd] シグナル線 EMA 期間（既定: 9）"
    )
    parser.add_argument(
        "--bb-window", type=int, default=20, help="[bollinger_reversal] 移動平均・σの期間（既定: 20）"
    )
    parser.add_argument(
        "--bb-std", type=float, default=2.0,
        help="[bollinger_reversal] バンド幅の標準偏差倍率（既定: 2.0）",
    )
    parser.add_argument(
        "--monthly", type=float, default=30000.0,
        help="[dca] 毎月の買付金額（円。既定: 30000）",
    )
    parser.add_argument(
        "--day-of-month", type=int, default=1,
        help="[dca] 目標買付日（1〜31。非営業日・月に無い日は翌営業日に繰越。既定: 1）",
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
    add_source_argument(parser)
    parser.add_argument("--no-charts", action="store_true", help="チャート画像の生成・埋め込みを無効化する")
    parser.add_argument(
        "--in-currency",
        type=str.upper,
        choices=sorted(currency.SUPPORTED_CURRENCIES),
        default=None,
        help="基準通貨建て評価節を追加する（海外投資家視点。シグナルは円建て価格で計算し、"
        "日次リターンをクロス円レート（例: EURJPY=X）の同日終値で基準通貨建てに換算して併記する）",
    )
    parser.add_argument(
        "--in-usd",
        action="store_true",
        help="--in-currency USD のエイリアス（後方互換）",
    )
    args = parser.parse_args(argv)
    set_default_source(args.source)
    if args.in_currency is None and args.in_usd:
        args.in_currency = "USD"

    filename = f"backtest-{args.strategy}-{args.code}-{dt.date.today().isoformat()}.md"
    img_stem = None if args.no_charts else filename.removesuffix(".md")
    try:
        content = build_report(args, img_stem=img_stem)
    except (DataFetchError, ValueError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    print(report.with_disclaimer(content))
    path = report.save_report(content, filename)
    print(f"レポート: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

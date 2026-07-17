#!/usr/bin/env python3
"""資産形成プランニング CLI（積立予測 / 目標逆算 / 進捗確認 / 取り崩し）。

使い方（リポジトリルートから）:
    python3 analysis/asset_plan.py project --monthly 50000 --years 20 --return 5 --vol 15 [--inflation 1] [--nisa]
    python3 analysis/asset_plan.py goal --target 30000000 --years 25 --return 4 [--vol 15] [--initial 1000000]
    python3 analysis/asset_plan.py progress --target 30000000 --years 15 --current 8000000 --monthly 70000 --return 4
    python3 analysis/asset_plan.py decumulate --initial 30000000 --years 30 --return 3 --vol 12 --monthly 120000
    python3 analysis/asset_plan.py decumulate --initial 30000000 --years 30 --return 3 --vol 12 --rate 4

全サブコマンド共通の ``--cost <年率%>``（信託報酬・実質コスト）を指定すると、
想定リターンからコストを控除した実効リターン $(1+R)(1-c)-1$ で計算し、
前提表に控除後リターンを明記する（``stocklib.planning.net_of_cost_return``）。

価格データを使わないネットワーク不要のツール。ロジックは ``stocklib.planning``、
ファンチャート PNG は ``stocklib.charts`` の設定（IMG_DIR / DPI / 配色）を再利用して
``reports/img/`` に出力し、``reports/plan-<サブコマンド>-<日付>.md`` に埋め込む。
レポートパスを stdout の最終行に出力する。

リターン・ボラ・インフレ率は**パーセント**で指定する（`--return 5` = 年率5%）。
想定リターンはユーザー入力の仮定であり、レポートには将来の保証ではない旨が
必ず明記される（``stocklib.planning.ASSUMPTION_NOTE``）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import numpy as np

from stocklib import charts, planning, report

MONEY = 0  # 金額の表示桁数（円、整数）


def _pct(value: float) -> float:
    """パーセント入力（5 = 5%）を比率（0.05）に変換する。"""
    return value / 100.0


def _fmt_money(value: float) -> str:
    return report.fmt_num(float(value), MONEY)


def _effective_return(args: argparse.Namespace) -> float:
    """コスト控除後の実効年率リターン（比率）。``--cost 0``（既定）なら想定リターンそのまま。"""
    return planning.net_of_cost_return(_pct(args.annual_return), _pct(args.cost))


def _return_assumption_rows(args: argparse.Namespace) -> list[list[object]]:
    """前提表の想定リターン・コスト行（``--cost`` 指定時は控除後リターンを明記）。"""
    rows: list[list[object]] = [["想定年率リターン", f"{args.annual_return:g}%"]]
    rows.append(["年率コスト（信託報酬等、--cost）", f"{args.cost:g}%"])
    if args.cost != 0:
        rows.append(
            ["コスト控除後リターン（計算に使用）", f"{_effective_return(args) * 100:.3f}%"]
        )
    return rows


# --------------------------------------------------------------------------
# ファンチャート（stocklib.charts の設定を再利用）
# --------------------------------------------------------------------------


def _plot_fan_chart(
    months: np.ndarray,
    percentiles: dict[int, np.ndarray],
    deterministic: np.ndarray,
    out_path: Path,
    title: str,
    contributions: np.ndarray | None = None,
) -> Path:
    """パーセンタイル帯（5–95 / 25–75）+ 中央値 + 決定論のファンチャートを PNG 出力する。

    軸ラベル・凡例は豆腐化回避のため英数字表記（:mod:`stocklib.charts` の方針に従う）。
    """
    import matplotlib.pyplot as plt  # charts の import で Agg バックエンド設定済み

    years_axis = months / planning.MONTHS_PER_YEAR
    fig, ax = plt.subplots(figsize=(10, 5), constrained_layout=True)
    band = "#2a6fdb"
    ax.fill_between(
        years_axis, percentiles[5], percentiles[95],
        color=band, alpha=0.12, linewidth=0, label="P5-P95",
    )
    ax.fill_between(
        years_axis, percentiles[25], percentiles[75],
        color=band, alpha=0.25, linewidth=0, label="P25-P75",
    )
    ax.plot(years_axis, percentiles[50], color=band, linewidth=1.8, label="Median (P50)")
    ax.plot(
        years_axis, deterministic,
        color=charts.LINE_COLORS[0], linewidth=1.4, linestyle="--", label="Deterministic",
    )
    if contributions is not None:
        ax.plot(
            years_axis, contributions,
            color="#6b7280", linewidth=1.2, linestyle=":", label="Contributions",
        )
    ax.set_xlabel("Years")
    ax.set_ylabel("Assets (JPY)")
    ax.set_title(title)
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    ax.grid(True, alpha=0.3, linewidth=0.5)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda v, _pos: f"{v:,.0f}")
    )

    path = Path(out_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=charts.DPI)
    plt.close(fig)
    return path


def _fan_chart_lines(
    months: np.ndarray,
    percentiles: dict[int, np.ndarray],
    deterministic: np.ndarray,
    img_stem: str,
    title: str,
    no_charts: bool,
    contributions: np.ndarray | None = None,
) -> list[str]:
    """ファンチャートを生成し、レポート埋め込み用の Markdown 行を返す（失敗時は空）。"""
    if no_charts:
        return []
    if not charts.charts_available():
        print("警告: matplotlib が利用できないため、チャートなしで続行します", file=sys.stderr)
        return []
    try:
        path = _plot_fan_chart(
            months, percentiles, deterministic,
            charts.IMG_DIR / f"{img_stem}.png", title, contributions=contributions,
        )
    except Exception as exc:  # チャートは補助情報。失敗してもレポート生成は続行する
        print(f"警告: チャート生成に失敗しました（チャートなしで続行します）: {exc}", file=sys.stderr)
        return []
    return [
        "## ファンチャート",
        "",
        f"![fan chart](img/{path.name})",
        "",
        "（帯: モンテカルロのパーセンタイル P5–P95（薄）/ P25–P75（濃）、実線: 中央値、"
        "破線: 決定論的複利。点線があれば累計元本）",
        "",
    ]


# --------------------------------------------------------------------------
# 共通のレポート部品
# --------------------------------------------------------------------------


def _method_note_lines(n_paths: int, seed: int, cost_pct: float = 0.0) -> list[str]:
    """手法・前提の注意書き（全レポート共通）。"""
    if cost_pct != 0:
        cost_line = (
            f"- 信託報酬等の年率コスト {cost_pct:g}% は想定リターンから控除済み"
            "（実効リターン $(1+R)(1-c)-1$）。税は考慮していない（NISA 比較の節を除く）。"
            "コストの長期的影響（複利で効く）の定量化は "
            "`knowledge/market-structure/investment-trusts-and-asset-management.md` を参照。"
        )
    else:
        cost_line = (
            "- 税・手数料・信託報酬は考慮していない（NISA 比較の節を除く。"
            "信託報酬等は `--cost <年率%>` で想定リターンから控除できる）。"
        )
    return [
        "## 手法と前提の注意",
        "",
        planning.ASSUMPTION_NOTE,
        "",
        f"- モンテカルロ: 月次リターンを対数正規（独立同分布）で {n_paths:,} パス生成"
        f"（シード {seed} で固定、再現可能）。実際の市場リターンには自己相関・"
        "ファットテール・レジーム変化があり、正規近似は極端な下落の確率を過小評価しうる。",
        "- 期待値が決定論的複利と一致するパラメータ化のため、**中央値は決定論的複利を"
        "下回る**（ボラティリティ・ドラッグ $\\ln(1+R)/12 - \\sigma_m^2/2$）。"
        "「決定論の線」は楽観側の目安として読む。",
        cost_line,
        "- 想定リターンの置き方は `knowledge/strategies/long-term-wealth-building.md`、"
        "リスク許容度との整合は "
        "`knowledge/strategies/household-risk-capacity-and-allocation.md` を参照。",
        "",
    ]


def _percentile_table(result_percentiles: dict[int, np.ndarray], deflator_last: float) -> str:
    """最終時点のパーセンタイル表（実質列は deflator_last != 1 のときのみ意味を持つ）。"""
    headers = ["パーセンタイル", "最終資産（名目・円）"]
    with_real = deflator_last != 1.0
    if with_real:
        headers.append("最終資産（実質・円）")
    rows: list[list[object]] = []
    labels = {5: "P5（悲観側）", 25: "P25", 50: "P50（中央値）", 75: "P75", 95: "P95（楽観側）"}
    for p in planning.PERCENTILES:
        v = float(result_percentiles[p][-1])
        row: list[object] = [labels[p], _fmt_money(v)]
        if with_real:
            row.append(_fmt_money(v / deflator_last))
        rows.append(row)
    return report.markdown_table(headers, rows)


def _nisa_lines(gain_median: float, gain_det: float, monthly: float,
                total_contribution: float, years: float) -> list[str]:
    """NISA 非課税メリットの節（--nisa 指定時）。"""
    b_med = planning.nisa_tax_benefit(gain_median)
    b_det = planning.nisa_tax_benefit(gain_det)
    lines = [
        "## NISA 非課税メリット（課税口座との比較、2025年時点の制度）",
        "",
        f"課税口座の税率: {b_med.tax_rate * 100:.3f}%"
        "（所得税15% + 復興特別所得税0.315% + 住民税5%、2025年時点）",
        "",
        report.markdown_table(
            ["シナリオ", "運用益（円）", "課税口座の税額（円）", "課税口座の税引後益（円）", "NISA非課税メリット（円）"],
            [
                [
                    "中央値（P50）", _fmt_money(b_med.gain), _fmt_money(b_med.tax_in_taxable),
                    _fmt_money(b_med.after_tax_gain_taxable), _fmt_money(b_med.benefit),
                ],
                [
                    "決定論的複利", _fmt_money(b_det.gain), _fmt_money(b_det.tax_in_taxable),
                    _fmt_money(b_det.after_tax_gain_taxable), _fmt_money(b_det.benefit),
                ],
            ],
        ),
        "",
        "- NISA の非課税メリット = 課税口座なら売却時に払う税額。運用益が大きいほど"
        "（積立額・期間・リターンが大きいほど）メリットも大きくなる。",
        "- **損失時の非対称性**: NISA の損失は課税口座と損益通算・繰越控除ができない"
        "（2025年時点）。運用益が出ない場合、非課税メリットはゼロで、損失の税務上の"
        "扱いはむしろ不利になる。",
    ]
    annual = monthly * 12
    if annual > planning.NISA_TSUMITATE_ANNUAL_LIMIT:
        lines.append(
            f"- 年間積立額 {_fmt_money(annual)} 円は**つみたて投資枠の年間上限 "
            f"{planning.NISA_TSUMITATE_ANNUAL_LIMIT:,} 円（月10万円）を超える**（2025年時点）。"
            f"成長投資枠（年 {planning.NISA_GROWTH_ANNUAL_LIMIT:,} 円）との併用で"
            f"年 {planning.NISA_ANNUAL_LIMIT_TOTAL:,} 円まで拡張できるが、"
            "超過分は課税口座での積立になる。"
        )
    if total_contribution > planning.NISA_LIFETIME_LIMIT:
        lines.append(
            f"- 総元本 {_fmt_money(total_contribution)} 円は**生涯非課税保有限度額 "
            f"{planning.NISA_LIFETIME_LIMIT:,} 円（簿価ベース、2025年時点）を超える**。"
            "超過分は課税口座での運用になり、上表のメリットは満額では得られない。"
        )
    lines.append("")
    return lines


# --------------------------------------------------------------------------
# サブコマンド: project（積立予測）
# --------------------------------------------------------------------------


def cmd_project(args: argparse.Namespace) -> int:
    result = planning.compound_projection(
        monthly_amount=args.monthly,
        years=args.years,
        annual_return=_effective_return(args),
        annual_vol=_pct(args.vol),
        initial=args.initial,
        inflation=_pct(args.inflation),
        n_paths=args.paths,
        seed=args.seed,
    )
    today = dt.date.today().isoformat()
    deflator_last = float(result.deflator[-1])
    median_final = float(result.percentiles[50][-1])
    det_final = float(result.deterministic[-1])

    lines = [report.report_header("資産形成プラン: 積立シミュレーション")]
    lines += [
        "## 前提（ユーザー入力）",
        "",
        report.markdown_table(
            ["項目", "値"],
            [
                ["毎月の積立額", f"{_fmt_money(args.monthly)} 円"],
                ["初期資産", f"{_fmt_money(args.initial)} 円"],
                ["期間", f"{args.years:g} 年（{int(result.months[-1])} ヶ月）"],
                *_return_assumption_rows(args),
                ["想定年率ボラティリティ", f"{args.vol:g}%"],
                ["想定インフレ率", f"{args.inflation:g}%"],
                ["モンテカルロ", f"{args.paths:,} パス / シード {args.seed}"],
            ],
        ),
        "",
        planning.ASSUMPTION_NOTE,
        "",
        "## 結果",
        "",
        f"- 総元本（初期資産 + 累計拠出）: **{_fmt_money(result.total_contribution)} 円**",
        f"- 決定論的複利の最終資産: **{_fmt_money(det_final)} 円**"
        f"（運用益 {_fmt_money(det_final - result.total_contribution)} 円）",
        f"- モンテカルロ中央値（P50）: **{_fmt_money(median_final)} 円**",
        f"- 名目の元本割れ確率（最終時点で総元本を下回る確率）: "
        f"**{result.shortfall_prob * 100:.1f}%**",
        "",
        _percentile_table(result.percentiles, deflator_last),
        "",
    ]
    if args.inflation != 0:
        lines += [
            f"- 実質列はインフレ率 {args.inflation:g}% で現在の購買力に割り引いた値"
            f"（{args.years:g} 年後の物価水準は現在の {deflator_last:.2f} 倍）。",
            "",
        ]
    if args.nisa:
        lines += _nisa_lines(
            gain_median=median_final - result.total_contribution,
            gain_det=det_final - result.total_contribution,
            monthly=args.monthly,
            total_contribution=result.total_contribution,
            years=args.years,
        )
    lines += _fan_chart_lines(
        result.months, result.percentiles, result.deterministic,
        img_stem=f"plan-project-{today}",
        title=(
            f"Projection: {args.monthly:,.0f} JPY/mo, {args.years:g}y, "
            f"r={args.annual_return:g}%, vol={args.vol:g}%"
        ),
        no_charts=args.no_charts,
        contributions=result.contributions,
    )
    lines += _method_note_lines(args.paths, args.seed, cost_pct=args.cost)

    content = "\n".join(lines)
    path = report.save_report(content, f"plan-project-{today}.md")
    print(f"総元本 {_fmt_money(result.total_contribution)} 円 → "
          f"中央値 {_fmt_money(median_final)} 円 / 決定論 {_fmt_money(det_final)} 円 "
          f"(元本割れ確率 {result.shortfall_prob * 100:.1f}%)")
    print(path)
    return 0


# --------------------------------------------------------------------------
# サブコマンド: goal（目標額からの逆算）
# --------------------------------------------------------------------------


def cmd_goal(args: argparse.Namespace) -> int:
    required = planning.required_monthly_saving(
        target_amount=args.target,
        years=args.years,
        annual_return=_effective_return(args),
        initial=args.initial,
    )
    today = dt.date.today().isoformat()

    # 想定リターンへの感応度（±1pt / ±2pt）: 「前提を問い直す」ための材料
    # （感応度はコスト控除前の想定リターンを動かし、控除後リターンで計算する）
    sens_rows: list[list[object]] = []
    for delta in (-2.0, -1.0, 0.0, 1.0, 2.0):
        r = args.annual_return + delta
        if r <= -100.0:
            continue
        p = planning.required_monthly_saving(
            args.target, args.years,
            planning.net_of_cost_return(_pct(r), _pct(args.cost)),
            initial=args.initial,
        )
        label = f"{r:g}%" + ("（指定値）" if delta == 0.0 else "")
        sens_rows.append([label, f"{_fmt_money(p)} 円"])

    lines = [report.report_header("資産形成プラン: 目標額からの逆算")]
    lines += [
        "## 前提（ユーザー入力）",
        "",
        report.markdown_table(
            ["項目", "値"],
            [
                ["目標額", f"{_fmt_money(args.target)} 円"],
                ["初期資産", f"{_fmt_money(args.initial)} 円"],
                ["期間", f"{args.years:g} 年"],
                *_return_assumption_rows(args),
                ["想定年率ボラティリティ", f"{args.vol:g}%（達成確率の推定に使用。0なら省略）"],
            ],
        ),
        "",
        planning.ASSUMPTION_NOTE,
        "",
        "## 必要積立額",
        "",
        f"- 毎月の必要積立額（決定論的複利、月末拠出）: **{_fmt_money(required)} 円**",
        f"- 総拠出額: {_fmt_money(required * args.years * 12)} 円"
        f"（+ 初期資産 {_fmt_money(args.initial)} 円）",
        "",
        "### 想定リターンへの感応度",
        "",
        report.markdown_table(["想定年率リターン", "毎月の必要積立額"], sens_rows),
        "",
        "- 必要積立額はリターン前提に大きく依存する。**想定を1〜2ポイント下げても"
        "家計が続けられる金額か**を確認する（前提の楽観に計画を依存させない）。",
        "",
    ]

    if args.vol > 0 and required > 0:
        proj = planning.compound_projection(
            monthly_amount=required,
            years=args.years,
            annual_return=_effective_return(args),
            annual_vol=_pct(args.vol),
            initial=args.initial,
            inflation=_pct(args.inflation),
            n_paths=args.paths,
            seed=args.seed,
        )
        achieve_prob = float(np.mean(proj.final_values >= args.target))
        lines += [
            "## 達成確率（モンテカルロ）",
            "",
            f"上の必要積立額 {_fmt_money(required)} 円で毎月積み立てた場合、"
            f"想定ボラティリティ {args.vol:g}% の下で目標 {_fmt_money(args.target)} 円に"
            f"到達する確率は **{achieve_prob * 100:.1f}%**（{args.paths:,} パス推定）。",
            "",
            "- 決定論的複利は「期待値どおりに毎年増える」仮定なので、ボラティリティが"
            "あると達成確率は 50% を下回るのが通常（中央値 < 期待値）。"
            "確実性を高めたい場合は、積立額を増やす・期間を延ばす・目標を下げる、の"
            "いずれかのトレードオフになる。",
            "",
            _percentile_table(proj.percentiles, float(proj.deflator[-1])),
            "",
        ]
        lines += _fan_chart_lines(
            proj.months, proj.percentiles, proj.deterministic,
            img_stem=f"plan-goal-{today}",
            title=(
                f"Goal: target {args.target:,.0f} JPY in {args.years:g}y "
                f"(required {required:,.0f} JPY/mo, r={args.annual_return:g}%)"
            ),
            no_charts=args.no_charts,
            contributions=proj.contributions,
        )
    elif required == 0:
        lines += [
            "- 初期資産の複利成長だけで目標に到達する想定のため、追加の積立は不要という"
            "計算結果（ただし想定リターンが実現しない場合はこの限りではない）。",
            "",
        ]

    lines += _method_note_lines(args.paths, args.seed, cost_pct=args.cost)
    content = "\n".join(lines)
    path = report.save_report(content, f"plan-goal-{today}.md")
    print(f"目標 {_fmt_money(args.target)} 円 / {args.years:g} 年 → "
          f"毎月 {_fmt_money(required)} 円（想定リターン {args.annual_return:g}%）")
    print(path)
    return 0


# --------------------------------------------------------------------------
# サブコマンド: progress（目標への現在地・要求リターン逆算）
# --------------------------------------------------------------------------


def cmd_progress(args: argparse.Namespace) -> int:
    net = _effective_return(args)
    required_net = planning.required_annual_return(
        args.target, args.years, current=args.current, monthly_amount=args.monthly
    )
    cost_ratio = _pct(args.cost)
    # 要求リターンは「コスト控除後に必要なリターン」。コストがある場合、
    # 控除前（商品のグロスリターン）ではさらに高いリターンが必要になる。
    required_gross = (1.0 + required_net) / (1.0 - cost_ratio) - 1.0

    proj = planning.compound_projection(
        monthly_amount=args.monthly,
        years=args.years,
        annual_return=net,
        annual_vol=_pct(args.vol),
        initial=args.current,
        inflation=_pct(args.inflation),
        n_paths=args.paths,
        seed=args.seed,
    )
    achieve_prob = float(np.mean(proj.final_values >= args.target))
    today = dt.date.today().isoformat()

    n_months = int(proj.months[-1])
    total_principal = args.current + args.monthly * n_months  # 現在資産 + 今後の拠出
    gap_pt = (net - required_net) * 100.0  # 想定（控除後） − 要求（控除後）、pt
    median_final = float(proj.percentiles[50][-1])
    det_final = float(proj.deterministic[-1])

    lines = [report.report_header("資産形成プラン: 目標への進捗確認（要求リターン逆算）")]
    lines += [
        "## 前提（ユーザー入力）",
        "",
        report.markdown_table(
            ["項目", "値"],
            [
                ["目標額", f"{_fmt_money(args.target)} 円"],
                ["現在の資産評価額", f"{_fmt_money(args.current)} 円"],
                ["毎月の積立額", f"{_fmt_money(args.monthly)} 円"],
                ["残り期間", f"{args.years:g} 年（{n_months} ヶ月）"],
                *_return_assumption_rows(args),
                ["想定年率ボラティリティ", f"{args.vol:g}%"],
                ["想定インフレ率", f"{args.inflation:g}%"],
                ["モンテカルロ", f"{args.paths:,} パス / シード {args.seed}"],
            ],
        ),
        "",
        planning.ASSUMPTION_NOTE,
        "",
        "## 現在地",
        "",
        f"- 目標額に対する現在資産の比率: **{args.current / args.target * 100:.1f}%**"
        f"（{_fmt_money(args.current)} 円 / {_fmt_money(args.target)} 円）",
        f"- 元本合計の見込み（現在資産 + 今後の拠出 {_fmt_money(args.monthly * n_months)} 円）: "
        f"**{_fmt_money(total_principal)} 円**"
        f"（目標との差 {_fmt_money(args.target - total_principal)} 円。"
        + (
            "元本だけでは目標に届かず、この差を運用リターンで埋める必要がある）"
            if total_principal < args.target
            else "元本合計だけで目標額を上回る）"
        ),
        "",
        "## 目標達成に必要な年率リターン（逆算）",
        "",
        f"- 要求年率リターン: **{required_net * 100:.2f}%**"
        "（決定論的複利・月末拠出で最終資産が目標額に一致するリターンの数値解。"
        "コスト控除後ベース）",
    ]
    if args.cost != 0:
        lines.append(
            f"- 信託報酬等（年率 {args.cost:g}%）の控除前では **{required_gross * 100:.2f}%** "
            "のグロスリターンが必要（コストの分だけ要求水準が上がる）。"
        )
    direction = "上回っている" if gap_pt >= 0 else "下回っている"
    lines += [
        f"- ユーザー想定リターン（コスト控除後 {net * 100:.2f}%）は要求リターンを "
        f"**{abs(gap_pt):.2f} ポイント{direction}**。",
    ]
    if required_net > planning.REQUIRED_RETURN_CAUTION_THRESHOLD:
        lines.append(
            f"- 注記: 要求リターン {required_net * 100:.2f}% は、歴史的な株式リターンの参考値"
            f"（世界株の実質リターン年率約 {planning.HISTORICAL_EQUITY_REAL_RETURN * 100:g}%・"
            "幾何平均、1900年以降の長期データ、2024年時点。"
            "`knowledge/strategies/long-term-wealth-building.md` 参照）を大きく超えている。"
            "これは達成可否の断定ではなく、**積立額または期間（あるいは目標額）の見直しの"
            "検討材料**である。なお参考値は実質・要求リターンは名目のため、"
            "インフレ分だけ名目の比較は緩めに見える点にも注意。"
        )
    elif required_net <= 0:
        lines.append(
            "- 要求リターンが 0% 以下であることは、元本の積み上げだけで目標水準に届く"
            "計算であることを意味する（ただし将来の積立の継続が前提）。"
        )
    lines += [
        "",
        "## 目標到達確率（モンテカルロ）",
        "",
        f"現在資産 {_fmt_money(args.current)} 円から毎月 {_fmt_money(args.monthly)} 円を"
        f"積み立て、想定リターン（控除後）{net * 100:.2f}%・ボラティリティ {args.vol:g}% "
        f"の下で {args.years:g} 年後に目標 {_fmt_money(args.target)} 円に到達する確率は "
        f"**{achieve_prob * 100:.1f}%**（{args.paths:,} パス推定）。",
        "",
        f"- 決定論的複利の最終資産: {_fmt_money(det_final)} 円 / "
        f"モンテカルロ中央値（P50）: {_fmt_money(median_final)} 円",
        "",
        _percentile_table(proj.percentiles, float(proj.deflator[-1])),
        "",
        "- 到達確率はモデル（対数正規・独立同分布）の仮定の産物であり、"
        "保証でも予測でもない。確率を高める手段は、積立額を増やす・期間を延ばす・"
        "目標を下げる・リスク（ボラ）を下げる、のいずれかのトレードオフになる。",
        "",
    ]
    lines += _fan_chart_lines(
        proj.months, proj.percentiles, proj.deterministic,
        img_stem=f"plan-progress-{today}",
        title=(
            f"Progress: {args.current:,.0f} + {args.monthly:,.0f} JPY/mo, "
            f"{args.years:g}y, target {args.target:,.0f} JPY"
        ),
        no_charts=args.no_charts,
        contributions=proj.contributions,
    )
    lines += _method_note_lines(args.paths, args.seed, cost_pct=args.cost)

    content = "\n".join(lines)
    path = report.save_report(content, f"plan-progress-{today}.md")
    print(f"現在 {_fmt_money(args.current)} 円 + 毎月 {_fmt_money(args.monthly)} 円 × "
          f"{args.years:g} 年 → 要求リターン 年率 {required_net * 100:.2f}%"
          f"（想定 {net * 100:.2f}% / 到達確率 {achieve_prob * 100:.1f}%）")
    print(path)
    return 0


# --------------------------------------------------------------------------
# サブコマンド: decumulate（取り崩し）
# --------------------------------------------------------------------------


def cmd_decumulate(args: argparse.Namespace) -> int:
    result = planning.decumulation_simulation(
        initial=args.initial,
        years=args.years,
        annual_return=_effective_return(args),
        annual_vol=_pct(args.vol),
        monthly_withdrawal=args.monthly,
        annual_withdrawal_rate=_pct(args.rate) if args.rate is not None else None,
        inflation=_pct(args.inflation),
        inflation_linked=args.inflation_linked,
        n_paths=args.paths,
        seed=args.seed,
    )
    today = dt.date.today().isoformat()
    mode_label = (
        f"定額 {_fmt_money(args.monthly)} 円/月"
        + ("（インフレ連動増額）" if result.inflation_linked else "")
        if result.mode == "fixed_amount"
        else f"定率 年 {args.rate:g}%（月次 {args.rate / 12:g}% ずつ）"
    )
    deflator_last = float(result.deflator[-1])
    median_final = float(result.percentiles[50][-1])

    lines = [report.report_header("資産形成プラン: 取り崩しシミュレーション")]
    lines += [
        "## 前提（ユーザー入力）",
        "",
        report.markdown_table(
            ["項目", "値"],
            [
                ["開始時資産", f"{_fmt_money(args.initial)} 円"],
                ["取り崩し方式", mode_label],
                ["期間", f"{args.years:g} 年（{int(result.months[-1])} ヶ月）"],
                *_return_assumption_rows(args),
                ["想定年率ボラティリティ", f"{args.vol:g}%"],
                ["想定インフレ率", f"{args.inflation:g}%"],
                ["モンテカルロ", f"{args.paths:,} パス / シード {args.seed}"],
            ],
        ),
        "",
        planning.ASSUMPTION_NOTE,
        "",
        "## 結果",
        "",
        f"- 期間内の枯渇確率: **{result.depletion_prob * 100:.1f}%**"
        + (
            f"（枯渇したパスの枯渇時期の中央値: 約 {result.depletion_month_median / 12:.1f} 年目）"
            if result.depletion_month_median is not None
            else ""
        ),
        f"- 最終残高の中央値（P50）: **{_fmt_money(median_final)} 円**",
        f"- 決定論（一定リターン）での最終残高: {_fmt_money(float(result.deterministic[-1]))} 円",
        "",
        _percentile_table(result.percentiles, deflator_last),
        "",
    ]

    # 年次の累積枯渇確率（5年刻み + 最終年）
    n = int(result.months[-1])
    year_marks = sorted({y for y in range(5, int(args.years) + 1, 5)} | {int(args.years)})
    ruin_rows = [
        [f"{y} 年目", f"{float(result.depletion_prob_by_month[min(y * 12, n)]) * 100:.1f}%"]
        for y in year_marks
        if y >= 1
    ]
    if ruin_rows:
        lines += [
            "### 累積枯渇確率の推移",
            "",
            report.markdown_table(["経過年数", "その時点までに枯渇している確率"], ruin_rows),
            "",
        ]

    if result.mode == "fixed_rate":
        assert result.withdrawal_median is not None
        first_w = float(result.withdrawal_median[0])
        last_w = float(result.withdrawal_median[-1])
        lines += [
            "### 定率方式の注意: 受取額は一定ではない",
            "",
            f"- 定率では残高に比例して受取額が変わる。月次受取額の中央値は初月 "
            f"{_fmt_money(first_w)} 円 → 最終月 {_fmt_money(last_w)} 円。",
            "- 数学的に残高が 0 にはならない（枯渇確率 0%）が、**受取額が生活費を"
            "下回るリスク**に置き換わっているだけである点に注意。",
            "",
        ]

    lines += [
        "## シークエンス・オブ・リターンズ（リターン順序）の影響",
        "",
        "同一のリターン集合（累積リターンは完全に同じ）を並べ替えて取り崩した場合の最終残高:",
        "",
        report.markdown_table(
            ["リターンの順序", "最終残高（円）"],
            [
                ["悪い年が先（worst-first）", _fmt_money(result.worst_first_final)],
                ["良い年が先（best-first）", _fmt_money(result.best_first_final)],
            ],
        ),
        "",
        "- 積立期と違い、**取り崩し期は序盤の下落が致命的**になりうる（下落時に定額を"
        "引き出すと安値で多くの資産を取り崩すことになり、その後の回復に乗れない）。"
        "平均リターンが同じでも順序次第で結果が大きく変わる——これが"
        "シークエンス・オブ・リターンズ・リスク。",
        "- 対処の選択肢（一般論）: 現金クッションを持つ、下落時は引出額を減らす"
        "（可変引出）、定率方式にする、開始直前のリスク資産比率を下げる、など。"
        "どれもトレードオフがあり、正解は家計の状況に依存する。",
        "",
    ]
    if args.inflation != 0 and not result.inflation_linked and result.mode == "fixed_amount":
        lines += [
            f"- 注意: 定額引出はインフレ率 {args.inflation:g}% の下で実質価値が目減りする"
            f"（{args.years:g} 年後の {_fmt_money(args.monthly)} 円は現在の購買力で"
            f"約 {_fmt_money(args.monthly / deflator_last)} 円）。実質一定の引出を試すには"
            "`--inflation-linked` を付ける。",
            "",
        ]
    lines += _fan_chart_lines(
        result.months, result.percentiles, result.deterministic,
        img_stem=f"plan-decumulate-{today}",
        title=(
            f"Decumulation: {args.initial:,.0f} JPY, {args.years:g}y, "
            f"r={args.annual_return:g}%, vol={args.vol:g}%"
        ),
        no_charts=args.no_charts,
    )
    lines += _method_note_lines(args.paths, args.seed, cost_pct=args.cost)

    content = "\n".join(lines)
    path = report.save_report(content, f"plan-decumulate-{today}.md")
    print(f"枯渇確率 {result.depletion_prob * 100:.1f}% / "
          f"最終残高中央値 {_fmt_money(median_final)} 円（{mode_label}）")
    print(path)
    return 0


# --------------------------------------------------------------------------
# 引数パース
# --------------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser, *, default_vol: float) -> None:
    p.add_argument(
        "--return", dest="annual_return", type=float, required=True,
        help="想定年率リターン（%%表記。例: 5 = 年率5%%。ユーザーの仮定であり保証ではない）",
    )
    p.add_argument(
        "--vol", type=float, default=default_vol,
        help=f"想定年率ボラティリティ（%%表記、既定: {default_vol:g}）",
    )
    p.add_argument("--inflation", type=float, default=0.0, help="想定年率インフレ率（%%表記、既定: 0）")
    p.add_argument(
        "--cost", type=float, default=0.0,
        help="信託報酬等の年率コスト（%%表記、既定: 0）。想定リターンから控除して計算し、"
        "控除後リターンを前提表に明記する",
    )
    p.add_argument("--paths", type=int, default=2000, help="モンテカルロのパス数（既定: 2000）")
    p.add_argument("--seed", type=int, default=42, help="乱数シード（既定: 42。固定で再現可能）")
    p.add_argument("--no-charts", action="store_true", help="ファンチャート PNG を生成しない")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="資産形成プランニング（積立予測 / 目標逆算 / 進捗確認 / 取り崩し）。"
        "ネットワーク不要・価格データ不使用。金利・率は%%表記で指定する。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_proj = sub.add_parser("project", help="毎月定額積立の資産推移を試算する")
    p_proj.add_argument("--monthly", type=float, required=True, help="毎月の積立額（円）")
    p_proj.add_argument("--years", type=float, required=True, help="積立年数")
    p_proj.add_argument("--initial", type=float, default=0.0, help="初期資産（円、既定: 0）")
    p_proj.add_argument(
        "--nisa", action="store_true",
        help="NISA 非課税メリット（課税口座 20.315%%、2025年時点との比較）を併記する",
    )
    _add_common(p_proj, default_vol=15.0)
    p_proj.set_defaults(func=cmd_project)

    p_goal = sub.add_parser("goal", help="目標額から毎月の必要積立額を逆算する")
    p_goal.add_argument("--target", type=float, required=True, help="目標額（円）")
    p_goal.add_argument("--years", type=float, required=True, help="積立年数")
    p_goal.add_argument("--initial", type=float, default=0.0, help="初期資産（円、既定: 0）")
    _add_common(p_goal, default_vol=0.0)
    p_goal.set_defaults(func=cmd_goal)

    p_prog = sub.add_parser(
        "progress",
        help="現在の資産と積立ペースで目標に届くかを確認する（要求リターン逆算 + 到達確率）",
    )
    p_prog.add_argument("--target", type=float, required=True, help="目標額（円）")
    p_prog.add_argument("--years", type=float, required=True, help="目標までの残り年数")
    p_prog.add_argument("--current", type=float, required=True, help="現在の資産評価額（円）")
    p_prog.add_argument(
        "--monthly", type=float, required=True, help="毎月の積立額（円。積立なしなら 0）"
    )
    _add_common(p_prog, default_vol=15.0)
    p_prog.set_defaults(func=cmd_progress)

    p_dec = sub.add_parser("decumulate", help="資産の取り崩し（定額/定率）を試算する")
    p_dec.add_argument("--initial", type=float, required=True, help="取り崩し開始時の資産（円）")
    p_dec.add_argument("--years", type=float, required=True, help="取り崩し期間（年）")
    mode = p_dec.add_mutually_exclusive_group(required=True)
    mode.add_argument("--monthly", type=float, help="定額: 月次引出額（円）")
    mode.add_argument("--rate", type=float, help="定率: 年率引出率（%%表記。例: 4 = 年4%%）")
    p_dec.add_argument(
        "--inflation-linked", action="store_true",
        help="定額引出をインフレ連動で増額する（実質一定の引出。--inflation と併用）",
    )
    _add_common(p_dec, default_vol=15.0)
    p_dec.set_defaults(func=cmd_decumulate)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ValueError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

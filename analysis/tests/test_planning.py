"""stocklib.planning（資産形成プランニング）と asset_plan.py CLI のテスト。

決定論的複利の手計算一致、目標逆算の往復整合、要求リターン逆算（progress）、
コスト控除（net_of_cost_return）、モンテカルロのシード再現性、
枯渇確率の境界（リターン0・引出過大 → 枯渇100%）、NISA 税額、インフレ調整、
CLI 4サブコマンドのスモークを検証する。ネットワーク不使用。
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from stocklib import planning

REPO_ROOT = Path(__file__).resolve().parents[2]
TODAY = dt.date.today().isoformat()


# --- compound_projection: 決定論的複利 ---

def test_deterministic_zero_return_equals_contributions() -> None:
    """リターン0なら最終資産 = 積立総額（1万円 × 12ヶ月 = 12万円）。"""
    r = planning.compound_projection(10_000, 1, 0.0, 0.0, n_paths=10)
    assert r.deterministic[-1] == pytest.approx(120_000.0)
    assert r.total_contribution == pytest.approx(120_000.0)


def test_deterministic_matches_hand_calculation() -> None:
    """月利ちょうど1%（年率 1.01^12 - 1）で年金将来価値公式と一致する。

    FV = P × ((1+r)^n − 1) / r = 10000 × (1.01^12 − 1) / 0.01
    """
    annual = 1.01**12 - 1.0
    r = planning.compound_projection(10_000, 1, annual, 0.0, n_paths=10)
    expected = 10_000 * (1.01**12 - 1.0) / 0.01
    assert r.deterministic[-1] == pytest.approx(expected, rel=1e-12)


def test_deterministic_with_initial_capital() -> None:
    """初期資産は (1+r)^n で複利成長する（拠出0で確認）。"""
    annual = 1.01**12 - 1.0
    r = planning.compound_projection(0.0, 2, annual, 0.0, initial=1_000_000, n_paths=10)
    assert r.deterministic[-1] == pytest.approx(1_000_000 * 1.01**24, rel=1e-12)


def test_zero_vol_percentiles_equal_deterministic() -> None:
    """ボラ0なら全モンテカルロパスが決定論的複利と一致する。"""
    r = planning.compound_projection(30_000, 5, 0.05, 0.0, n_paths=50)
    for p in planning.PERCENTILES:
        np.testing.assert_allclose(r.percentiles[p], r.deterministic, rtol=1e-10)
    assert r.shortfall_prob == 0.0


def test_percentiles_are_ordered() -> None:
    """最終時点で P5 ≤ P25 ≤ P50 ≤ P75 ≤ P95。"""
    r = planning.compound_projection(50_000, 20, 0.05, 0.15, n_paths=500, seed=7)
    finals = [float(r.percentiles[p][-1]) for p in planning.PERCENTILES]
    assert finals == sorted(finals)
    assert finals[0] < finals[-1]  # ボラがあれば帯は開く


def test_volatility_drag_median_below_deterministic() -> None:
    """期待値一致のパラメータ化では中央値が決定論を下回る（ボラティリティ・ドラッグ）。"""
    r = planning.compound_projection(50_000, 20, 0.05, 0.20, n_paths=4000, seed=11)
    assert float(r.percentiles[50][-1]) < float(r.deterministic[-1])


def test_monte_carlo_reproducible_with_seed() -> None:
    """同一シードなら結果が完全一致し、異なるシードなら一致しない。"""
    a = planning.compound_projection(50_000, 10, 0.05, 0.15, n_paths=200, seed=42)
    b = planning.compound_projection(50_000, 10, 0.05, 0.15, n_paths=200, seed=42)
    c = planning.compound_projection(50_000, 10, 0.05, 0.15, n_paths=200, seed=43)
    np.testing.assert_array_equal(a.final_values, b.final_values)
    for p in planning.PERCENTILES:
        np.testing.assert_array_equal(a.percentiles[p], b.percentiles[p])
    assert not np.array_equal(a.final_values, c.final_values)


def test_inflation_deflator() -> None:
    """デフレーターは (1+π)^{t/12}。実質値 = 名目値 / デフレーター。"""
    r = planning.compound_projection(10_000, 10, 0.0, 0.0, inflation=0.02, n_paths=10)
    assert float(r.deflator[0]) == pytest.approx(1.0)
    assert float(r.deflator[-1]) == pytest.approx(1.02**10)
    real_final = r.real(r.deterministic)[-1]
    assert real_final == pytest.approx(1_200_000 / 1.02**10)


def test_projection_validation_errors() -> None:
    with pytest.raises(ValueError):
        planning.compound_projection(-1.0, 10, 0.05, 0.15)
    with pytest.raises(ValueError):
        planning.compound_projection(10_000, 0, 0.05, 0.15)
    with pytest.raises(ValueError):
        planning.compound_projection(10_000, 10, 0.05, -0.1)
    with pytest.raises(ValueError):
        planning.compound_projection(10_000, 10, -1.5, 0.15)


# --- required_monthly_saving: 逆算 ---

def test_required_monthly_zero_return() -> None:
    """リターン0なら 必要額 = 目標 ÷ 月数（1200万円 ÷ 240ヶ月 = 5万円）。"""
    p = planning.required_monthly_saving(12_000_000, 20, 0.0)
    assert p == pytest.approx(50_000.0)


def test_required_monthly_round_trip() -> None:
    """逆算した積立額で積み立てると決定論的最終額が目標に一致する（往復整合）。"""
    target, years, ret = 30_000_000.0, 25, 0.04
    p = planning.required_monthly_saving(target, years, ret, initial=1_000_000)
    r = planning.compound_projection(p, years, ret, 0.0, initial=1_000_000, n_paths=10)
    assert float(r.deterministic[-1]) == pytest.approx(target, rel=1e-10)


def test_required_monthly_zero_when_initial_sufficient() -> None:
    """初期資産の複利成長だけで目標に届くなら 0 を返す。"""
    p = planning.required_monthly_saving(1_000_000, 20, 0.05, initial=1_000_000)
    assert p == 0.0


def test_required_monthly_decreases_with_return() -> None:
    """想定リターンが高いほど必要積立額は小さい（単調性）。"""
    lo = planning.required_monthly_saving(30_000_000, 20, 0.02)
    hi = planning.required_monthly_saving(30_000_000, 20, 0.06)
    assert hi < lo


def test_required_monthly_validation() -> None:
    with pytest.raises(ValueError):
        planning.required_monthly_saving(0.0, 20, 0.05)
    with pytest.raises(ValueError):
        planning.required_monthly_saving(1_000_000, -1, 0.05)


# --- net_of_cost_return: コスト控除 ---

def test_net_of_cost_return_closed_form() -> None:
    """実効リターン = (1+R)(1-c) - 1（閉形式）。コスト0なら想定リターンそのまま。"""
    assert planning.net_of_cost_return(0.05, 0.01) == pytest.approx(1.05 * 0.99 - 1.0)
    assert planning.net_of_cost_return(0.05, 0.0) == pytest.approx(0.05)
    assert planning.net_of_cost_return(0.0, 0.005) == pytest.approx(-0.005)


def test_net_of_cost_return_matches_knowledge_quantification() -> None:
    """信託報酬差1.4%は30年で最終資産を約34.5%押し下げる。

    knowledge/market-structure/investment-trusts-and-asset-management.md の
    定量化 $1-(1-0.014)^{30} \\approx 34.5\\%$ と整合する（2020年代の低コスト
    インデックス0.1% vs アクティブ1.5%の比較例）。
    """
    net = planning.net_of_cost_return(0.05, 0.014)
    ratio_30y = (1.0 + net) ** 30 / 1.05**30
    assert ratio_30y == pytest.approx((1.0 - 0.014) ** 30, rel=1e-12)
    assert 1.0 - ratio_30y == pytest.approx(0.345, abs=0.005)


def test_net_of_cost_reduces_deterministic_final() -> None:
    """コスト控除後リターンでの決定論的最終資産は、控除前より小さい。"""
    gross = planning.compound_projection(50_000, 20, 0.05, 0.0, n_paths=10)
    net_r = planning.net_of_cost_return(0.05, 0.005)
    net = planning.compound_projection(50_000, 20, net_r, 0.0, n_paths=10)
    assert float(net.deterministic[-1]) < float(gross.deterministic[-1])


def test_net_of_cost_return_validation() -> None:
    with pytest.raises(ValueError):
        planning.net_of_cost_return(0.05, -0.01)
    with pytest.raises(ValueError):
        planning.net_of_cost_return(0.05, 1.0)
    with pytest.raises(ValueError):
        planning.net_of_cost_return(-1.5, 0.01)


# --- required_annual_return: 要求リターン逆算（progress） ---

def test_required_annual_return_round_trip() -> None:
    """既知リターンでの決定論的最終額を目標にすると、そのリターンが復元される。"""
    proj = planning.compound_projection(
        30_000, 15, 0.04, 0.0, initial=2_000_000, n_paths=10
    )
    target = float(proj.deterministic[-1])
    req = planning.required_annual_return(
        target, 15, current=2_000_000, monthly_amount=30_000
    )
    assert req == pytest.approx(0.04, abs=1e-9)


def test_required_annual_return_zero_when_principal_suffices() -> None:
    """目標 = 元本合計（現在資産 + 積立総額）なら要求リターンは0。

    100万円 + 1万円 × 120ヶ月 = 220万円。
    """
    req = planning.required_annual_return(
        2_200_000, 10, current=1_000_000, monthly_amount=10_000
    )
    assert req == pytest.approx(0.0, abs=1e-9)


def test_required_annual_return_negative_when_target_below_principal() -> None:
    """目標が元本合計を下回るなら要求リターンは負（運用リターン不要の領域）。"""
    req = planning.required_annual_return(
        1_500_000, 10, current=1_000_000, monthly_amount=10_000
    )
    assert req < 0.0


def test_required_annual_return_monotonic_in_target() -> None:
    """目標が高いほど要求リターンも高い（単調性）。"""
    lo = planning.required_annual_return(
        20_000_000, 20, current=5_000_000, monthly_amount=50_000
    )
    hi = planning.required_annual_return(
        30_000_000, 20, current=5_000_000, monthly_amount=50_000
    )
    assert hi > lo


def test_required_annual_return_lump_sum_closed_form() -> None:
    """積立0（一括のみ）なら閉形式 $R = (\\text{target}/V_0)^{1/y} - 1$ と一致する。"""
    req = planning.required_annual_return(
        2_000_000, 10, current=1_000_000, monthly_amount=0.0
    )
    assert req == pytest.approx(2.0 ** (1.0 / 10.0) - 1.0, abs=1e-9)


def test_required_annual_return_unreachable_raises() -> None:
    """現在資産0・期間1ヶ月では最終月の拠出額しか積めない → 到達不能はエラー。"""
    with pytest.raises(ValueError):
        planning.required_annual_return(
            1_000_000, 1 / 12, current=0.0, monthly_amount=10_000
        )


def test_required_annual_return_validation() -> None:
    with pytest.raises(ValueError):  # 目標が非正
        planning.required_annual_return(0.0, 10, current=1_000_000, monthly_amount=10_000)
    with pytest.raises(ValueError):  # 期間が非正
        planning.required_annual_return(1_000_000, 0, current=1_000_000, monthly_amount=10_000)
    with pytest.raises(ValueError):  # 現在資産・積立の両方が0
        planning.required_annual_return(1_000_000, 10, current=0.0, monthly_amount=0.0)
    with pytest.raises(ValueError):  # 現在資産が負
        planning.required_annual_return(1_000_000, 10, current=-1.0, monthly_amount=10_000)


# --- decumulation_simulation: 取り崩し ---

def test_decumulation_depletes_with_certainty_when_overdrawn() -> None:
    """リターン0・ボラ0で 100万円から月10万円 → 10ヶ月で枯渇（枯渇確率100%）。"""
    r = planning.decumulation_simulation(
        1_000_000, 2, 0.0, 0.0, monthly_withdrawal=100_000, n_paths=50
    )
    assert r.depletion_prob == pytest.approx(1.0)
    assert float(r.deterministic[10]) == pytest.approx(0.0)
    assert float(r.deterministic[9]) > 0.0
    assert r.depletion_month_median == pytest.approx(10.0)


def test_decumulation_overdrawn_with_vol_still_near_certain() -> None:
    """引出が過大（月2%相当・リターン0）ならボラがあっても枯渇確率はほぼ100%。"""
    r = planning.decumulation_simulation(
        10_000_000, 15, 0.0, 0.15, monthly_withdrawal=200_000, n_paths=500, seed=3
    )
    assert r.depletion_prob > 0.95


def test_decumulation_no_depletion_when_withdrawal_small() -> None:
    """引出が十分小さければ枯渇確率は0近傍。"""
    r = planning.decumulation_simulation(
        100_000_000, 10, 0.03, 0.10, monthly_withdrawal=100_000, n_paths=300, seed=5
    )
    assert r.depletion_prob == pytest.approx(0.0, abs=0.01)
    assert r.depletion_month_median is None or r.depletion_month_median > 0


def test_decumulation_fixed_rate_never_depletes() -> None:
    """定率引出は数学的に枯渇しない（残高は常に正）。"""
    r = planning.decumulation_simulation(
        30_000_000, 30, 0.03, 0.15, annual_withdrawal_rate=0.04, n_paths=300, seed=9
    )
    assert r.depletion_prob == 0.0
    assert (r.final_values > 0).all()
    assert r.withdrawal_median is not None
    assert (r.withdrawal_median > 0).all()


def test_decumulation_absorbing_zero() -> None:
    """一度枯渇した残高は0のまま（吸収状態）→ 月次枯渇確率は単調非減少。"""
    r = planning.decumulation_simulation(
        5_000_000, 10, 0.0, 0.20, monthly_withdrawal=100_000, n_paths=300, seed=1
    )
    diffs = np.diff(r.depletion_prob_by_month)
    assert (diffs >= -1e-12).all()


def test_sequence_of_returns_worst_first_is_worse() -> None:
    """同一リターン集合でも「悪い順」の最終残高 ≤「良い順」（取り崩し中は順序が効く）。"""
    r = planning.decumulation_simulation(
        30_000_000, 25, 0.04, 0.15, monthly_withdrawal=120_000, n_paths=100, seed=42
    )
    assert r.worst_first_final <= r.best_first_final
    assert r.worst_first_final < r.best_first_final  # ボラがあれば厳密に差が出る


def test_decumulation_reproducible_with_seed() -> None:
    a = planning.decumulation_simulation(
        30_000_000, 20, 0.03, 0.12, monthly_withdrawal=120_000, n_paths=200, seed=42
    )
    b = planning.decumulation_simulation(
        30_000_000, 20, 0.03, 0.12, monthly_withdrawal=120_000, n_paths=200, seed=42
    )
    np.testing.assert_array_equal(a.final_values, b.final_values)
    assert a.depletion_prob == b.depletion_prob


def test_decumulation_inflation_linked_withdrawal_depletes_faster() -> None:
    """インフレ連動増額は定額（名目一定）より枯渇しやすい。"""
    kwargs = dict(monthly_withdrawal=150_000, n_paths=400, seed=8)
    flat = planning.decumulation_simulation(30_000_000, 30, 0.02, 0.10, **kwargs)
    linked = planning.decumulation_simulation(
        30_000_000, 30, 0.02, 0.10, inflation=0.02, inflation_linked=True, **kwargs
    )
    assert linked.depletion_prob >= flat.depletion_prob


def test_decumulation_validation_errors() -> None:
    with pytest.raises(ValueError):  # 定額・定率の両方指定
        planning.decumulation_simulation(
            1_000_000, 10, 0.03, 0.1,
            monthly_withdrawal=50_000, annual_withdrawal_rate=0.04,
        )
    with pytest.raises(ValueError):  # どちらも未指定
        planning.decumulation_simulation(1_000_000, 10, 0.03, 0.1)
    with pytest.raises(ValueError):  # 初期資産が非正
        planning.decumulation_simulation(0.0, 10, 0.03, 0.1, monthly_withdrawal=50_000)
    with pytest.raises(ValueError):  # 引出率が過大（月次で100%超）
        planning.decumulation_simulation(
            1_000_000, 10, 0.03, 0.1, annual_withdrawal_rate=13.0
        )


# --- nisa_tax_benefit ---

def test_nisa_tax_benefit_amount() -> None:
    """運用益100万円 → 課税口座の税額 = 非課税メリット = 203,150円（20.315%、2025年時点）。"""
    b = planning.nisa_tax_benefit(1_000_000)
    assert b.tax_in_taxable == pytest.approx(203_150.0)
    assert b.benefit == pytest.approx(203_150.0)
    assert b.after_tax_gain_taxable == pytest.approx(796_850.0)
    assert b.after_tax_gain_nisa == pytest.approx(1_000_000.0)


def test_nisa_tax_benefit_zero_on_loss() -> None:
    """損失時は課税口座の税額0 → 非課税メリットも0。"""
    b = planning.nisa_tax_benefit(-500_000)
    assert b.tax_in_taxable == 0.0
    assert b.benefit == 0.0
    assert b.after_tax_gain_taxable == pytest.approx(-500_000.0)


def test_nisa_tax_rate_validation() -> None:
    with pytest.raises(ValueError):
        planning.nisa_tax_benefit(1_000_000, tax_rate=1.5)


# --- real_value / monthly_rate ---

def test_real_value() -> None:
    assert planning.real_value(1_000_000, 10, 0.02) == pytest.approx(1_000_000 / 1.02**10)
    assert planning.real_value(1_000_000, 10, 0.0) == pytest.approx(1_000_000.0)


def test_monthly_rate_geometric() -> None:
    """(1 + 月利)^12 = 1 + 年率（幾何換算の整合）。"""
    r = planning.monthly_rate(0.05)
    assert (1.0 + r) ** 12 == pytest.approx(1.05)


# --- CLI スモーク（subprocess、ネットワーク不使用） ---

def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "analysis/asset_plan.py", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_cli_project() -> None:
    proc = _run(
        "project", "--monthly", "50000", "--years", "20",
        "--return", "5", "--vol", "15", "--inflation", "1", "--nisa",
        "--paths", "500",
    )
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    assert report_path.name == f"plan-project-{TODAY}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "免責事項" in content
    assert "保証するものではありません" in content  # ASSUMPTION_NOTE
    assert "NISA 非課税メリット" in content
    assert "実質" in content  # インフレ調整の併記


def test_cli_goal() -> None:
    proc = _run(
        "goal", "--target", "30000000", "--years", "25",
        "--return", "4", "--vol", "15", "--paths", "300", "--no-charts",
    )
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    assert report_path.name == f"plan-goal-{TODAY}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "必要積立額" in content
    assert "達成確率" in content
    assert "感応度" in content
    assert "保証するものではありません" in content


def test_cli_progress() -> None:
    proc = _run(
        "progress", "--target", "30000000", "--years", "15",
        "--current", "8000000", "--monthly", "70000",
        "--return", "4", "--vol", "15", "--paths", "300", "--no-charts",
    )
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    assert report_path.name == f"plan-progress-{TODAY}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "要求年率リターン" in content
    assert "到達する確率" in content
    assert "現在地" in content
    assert "保証するものではありません" in content  # ASSUMPTION_NOTE
    assert "免責事項" in content
    # knowledge 参照が壊れていない（品質ゲート指摘の回帰テスト）
    assert "household-risk-capacity-and-allocation.md" in content
    assert "household-risk-capacity-and-asset-allocation.md" not in content


def test_cli_progress_high_required_return_has_neutral_note() -> None:
    """要求リターンが歴史的な株式リターン参考値を大きく超える場合の中立的注記。"""
    proc = _run(
        "progress", "--target", "100000000", "--years", "10",
        "--current", "1000000", "--monthly", "30000",
        "--return", "5", "--vol", "15", "--paths", "200", "--no-charts",
    )
    assert proc.returncode == 0, proc.stderr
    content = Path(proc.stdout.strip().splitlines()[-1]).read_text(encoding="utf-8")
    assert "見直しの検討材料" in content
    assert "long-term-wealth-building.md" in content
    # 達成可否の断定をしない
    assert "達成できない" not in content
    assert "達成できる" not in content


def test_cli_project_with_cost() -> None:
    """--cost 指定時は前提表にコストと控除後リターンが明記される。"""
    proc = _run(
        "project", "--monthly", "50000", "--years", "10",
        "--return", "5", "--vol", "10", "--cost", "0.5",
        "--paths", "200", "--no-charts",
    )
    assert proc.returncode == 0, proc.stderr
    content = Path(proc.stdout.strip().splitlines()[-1]).read_text(encoding="utf-8")
    assert "年率コスト（信託報酬等、--cost）" in content
    assert "コスト控除後リターン（計算に使用）" in content
    assert "4.475%" in content  # (1.05)(0.995) - 1 = 4.475%
    assert "控除済み" in content  # 手法ノートのコスト文言


def test_cli_progress_with_cost_shows_gross_requirement() -> None:
    """--cost 指定時、progress はコスト控除前の要求グロスリターンも併記する。"""
    proc = _run(
        "progress", "--target", "30000000", "--years", "15",
        "--current", "8000000", "--monthly", "70000",
        "--return", "4", "--cost", "0.5", "--vol", "15",
        "--paths", "200", "--no-charts",
    )
    assert proc.returncode == 0, proc.stderr
    content = Path(proc.stdout.strip().splitlines()[-1]).read_text(encoding="utf-8")
    assert "控除前では" in content
    assert "グロスリターンが必要" in content


def test_cli_decumulate_fixed_amount() -> None:
    proc = _run(
        "decumulate", "--initial", "30000000", "--years", "30",
        "--return", "3", "--vol", "12", "--monthly", "120000",
        "--paths", "300", "--no-charts",
    )
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    assert report_path.name == f"plan-decumulate-{TODAY}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "枯渇確率" in content
    assert "シークエンス・オブ・リターンズ" in content


def test_cli_decumulate_fixed_rate() -> None:
    proc = _run(
        "decumulate", "--initial", "30000000", "--years", "30",
        "--return", "3", "--vol", "12", "--rate", "4",
        "--paths", "300", "--no-charts",
    )
    assert proc.returncode == 0, proc.stderr
    content = Path(proc.stdout.strip().splitlines()[-1]).read_text(encoding="utf-8")
    assert "定率" in content
    assert "受取額" in content


def test_cli_decumulate_rejects_both_modes() -> None:
    """--monthly と --rate の同時指定は argparse が拒否する（exit 2）。"""
    proc = _run(
        "decumulate", "--initial", "30000000", "--years", "30",
        "--return", "3", "--monthly", "120000", "--rate", "4",
    )
    assert proc.returncode == 2


def test_cli_invalid_years_exits_nonzero() -> None:
    proc = _run("project", "--monthly", "50000", "--years", "0", "--return", "5")
    assert proc.returncode == 1
    assert "エラー" in proc.stderr

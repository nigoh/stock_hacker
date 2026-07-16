"""signals モジュールの境界値テストと daily_brief CLI のスモークテスト。

人工的な系列（決定論的）で各シグナルの閾値前後を検証する。ネットワーク不使用。
RSI の境界値は、Wilder 平滑化の定常状態解を使って任意の RSI 値を持つ系列を
逆算して構成する（:func:`_series_with_rsi` 参照）。
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stocklib import indicators, signals
from stocklib.signals import Signal, detect_signals

REPO_ROOT = Path(__file__).resolve().parents[2]
TODAY = dt.date.today().isoformat()


def _df(close: list[float] | pd.Series, volume: list[float] | None = None) -> pd.DataFrame:
    """終値（と任意の出来高）から検出用 DataFrame を組み立てる。"""
    close = pd.Series(close, dtype=float).reset_index(drop=True)
    index = pd.date_range(end=dt.date.today(), periods=len(close), freq="B")
    df = pd.DataFrame({"Close": close.to_numpy()}, index=index)
    df["Volume"] = volume if volume is not None else 1_000_000.0
    return df


def _of_kind(sigs: list[Signal], kind: str) -> list[Signal]:
    return [s for s in sigs if s.kind == kind]


# ---------------------------------------------------------------- RSI 境界値


def _series_with_rsi(target: float, days: int = 600) -> pd.Series:
    """最終日の RSI(14) がちょうど ``target`` になる価格系列を構成する。

    日次変化を +a（偶数日）/ −1（奇数日）の交互にすると、Wilder 平滑化
    （alpha = 1/14, adjust=False）の定常状態では、損失日の終了時点で
    avg_gain = (13/14)a/(2-1/14)、avg_loss = 1/(2-1/14) となり

        RSI = 100 * 13a / (13a + 14)

    に収束する。これを target について解くと a = 14*target / (13*(100-target))。
    days=600 で初期条件の残差は (13/14)^600 ≈ 5e-20 と無視できる。
    最終日が損失日になるよう days は偶数にすること。
    """
    assert days % 2 == 0, "最終日を損失日にするため days は偶数"
    a = 14.0 * target / (13.0 * (100.0 - target))
    diffs = np.where(np.arange(days) % 2 == 0, a, -1.0)
    prices = 1000.0 + np.concatenate([[0.0], np.cumsum(diffs)])
    return pd.Series(prices)


@pytest.mark.parametrize("target", [29.9, 30.1, 69.9, 70.1])
def test_series_with_rsi_construction(target: float) -> None:
    close = _series_with_rsi(target)
    assert float(indicators.rsi(close, 14).iloc[-1]) == pytest.approx(target, abs=1e-6)


def test_rsi_oversold_boundary() -> None:
    # RSI = 29.9 → 30以下で検出（bullish）
    sigs = _of_kind(detect_signals(_df(_series_with_rsi(29.9))), "rsi")
    assert len(sigs) == 1
    assert sigs[0].direction == "bullish"
    assert "売られすぎ" in sigs[0].detail
    # RSI = 30.1 → 非検出
    assert _of_kind(detect_signals(_df(_series_with_rsi(30.1))), "rsi") == []


def test_rsi_overbought_boundary() -> None:
    # RSI = 70.1 → 70以上で検出（bearish）
    sigs = _of_kind(detect_signals(_df(_series_with_rsi(70.1))), "rsi")
    assert len(sigs) == 1
    assert sigs[0].direction == "bearish"
    # RSI = 69.9 → 非検出
    assert _of_kind(detect_signals(_df(_series_with_rsi(69.9))), "rsi") == []


# ------------------------------------------------------------ 移動平均クロス


def test_golden_cross_within_5_days_detected() -> None:
    # 100で150日 → 130にジャンプ: ジャンプ当日に SMA25 が SMA75 を上抜ける
    # ジャンプ後5日分（クロスは4営業日前）→ 検出
    close = [100.0] * 150 + [130.0] * 5
    sigs = _of_kind(detect_signals(_df(close)), "ma_cross")
    assert len(sigs) == 1
    assert sigs[0].direction == "bullish"
    assert "ゴールデンクロス" in sigs[0].detail
    assert "4営業日前" in sigs[0].detail


def test_golden_cross_older_than_5_days_not_detected() -> None:
    # クロスは5営業日前（6行前）→ 直近5営業日の窓から外れて非検出
    close = [100.0] * 150 + [130.0] * 6
    assert _of_kind(detect_signals(_df(close)), "ma_cross") == []


def test_dead_cross_detected() -> None:
    close = [100.0] * 150 + [70.0] * 3
    sigs = _of_kind(detect_signals(_df(close)), "ma_cross")
    assert len(sigs) == 1
    assert sigs[0].direction == "bearish"
    assert "デッドクロス" in sigs[0].detail


def test_ma_cross_same_day_labeled_today() -> None:
    close = [100.0] * 150 + [130.0]
    sigs = _of_kind(detect_signals(_df(close)), "ma_cross")
    assert len(sigs) == 1 and "当日" in sigs[0].detail


# ---------------------------------------------------------------- 出来高急増


def test_volume_surge_boundary() -> None:
    close = [100.0] * 40
    # 平均は直近日を除く20日 = 1,000,000。2倍ちょうど（2,000,000）は「超」でないため非検出
    vol_exact = [1_000_000.0] * 39 + [2_000_000.0]
    assert _of_kind(detect_signals(_df(close, vol_exact)), "volume") == []
    # 2倍を1株でも超えれば検出
    vol_over = [1_000_000.0] * 39 + [2_000_001.0]
    sigs = _of_kind(detect_signals(_df(close, vol_over)), "volume")
    assert len(sigs) == 1
    assert sigs[0].direction == "neutral"
    assert "出来高急増" in sigs[0].detail


def test_volume_signal_skipped_when_insufficient_data() -> None:
    # 20日平均が計算できない長さでは出来高シグナルを出さない
    close = [100.0] * 15
    vol = [1_000_000.0] * 14 + [9_000_000.0]
    assert _of_kind(detect_signals(_df(close, vol)), "volume") == []


# ------------------------------------------------------------ 52週高値/安値


def test_week52_high_boundary() -> None:
    # 期間最高値 200 に対し終値 194 → (200-194)/200 = 3.0%（3%以内、境界含む）→ 検出
    base = [100.0] * 100 + [200.0] + [100.0] * 100
    sigs = _of_kind(detect_signals(_df(base + [194.0])), "week52")
    assert len(sigs) == 1
    assert sigs[0].direction == "bullish"
    assert "52週高値圏" in sigs[0].detail
    # 193.9 → 3.05% → 非検出
    assert _of_kind(detect_signals(_df(base + [193.9])), "week52") == []


def test_week52_low_boundary() -> None:
    # 期間最安値 50 に対し終値 51.5 → (51.5-50)/50 = 3.0% → 検出
    base = [100.0] * 100 + [50.0] + [100.0] * 100
    sigs = _of_kind(detect_signals(_df(base + [51.5])), "week52")
    assert len(sigs) == 1
    assert sigs[0].direction == "bearish"
    assert "52週安値圏" in sigs[0].detail
    # 51.6 → 3.2% → 非検出
    assert _of_kind(detect_signals(_df(base + [51.6])), "week52") == []


def test_week52_uses_trailing_252_days_only() -> None:
    # 高値 200 は 252 日窓の外 → 窓内の高値は 110 で、終値 110 が高値圏と判定される
    close = [200.0] + [100.0] * 260 + [110.0] * 40
    sigs = _of_kind(detect_signals(_df(close)), "week52")
    assert len(sigs) == 1
    assert sigs[0].direction == "bullish"
    assert "110" in sigs[0].detail and "200" not in sigs[0].detail


# ------------------------------------------------------------------ 急変動


def test_price_move_boundary() -> None:
    # +3.1% → 検出（bullish）
    sigs = _of_kind(detect_signals(_df([100.0] * 30 + [103.1])), "price_move")
    assert len(sigs) == 1 and sigs[0].direction == "bullish"
    # -3.1% → 検出（bearish）
    sigs = _of_kind(detect_signals(_df([100.0] * 30 + [96.9])), "price_move")
    assert len(sigs) == 1 and sigs[0].direction == "bearish"
    # ±2.9% → 非検出
    assert _of_kind(detect_signals(_df([100.0] * 30 + [102.9])), "price_move") == []
    assert _of_kind(detect_signals(_df([100.0] * 30 + [97.1])), "price_move") == []


# -------------------------------------------------------------------- 全体


def test_quiet_flat_series_has_no_signals() -> None:
    # 完全に平坦な系列（RSI=50・クロスなし・出来高平坦・無変動）はシグナルゼロ
    assert detect_signals(_df([100.0] * 300)) == []


def test_detect_signals_requires_close_column() -> None:
    with pytest.raises(ValueError):
        detect_signals(pd.DataFrame({"Volume": [1.0, 2.0]}))


def test_detect_signals_short_series_returns_empty() -> None:
    assert detect_signals(_df([100.0])) == []


def test_detect_signals_works_without_volume_column() -> None:
    df = _df([100.0] * 30 + [104.0]).drop(columns=["Volume"])
    sigs = detect_signals(df)
    assert _of_kind(sigs, "price_move") != []
    assert _of_kind(sigs, "volume") == []


def test_signal_dataclass_fields() -> None:
    sig = Signal(kind="rsi", direction="bullish", detail="テスト")
    assert (sig.kind, sig.direction, sig.detail) == ("rsi", "bullish", "テスト")


# ------------------------------------------------------- daily_brief CLI


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_daily_brief_cli_with_watchlist() -> None:
    proc = _run(
        "analysis/daily_brief.py",
        "--watchlist", "analysis/templates/watchlist-example.csv",
        "--synthetic",
    )
    assert proc.returncode == 0, proc.stderr
    assert "市況" in proc.stdout
    assert "ウォッチリスト" in proc.stdout
    report_path = REPO_ROOT / "reports" / f"brief-{TODAY}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "免責事項" in content
    assert "合成データ" in content
    assert "7203" in content


def test_daily_brief_cli_without_watchlist_continues_market_only(tmp_path: Path) -> None:
    missing = tmp_path / "no-watchlist.csv"
    proc = _run("analysis/daily_brief.py", "--watchlist", str(missing), "--synthetic")
    assert proc.returncode == 0, proc.stderr
    assert "watchlist-example.csv" in proc.stdout  # テンプレートの案内
    assert "市況" in proc.stdout


def test_signals_constants_match_documented_thresholds() -> None:
    assert signals.RSI_OVERSOLD == 30.0
    assert signals.RSI_OVERBOUGHT == 70.0
    assert (signals.FAST_SMA_WINDOW, signals.SLOW_SMA_WINDOW) == (25, 75)
    assert signals.CROSS_LOOKBACK == 5
    assert signals.VOLUME_SURGE_RATIO == 2.0
    assert signals.WEEK52_PROXIMITY == 0.03
    assert signals.PRICE_MOVE_THRESHOLD == 0.03

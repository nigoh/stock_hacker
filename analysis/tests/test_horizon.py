"""--horizon（時間軸フレーム）のテスト。

合成データ（--synthetic）でネットワーク不使用。検証内容:

1. 省略時（horizon=None）の既定動作が不変であること（視点節が出ない）
2. 各 horizon で期待される節・指標が含まれ、他の horizon の節が含まれないこと
3. 数値の整合（ATR倍数ストップ水準・モメンタムリターン・52週高値距離・年率ボラ）
4. CLI スモーク（期間デフォルト short=6mo/mid=2y/long=5y、--period 上書き、
   出力ファイル名 analyze-<code>-<horizon>-<日付>.md、免責文）
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import pytest

import analyze_stock
from stocklib import indicators, metrics, report
from stocklib.data import fetch_info, fetch_prices

REPO_ROOT = Path(__file__).resolve().parents[2]
TODAY = dt.date.today().isoformat()

SHORT_HEADING = "## 短期の視点（〜数週間）"
MID_HEADING = "## 中期の視点（数ヶ月〜1年）"
LONG_HEADING = "## 長期の視点（数年〜）"
ALL_HEADINGS = (SHORT_HEADING, MID_HEADING, LONG_HEADING)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _build(horizon: str | None, period: str) -> str:
    return analyze_stock.build_report(
        "7203", period, "^N225", True, img_stem=None, horizon=horizon
    )


def _section(content: str, heading: str) -> str:
    """レポートから heading で始まる節（次の ## まで）を切り出す。"""
    assert heading in content
    return content.split(heading, 1)[1].split("\n## ", 1)[0]


# ---------------------------------------------------------------------------
# デフォルト動作の不変性
# ---------------------------------------------------------------------------


def test_default_report_has_no_horizon_sections() -> None:
    """horizon 省略時は従来の全部入りのままで、視点節・時間軸表記が一切出ない。"""
    content = _build(None, "1y")
    for heading in ALL_HEADINGS:
        assert heading not in content
    assert "時間軸フレーム" not in content
    assert "この時間軸で見るべきもの" not in content
    # 従来の節は維持される
    assert "## 価格サマリー" in content
    assert "## テクニカル指標" in content
    assert "## リスク・リターン指標" in content


def test_build_report_rejects_unknown_horizon() -> None:
    with pytest.raises(ValueError):
        _build("weekly", "1y")


# ---------------------------------------------------------------------------
# short: 5/25日線・RSI・ATR・出来高・直近高安値・ストップ目安
# ---------------------------------------------------------------------------


def test_short_section_contents() -> None:
    content = _build("short", "6mo")
    assert SHORT_HEADING in content
    assert MID_HEADING not in content
    assert LONG_HEADING not in content

    section = _section(content, SHORT_HEADING)
    assert "この時間軸で見るべきもの" in section
    assert "見るべきでないもの" in section
    assert "SMA(5)" in section
    assert "SMA(25)" in section
    assert "RSI(14)" in section
    assert "ATR(14)" in section
    assert "出来高" in section
    assert "直近20日高値" in section
    assert "直近20日安値" in section
    # ストップ目安（ATR倍数）のテーブルと注意書き
    assert "ストップ（撤退水準）設定の目安（ATR倍数）" in section
    for k in ("1×ATR", "2×ATR", "3×ATR"):
        assert k in section
    assert "推奨するものではない" in section
    # 全部入りの節も維持される（horizon は「強調」の追加）
    assert "## テクニカル指標" in content
    assert "## リスク・リターン指標" in content


def test_short_stop_levels_match_atr_multiples() -> None:
    """ストップ水準が 終値 − k×ATR(14) と一致する（合成データで決定論的）。"""
    df = fetch_prices("7203", period="6mo", synthetic=True)["7203"]
    last = float(df["Close"].iloc[-1])
    atr = float(indicators.atr(df, 14).iloc[-1])
    section = _section(_build("short", "6mo"), SHORT_HEADING)
    for k in (1.0, 2.0, 3.0):
        assert report.fmt_num(last - k * atr) in section
    # ATR の終値比も表示される
    assert report.fmt_pct(atr / last) in section


# ---------------------------------------------------------------------------
# mid: 25/75/200日線・3/6/12ヶ月モメンタム・52週高値距離・決算注意
# ---------------------------------------------------------------------------


def test_mid_section_contents() -> None:
    content = _build("mid", "2y")
    assert MID_HEADING in content
    assert SHORT_HEADING not in content
    assert LONG_HEADING not in content

    section = _section(content, MID_HEADING)
    assert "この時間軸で見るべきもの" in section
    assert "見るべきでないもの" in section
    assert "SMA(25)" in section
    assert "SMA(75)" in section
    assert "SMA(200)" in section
    assert "移動平均の並び" in section
    assert "3ヶ月リターン（63営業日）" in section
    assert "6ヶ月リターン（126営業日）" in section
    assert "12ヶ月リターン（252営業日）" in section
    assert "52週高値からの距離" in section
    assert "決算スケジュールへの注意" in section
    assert "ギャップリスク" in section


def test_mid_momentum_and_week52_values() -> None:
    """モメンタムリターンと52週高値距離が定義どおりの値で表示される。"""
    close = fetch_prices("7203", period="2y", synthetic=True)["7203"]["Close"]
    last = float(close.iloc[-1])
    section = _section(_build("mid", "2y"), MID_HEADING)
    for days in (63, 126, 252):
        expected = report.fmt_pct(float(close.iloc[-1] / close.iloc[-1 - days] - 1.0))
        assert expected in section, f"{days}営業日リターン"
    high52 = float(close.iloc[-252:].max())
    assert report.fmt_pct(last / high52 - 1.0) in section


# ---------------------------------------------------------------------------
# long: 年率統計・最大DD・配当利回り・PBR/PER・積立適性
# ---------------------------------------------------------------------------


def test_long_section_contents() -> None:
    content = _build("long", "5y")
    assert LONG_HEADING in content
    assert SHORT_HEADING not in content
    assert MID_HEADING not in content

    section = _section(content, LONG_HEADING)
    assert "この時間軸で見るべきもの" in section
    # 例: 長期投資で RSI を見る意味は薄い
    assert "RSI" in section and "意味は薄い" in section
    assert "年率リターン（幾何平均）" in section
    assert "年率ボラティリティ（√252換算）" in section
    assert "最大ドローダウン" in section
    assert "配当利回り" in section
    assert "PER（実績）" in section
    assert "PBR" in section
    # PBR/PER の長期文脈と積立適性（根拠: 調和平均 ≤ 算術平均）の明記
    assert "PBR/PERの長期文脈" in section
    assert "積立（時間分散）適性の観点" in section
    assert "調和平均" in section
    assert "期待リターンが高くなることを意味しない" in section


def test_long_values_match_metrics_and_info() -> None:
    """年率ボラ・最大DD・配当利回りが stocklib の計算値・info と一致して表示される。"""
    close = fetch_prices("7203", period="5y", synthetic=True)["7203"]["Close"]
    rets = metrics.daily_returns(close)
    section = _section(_build("long", "5y"), LONG_HEADING)
    assert report.fmt_pct(metrics.ann_vol(rets)) in section
    assert report.fmt_pct(metrics.max_drawdown(close)) in section
    info = fetch_info("7203", synthetic=True)
    assert report.fmt_pct(float(info["配当利回り"])) in section


# ---------------------------------------------------------------------------
# CLI スモーク（期間デフォルト・ファイル名・上書き・免責）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("horizon", "default_period", "heading"),
    [
        ("short", "6mo", SHORT_HEADING),
        ("mid", "2y", MID_HEADING),
        ("long", "5y", LONG_HEADING),
    ],
)
def test_cli_horizon_smoke(horizon: str, default_period: str, heading: str) -> None:
    proc = _run(
        "analysis/analyze_stock.py", "7203",
        "--horizon", horizon, "--synthetic", "--no-charts",
    )
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    assert report_path.name == f"analyze-7203-{horizon}-{TODAY}.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert f"- 期間: {default_period}" in content
    assert "時間軸フレーム" in content
    assert heading in content
    assert "免責事項" in content


def test_cli_period_overrides_horizon_default() -> None:
    proc = _run(
        "analysis/analyze_stock.py", "7203",
        "--horizon", "short", "--period", "1y", "--synthetic", "--no-charts",
    )
    assert proc.returncode == 0, proc.stderr
    content = Path(proc.stdout.strip().splitlines()[-1]).read_text(encoding="utf-8")
    assert "- 期間: 1y" in content
    assert SHORT_HEADING in content


def test_cli_default_filename_and_period_unchanged() -> None:
    """horizon 省略時はファイル名・期間既定（2y）とも従来どおり。"""
    proc = _run("analysis/analyze_stock.py", "7203", "--synthetic", "--no-charts")
    assert proc.returncode == 0, proc.stderr
    report_path = Path(proc.stdout.strip().splitlines()[-1])
    assert report_path.name == f"analyze-7203-{TODAY}.md"
    content = report_path.read_text(encoding="utf-8")
    assert "- 期間: 2y" in content
    for heading in ALL_HEADINGS:
        assert heading not in content


def test_cli_rejects_unknown_horizon() -> None:
    proc = _run(
        "analysis/analyze_stock.py", "7203",
        "--horizon", "weekly", "--synthetic", "--no-charts",
    )
    assert proc.returncode == 2  # argparse の choices エラー

"""daily_brief の自動実行対応のテスト。

対象: stdout 最終行の
``RESULT signals=<N> watch=<取得成功数>/<総数> data=<real|synthetic|unavailable>``、
実データ全滅時の exit code 2（fetch_prices をモック）、watch フィールドによる
「変化なし / ウォッチリスト取得失敗 / watchlist 未設定」の区別、--max-alerts による
シグナル詳細表示の絞り込み。
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import daily_brief
from stocklib import signals
from stocklib.data import DataFetchError

REPO_ROOT = Path(__file__).resolve().parents[2]
WATCHLIST_TEMPLATE = "analysis/templates/watchlist-example.csv"
RESULT_RE = re.compile(
    r"^RESULT signals=(\d+) watch=(\d+)/(\d+) data=(real|synthetic|unavailable)$"
)


# ------------------------------------------------------------------ helpers


def _make_signal_rich_prices(n: int = 300) -> pd.DataFrame:
    """複数シグナル（急変動・出来高急増・52週高値圏など）が確実に立つ系列。"""
    idx = pd.bdate_range(end=dt.date.today(), periods=n)
    close = 100.0 + 0.05 * np.arange(n, dtype=float)  # 緩やかな上昇
    close[-1] = close[-2] * 1.05  # 前日比 +5% の急変動 → 52週高値圏でもある
    volume = np.full(n, 1000.0)
    volume[-1] = 10000.0  # 20日平均の約10倍の出来高急増
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": volume},
        index=idx,
    )


def _make_plain_prices(n: int = 300) -> pd.DataFrame:
    """市況セクション用の無難な系列（シグナル判定には使われない）。"""
    idx = pd.bdate_range(end=dt.date.today(), periods=n)
    close = 100.0 + 0.05 * np.arange(n, dtype=float)
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": np.full(n, 1000.0)},
        index=idx,
    )


def _fake_fetch(df_map: dict[str, pd.DataFrame]):
    """fetch_prices 互換のモック。df_map に無いコードは DataFetchError を投げる。"""

    def fetch(
        codes: str | list[str],
        period: str = "1y",
        interval: str = "1d",
        *,
        synthetic: bool = False,
        use_cache: bool = True,
    ) -> dict[str, pd.DataFrame]:
        code_list = [codes] if isinstance(codes, str) else list(codes)
        out: dict[str, pd.DataFrame] = {}
        for code in code_list:
            if code not in df_map:
                raise DataFetchError(f"{code}: テスト用に取得失敗を注入")
            out[code] = df_map[code]
        return out

    return fetch


def _write_watchlist(tmp_path: Path, codes: list[str]) -> Path:
    path = tmp_path / "watchlist.csv"
    path.write_text("code,note\n" + "".join(f"{c},\n" for c in codes), encoding="utf-8")
    return path


def _patch_save_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """reports/ を汚さないよう保存先を tmp に差し替える。"""

    def fake_save(content: str, filename: str) -> Path:
        out = tmp_path / filename
        out.write_text(content, encoding="utf-8")
        return out

    monkeypatch.setattr(daily_brief.report, "save_report", fake_save)


def _last_result_line(stdout: str) -> re.Match[str]:
    last = stdout.strip().splitlines()[-1]
    match = RESULT_RE.match(last)
    assert match is not None, f"stdout 最終行が RESULT 形式ではない: {last!r}"
    return match


# ------------------------------------------------------- RESULT 行（synthetic）


def test_result_line_is_last_stdout_line_with_synthetic() -> None:
    proc = subprocess.run(
        [sys.executable, "analysis/daily_brief.py",
         "--watchlist", WATCHLIST_TEMPLATE, "--synthetic"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    match = _last_result_line(proc.stdout)
    assert match.group(4) == "synthetic"
    assert int(match.group(1)) >= 0
    # 合成データは取得失敗しないため watch は全数成功（テンプレートは4銘柄）
    assert match.group(2) == match.group(3)
    assert int(match.group(3)) == 4


# ------------------------------------------------- 実データ全滅 → exit 2


def test_all_fetch_failed_exits_2_with_data_unavailable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(daily_brief, "fetch_prices", _fake_fetch({}))
    watchlist = _write_watchlist(tmp_path, ["7203"])

    rc = daily_brief.main(["--watchlist", str(watchlist)])
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.out.strip().splitlines()[-1] == "RESULT signals=0 watch=0/1 data=unavailable"
    # 環境差 (a)/(b) と対処（ネットワークポリシー・ローカル実行）への言及
    assert "ローカル環境" in captured.err
    assert "リモート環境" in captured.err
    assert "ネットワークポリシー" in captured.err


def test_partial_fetch_failure_continues_with_data_real(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """一部だけ取得できた場合は取得分で継続し data=real / exit 0。"""
    monkeypatch.setattr(daily_brief, "fetch_prices", _fake_fetch({"^N225": _make_plain_prices()}))
    _patch_save_report(monkeypatch, tmp_path)
    watchlist = _write_watchlist(tmp_path, ["7203"])

    rc = daily_brief.main(["--watchlist", str(watchlist)])
    captured = capsys.readouterr()

    assert rc == 0
    match = _last_result_line(captured.out)
    assert match.group(4) == "real"
    assert "## 取得失敗" in captured.out
    assert "7203" in captured.out  # 失敗した銘柄が列挙される


# ---------------------------------------------- watch フィールド（signals=0 の区別）


def test_market_only_watchlist_all_failed_reports_watch_0_of_n(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """市況のみ成功・ウォッチリスト全滅 → watch=0/N で「変化なし」と区別できる。"""
    df_map = {ticker: _make_plain_prices() for ticker, _ in daily_brief.MARKET_TICKERS}
    monkeypatch.setattr(daily_brief, "fetch_prices", _fake_fetch(df_map))
    _patch_save_report(monkeypatch, tmp_path)
    watchlist = _write_watchlist(tmp_path, ["7203", "6758"])

    rc = daily_brief.main(["--watchlist", str(watchlist)])
    captured = capsys.readouterr()

    assert rc == 0
    match = _last_result_line(captured.out)
    assert int(match.group(1)) == 0  # signals=0 だが「変化なし」ではない
    assert (match.group(2), match.group(3)) == ("0", "2")
    assert match.group(4) == "real"
    assert "## 取得失敗" in captured.out


def test_partial_watchlist_failure_reports_watch_ok_of_total(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """ウォッチリスト2銘柄中1銘柄のみ成功 → watch=1/2（data=real のまま）。"""
    df_map = {ticker: _make_plain_prices() for ticker, _ in daily_brief.MARKET_TICKERS}
    df_map["7203"] = _make_plain_prices()
    monkeypatch.setattr(daily_brief, "fetch_prices", _fake_fetch(df_map))
    _patch_save_report(monkeypatch, tmp_path)
    watchlist = _write_watchlist(tmp_path, ["7203", "6758"])

    rc = daily_brief.main(["--watchlist", str(watchlist)])
    captured = capsys.readouterr()

    assert rc == 0
    match = _last_result_line(captured.out)
    assert (match.group(2), match.group(3)) == ("1", "2")
    assert match.group(4) == "real"


def test_missing_watchlist_reports_watch_0_of_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """watchlist 未設定 → watch=0/0（全滅 watch=0/N とも変化なしとも区別できる）。"""
    df_map = {ticker: _make_plain_prices() for ticker, _ in daily_brief.MARKET_TICKERS}
    monkeypatch.setattr(daily_brief, "fetch_prices", _fake_fetch(df_map))
    _patch_save_report(monkeypatch, tmp_path)

    rc = daily_brief.main(["--watchlist", str(tmp_path / "no-such-watchlist.csv")])
    captured = capsys.readouterr()

    assert rc == 0
    match = _last_result_line(captured.out)
    assert int(match.group(1)) == 0
    assert (match.group(2), match.group(3)) == ("0", "0")
    assert match.group(4) == "real"
    assert "見つかりません" in captured.out  # テンプレート案内の注意書き


# ------------------------------------------------------------- --max-alerts


def test_max_alerts_limits_displayed_signal_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    df = _make_signal_rich_prices()
    n_detected = len(signals.detect_signals(df))
    assert n_detected >= 2, "テストの前提: 複数シグナルが立つ系列であること"

    df_map: dict[str, pd.DataFrame] = {ticker: _make_plain_prices() for ticker, _ in daily_brief.MARKET_TICKERS}
    df_map["7203"] = df
    monkeypatch.setattr(daily_brief, "fetch_prices", _fake_fetch(df_map))
    _patch_save_report(monkeypatch, tmp_path)
    watchlist = _write_watchlist(tmp_path, ["7203"])

    rc = daily_brief.main(["--watchlist", str(watchlist), "--max-alerts", "1"])
    captured = capsys.readouterr()

    assert rc == 0
    match = _last_result_line(captured.out)
    # RESULT の signals は表示絞り込み前の総数
    assert int(match.group(1)) == n_detected
    assert match.group(4) == "real"
    assert (match.group(2), match.group(3)) == ("1", "1")

    detail_lines = [ln for ln in captured.out.splitlines() if ln.startswith("  - [")]
    assert len(detail_lines) == 1
    assert "急変動" in detail_lines[0]  # 優先度最上位の price_move が残る
    assert "表示省略" in captured.out
    assert f"シグナル {n_detected} 件" in captured.out  # 見出しの件数は総数のまま


def test_max_alerts_no_truncation_when_within_limit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    df = _make_signal_rich_prices()
    n_detected = len(signals.detect_signals(df))

    df_map: dict[str, pd.DataFrame] = {ticker: _make_plain_prices() for ticker, _ in daily_brief.MARKET_TICKERS}
    df_map["7203"] = df
    monkeypatch.setattr(daily_brief, "fetch_prices", _fake_fetch(df_map))
    _patch_save_report(monkeypatch, tmp_path)
    watchlist = _write_watchlist(tmp_path, ["7203"])

    rc = daily_brief.main(["--watchlist", str(watchlist), "--max-alerts", str(n_detected)])
    captured = capsys.readouterr()

    assert rc == 0
    detail_lines = [ln for ln in captured.out.splitlines() if ln.startswith("  - [")]
    assert len(detail_lines) == n_detected
    assert "表示省略" not in captured.out


def test_max_alerts_rejects_non_positive() -> None:
    with pytest.raises(SystemExit):
        daily_brief.main(["--max-alerts", "0", "--synthetic"])

"""scripts/smoke_realdata.py のテスト（ネットワーク不要）。

スモークテスト本体は実データ経路（Yahoo）に到達することが目的だが、**このテストは
スクリプト自体の健全性**——例外分類・実データ検証・RESULT 行の形式・exit code の
分岐・1経路の失敗が他経路を妨げないこと——をモックだけで検証する（CI で回るもの）。

RESULT 行・exit code の契約の書き方は test_daily_brief_automation.py に揃えてある。
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stocklib.data import DataFetchError, normalize_code, period_to_days, synthetic_prices

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "smoke_realdata.py"

_spec = importlib.util.spec_from_file_location("smoke_realdata", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
smoke = importlib.util.module_from_spec(_spec)
# dataclass のフィールド型解決が sys.modules 経由でモジュール名前空間を引くため、
# exec_module の前に登録しておく（登録しないと ProbeResult の定義で AttributeError）。
sys.modules["smoke_realdata"] = smoke
_spec.loader.exec_module(smoke)

RESULT_RE = re.compile(r"^RESULT ok=(\d+)/(\d+) data=(real|unavailable)$")


# ------------------------------------------------------------------ helpers


def _make_real_prices(n: int = 22, last_close: float = 2897.0) -> pd.DataFrame:
    """実データらしい OHLCV（直近営業日まで・正の終値）。"""
    idx = pd.bdate_range(end=dt.date.today(), periods=n)
    close = np.linspace(last_close * 0.95, last_close, n)
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close,
         "Volume": np.full(n, 1_000_000.0)},
        index=idx,
    )


def _fake_fetch_prices(df_map: dict[str, pd.DataFrame], *, calls: list[dict] | None = None):
    """fetch_prices 互換のモック。df_map に無いコードは DataFetchError を投げる。"""

    def fetch(
        codes: str | list[str],
        period: str = "1y",
        interval: str = "1d",
        *,
        synthetic: bool = False,
        use_cache: bool = True,
        source: str | None = None,
    ) -> dict[str, pd.DataFrame]:
        code_list = [codes] if isinstance(codes, str) else list(codes)
        if calls is not None:
            calls.append({"codes": code_list, "period": period, "use_cache": use_cache})
        out: dict[str, pd.DataFrame] = {}
        for code in code_list:
            if code not in df_map:
                raise DataFetchError(f"{code} を Yahoo chart API から取得できませんでした（HTTP 429）。")
            out[code] = df_map[code]
        return out

    return fetch


REAL_INFO: dict[str, object] = {
    "名称": "Toyota Motor Corporation",
    "セクター": "Consumer Cyclical",
    "時価総額": 40_000_000_000_000,
    "PER（実績）": 11.2,
    "PBR": 1.2,
}


def _all_real(monkeypatch: pytest.MonkeyPatch, *, calls: list[dict] | None = None) -> None:
    """全4経路が実データで成功するようモックする。"""
    df_map = {
        "7203": _make_real_prices(),
        smoke.INDEX_TICKER: _make_real_prices(n=5, last_close=64_611.0),
        smoke.FX_TICKER: _make_real_prices(n=5, last_close=163.8),
    }
    monkeypatch.setattr(smoke, "fetch_prices", _fake_fetch_prices(df_map, calls=calls))
    monkeypatch.setattr(smoke, "fetch_info", lambda code: dict(REAL_INFO))


def _no_network_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    """失敗時に走る診断（実 HTTP）を無効化する。"""
    monkeypatch.setattr(smoke, "diagnose_yahoo_endpoints", lambda code=smoke.DEFAULT_CODE: ["(診断はモック)"])


def _last_result_line(stdout: str) -> re.Match[str]:
    last = stdout.strip().splitlines()[-1]
    match = RESULT_RE.match(last)
    assert match is not None, f"stdout 最終行が RESULT 形式ではない: {last!r}"
    return match


# ------------------------------------------------------------- import できる


def test_module_imports_and_exposes_contract() -> None:
    for attr in ("main", "run_probes", "probe_prices", "probe_info",
                 "format_result_line", "exit_code_for", "classify_error"):
        assert hasattr(smoke, attr), f"{attr} が公開されていない"


# ----------------------------------------------------------------- 例外分類


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (DataFetchError("7203.T を取得できませんでした（HTTP 429）。"), "http_429"),
        (DataFetchError("取得できませんでした（HTTP 404）。"), "http_404"),
        (DataFetchError("（ConnectionError: NameResolutionError: ...）"), "dns"),
        (DataFetchError("（ConnectionError: Connection reset by peer）"), "conn_reset"),
        (DataFetchError("（ReadTimeout: HTTPSConnectionPool timed out）"), "timeout"),
        (DataFetchError("（SSLError: certificate verify failed）"), "tls"),
        (DataFetchError("（ProxyError: cannot connect to proxy）"), "proxy"),
        (DataFetchError("STOCK_HACKER_DISABLE_YAHOO により Yahoo 経路は無効化されています。"),
         "yahoo_disabled"),
        (DataFetchError("Yahoo crumb を取得できませんでした。"), "crumb"),
        (DataFetchError("7203.T を取得できませんでした（空データ（result/timestamp なし））。"), "empty"),
        (ValueError("なにか別の失敗"), "valueerror"),
    ],
)
def test_classify_error(exc: BaseException, expected: str) -> None:
    assert smoke.classify_error(exc) == expected


def test_classify_error_prefers_http_status_over_generic_wording() -> None:
    """HTTP ステータスが読めるならネットワーク語彙より優先する（対処が変わるため）。"""
    exc = DataFetchError("接続を試みたが timed out ではなく HTTP 429 が返った")
    assert smoke.classify_error(exc) == "http_429"


def test_disable_flag_wins_over_http_status() -> None:
    """利用者の明示的な opt-out は他の分類より優先する。"""
    exc = DataFetchError("STOCK_HACKER_DISABLE_YAHOO 設定中（HTTP 429 は無関係）")
    assert smoke.classify_error(exc) == "yahoo_disabled"


# ------------------------------------------------------- 実データ検証（価格）


def test_verify_real_prices_accepts_fresh_real_series() -> None:
    assert smoke.verify_real_prices("7203", "1mo", _make_real_prices()) == []


def test_verify_real_prices_rejects_empty() -> None:
    problems = smoke.verify_real_prices("7203", "1mo", pd.DataFrame())
    assert problems and "空" in problems[0]


def test_verify_real_prices_detects_stale_series() -> None:
    """古い足しか無い（＝キャッシュ・保存物を掴んでいる）ことを検出する。"""
    df = _make_real_prices()
    df.index = pd.bdate_range(end=dt.date.today() - dt.timedelta(days=90), periods=len(df))
    problems = smoke.verify_real_prices("7203", "1mo", df, max_stale_days=7)
    assert any("古い" in p for p in problems)


def test_verify_real_prices_allows_stale_within_limit() -> None:
    df = _make_real_prices()
    df.index = pd.bdate_range(end=dt.date.today() - dt.timedelta(days=3), periods=len(df))
    assert smoke.verify_real_prices("7203", "1mo", df, max_stale_days=7) == []


def test_verify_real_prices_detects_synthetic_data() -> None:
    """--synthetic の合成値をそのまま返されたら「実データではない」と判定する。"""
    fake = synthetic_prices(normalize_code("7203"), days=period_to_days("1mo"))
    problems = smoke.verify_real_prices("7203", "1mo", fake)
    assert any("合成データ" in p for p in problems)


def test_verify_real_prices_detects_missing_columns() -> None:
    df = _make_real_prices().drop(columns=["Volume"])
    problems = smoke.verify_real_prices("7203", "1mo", df)
    assert any("Volume" in p for p in problems)


def test_verify_real_prices_detects_non_positive_close() -> None:
    df = _make_real_prices()
    df.iloc[-1, df.columns.get_loc("Close")] = 0.0
    problems = smoke.verify_real_prices("7203", "1mo", df)
    assert any("直近終値" in p for p in problems)


def test_verify_real_prices_detects_future_dated_series() -> None:
    df = _make_real_prices()
    df.index = pd.bdate_range(start=dt.date.today() + dt.timedelta(days=10), periods=len(df))
    problems = smoke.verify_real_prices("7203", "1mo", df)
    assert any("未来日付" in p for p in problems)


# ------------------------------------------------------- 実データ検証（情報）


def test_verify_real_info_accepts_real_payload() -> None:
    assert smoke.verify_real_info(dict(REAL_INFO)) == []


def test_verify_real_info_rejects_empty_payload() -> None:
    """crumb は取れたが quoteSummary の中身が空、という劣化を検出する。"""
    problems = smoke.verify_real_info({})
    assert problems and "空" in problems[0]


def test_verify_real_info_detects_synthetic_payload() -> None:
    from stocklib.data import fetch_info

    problems = smoke.verify_real_info(fetch_info("7203", synthetic=True))
    assert any("合成データ" in p for p in problems)


def test_verify_real_info_requires_fundamental_metrics() -> None:
    """名称だけ返ってファンダ指標が全滅（quoteSummary の部分劣化）を検出する。"""
    problems = smoke.verify_real_info({"名称": "Toyota Motor Corporation"})
    assert any("ファンダ指標" in p for p in problems)


# ------------------------------------------------------------------- 各経路


def test_probe_prices_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smoke, "fetch_prices", _fake_fetch_prices({"7203": _make_real_prices()}))
    result = smoke.probe_prices("7203", "1mo")
    assert result.ok is True
    assert result.name == "price:7203"
    assert result.error_kind is None
    assert result.elapsed_sec >= 0.0
    assert "終値" in result.detail


def test_probe_prices_bypasses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """キャッシュに当たると疎通を検証できないため use_cache=False で呼ぶこと。"""
    calls: list[dict] = []
    monkeypatch.setattr(
        smoke, "fetch_prices", _fake_fetch_prices({"7203": _make_real_prices()}, calls=calls)
    )
    smoke.probe_prices("7203", "1mo")
    assert calls and calls[0]["use_cache"] is False


def test_probe_prices_failure_reports_error_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smoke, "fetch_prices", _fake_fetch_prices({}))
    result = smoke.probe_prices("7203", "1mo")
    assert result.ok is False
    assert result.error_kind == "http_429"


def test_probe_prices_flags_synthetic_as_not_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """取得は成功しても値が合成なら失敗（not_real）にする。"""
    fake = synthetic_prices(normalize_code("7203"), days=period_to_days("1mo"))
    monkeypatch.setattr(smoke, "fetch_prices", _fake_fetch_prices({"7203": fake}))
    result = smoke.probe_prices("7203", "1mo")
    assert result.ok is False
    assert result.error_kind == "not_real"


def test_probe_info_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smoke, "fetch_info", lambda code: dict(REAL_INFO))
    result = smoke.probe_info("7203")
    assert result.ok is True
    assert result.name == "info:7203"
    assert "Toyota" in result.detail


def test_probe_info_failure_classifies_crumb_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """UA 変更で最初に壊れる crumb 経路の失敗が種別として見えること。"""

    def boom(code: str) -> dict[str, object]:
        raise DataFetchError("Yahoo crumb を取得できませんでした。")

    monkeypatch.setattr(smoke, "fetch_info", boom)
    result = smoke.probe_info("7203")
    assert result.ok is False
    assert result.error_kind == "crumb"


def test_probe_info_failure_classifies_429(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(code: str) -> dict[str, object]:
        raise DataFetchError("quoteSummary を取得できませんでした（HTTP 429）。")

    monkeypatch.setattr(smoke, "fetch_info", boom)
    assert smoke.probe_info("7203").error_kind == "http_429"


# ------------------------------------------- 1経路の失敗が他経路を妨げない


def test_run_probes_continues_after_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """価格・情報のどちらが落ちても全4経路が実行され結果が揃うこと。"""
    df_map = {
        smoke.INDEX_TICKER: _make_real_prices(n=5, last_close=64_611.0),
        smoke.FX_TICKER: _make_real_prices(n=5, last_close=163.8),
    }  # "7203" は意図的に欠落 → 価格経路が失敗
    monkeypatch.setattr(smoke, "fetch_prices", _fake_fetch_prices(df_map))

    def boom(code: str) -> dict[str, object]:
        raise DataFetchError("Yahoo crumb を取得できませんでした。")

    monkeypatch.setattr(smoke, "fetch_info", boom)

    results = smoke.run_probes("7203")
    assert [r.name for r in results] == [
        "price:7203", "info:7203", f"price:{smoke.INDEX_TICKER}", f"price:{smoke.FX_TICKER}"
    ]
    assert [r.ok for r in results] == [False, False, True, True]


# ------------------------------------------------------------- RESULT 行の形式


def test_format_result_line_all_ok() -> None:
    results = [smoke.ProbeResult(name=f"p{i}", ok=True, elapsed_sec=0.1, detail="") for i in range(4)]
    line = smoke.format_result_line(results)
    assert line == "RESULT ok=4/4 data=real"
    assert RESULT_RE.match(line)


def test_format_result_line_partial() -> None:
    results = [
        smoke.ProbeResult(name="a", ok=True, elapsed_sec=0.1, detail=""),
        smoke.ProbeResult(name="b", ok=True, elapsed_sec=0.1, detail=""),
        smoke.ProbeResult(name="c", ok=True, elapsed_sec=0.1, detail=""),
        smoke.ProbeResult(name="d", ok=False, elapsed_sec=0.1, detail="", error_kind="http_429"),
    ]
    assert smoke.format_result_line(results) == "RESULT ok=3/4 data=real"


def test_format_result_line_all_failed_is_unavailable() -> None:
    results = [
        smoke.ProbeResult(name=f"p{i}", ok=False, elapsed_sec=0.1, detail="", error_kind="dns")
        for i in range(4)
    ]
    assert smoke.format_result_line(results) == "RESULT ok=0/4 data=unavailable"


# ------------------------------------------------------------- exit code の分岐


def test_exit_code_all_ok_is_0() -> None:
    results = [smoke.ProbeResult(name="a", ok=True, elapsed_sec=0.1, detail="")]
    assert smoke.exit_code_for(results) == 0


def test_exit_code_partial_is_1() -> None:
    results = [
        smoke.ProbeResult(name="a", ok=True, elapsed_sec=0.1, detail=""),
        smoke.ProbeResult(name="b", ok=False, elapsed_sec=0.1, detail="", error_kind="http_429"),
    ]
    assert smoke.exit_code_for(results) == 1


def test_exit_code_all_failed_is_2() -> None:
    results = [
        smoke.ProbeResult(name="a", ok=False, elapsed_sec=0.1, detail="", error_kind="dns"),
        smoke.ProbeResult(name="b", ok=False, elapsed_sec=0.1, detail="", error_kind="dns"),
    ]
    assert smoke.exit_code_for(results) == 2


def test_exit_code_no_probes_is_2() -> None:
    assert smoke.exit_code_for([]) == 2


# ------------------------------------------------------------------ main()


def test_main_all_ok_exits_0_with_result_last(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _all_real(monkeypatch)
    rc = smoke.main([])
    captured = capsys.readouterr()

    assert rc == 0
    match = _last_result_line(captured.out)
    assert (match.group(1), match.group(2), match.group(3)) == ("4", "4", "real")


def test_main_partial_failure_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _all_real(monkeypatch)
    _no_network_diagnostics(monkeypatch)

    def boom(code: str) -> dict[str, object]:
        raise DataFetchError("quoteSummary を取得できませんでした（HTTP 429）。")

    monkeypatch.setattr(smoke, "fetch_info", boom)

    rc = smoke.main([])
    captured = capsys.readouterr()

    assert rc == 1
    match = _last_result_line(captured.out)
    assert (match.group(1), match.group(2), match.group(3)) == ("3", "4", "real")
    # 失敗種別が stderr に出る（429 と DNS 失敗で対処が変わるため）
    assert "http_429" in captured.err


def test_main_all_failed_exits_2_with_data_unavailable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(smoke, "fetch_prices", _fake_fetch_prices({}))

    def boom(code: str) -> dict[str, object]:
        raise DataFetchError("Yahoo crumb を取得できませんでした。")

    monkeypatch.setattr(smoke, "fetch_info", boom)
    _no_network_diagnostics(monkeypatch)

    rc = smoke.main([])
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.out.strip().splitlines()[-1] == "RESULT ok=0/4 data=unavailable"


def test_main_runs_diagnostics_only_on_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """成功時は診断の追加リクエストを投げない（Yahoo への無駄な負荷を避ける）。"""
    calls: list[str] = []
    monkeypatch.setattr(
        smoke, "diagnose_yahoo_endpoints",
        lambda code=smoke.DEFAULT_CODE: (calls.append(code), ["(診断)"])[1],
    )

    _all_real(monkeypatch)
    smoke.main([])
    capsys.readouterr()
    assert calls == []

    monkeypatch.setattr(smoke, "fetch_prices", _fake_fetch_prices({}))
    smoke.main([])
    out = capsys.readouterr().out
    assert calls == [smoke.DEFAULT_CODE]
    assert "診断" in out


def test_main_honors_custom_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    df_map = {
        "6758": _make_real_prices(),
        smoke.INDEX_TICKER: _make_real_prices(n=5, last_close=64_611.0),
        smoke.FX_TICKER: _make_real_prices(n=5, last_close=163.8),
    }
    monkeypatch.setattr(smoke, "fetch_prices", _fake_fetch_prices(df_map))
    monkeypatch.setattr(smoke, "fetch_info", lambda code: dict(REAL_INFO))

    rc = smoke.main(["--code", "6758"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "price:6758" in out
    assert "info:6758" in out


def test_main_rejects_negative_stale_days() -> None:
    with pytest.raises(SystemExit):
        smoke.main(["--max-stale-days", "-1"])


def test_diagnostics_skipped_when_yahoo_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """STOCK_HACKER_DISABLE_YAHOO 設定時は診断でも Yahoo を叩かない（利用者の opt-out を尊重）。"""
    monkeypatch.setenv("STOCK_HACKER_DISABLE_YAHOO", "1")
    lines = smoke.diagnose_yahoo_endpoints("7203")
    assert len(lines) == 1
    assert "スキップ" in lines[0]

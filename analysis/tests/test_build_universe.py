"""build_universe（J-Quants 全銘柄ユニバース構築 CLI）のテスト。

test_jquants.py と同様にネットワーク不使用。fetch_listed_info はモック
（または応答相当の DataFrame を直接構築）し、フィルタ・5桁→4桁変換・
除外カウント・screen.py 互換性・CLI の入出力を検証する。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import build_universe as bu
from screen import load_universe
from stocklib.jquants import JQuantsAuthError, JQuantsError

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# テストデータ（J-Quants /listed/info 応答相当）
# ---------------------------------------------------------------------------


def _row(code: str, name: str, sector: str, market: str) -> dict[str, str]:
    return {
        "Code": code,
        "CompanyName": name,
        "Sector33CodeName": sector,
        "MarketCodeName": market,
    }


def _listed_df() -> pd.DataFrame:
    rows = [
        _row("72030", "トヨタ自動車", "輸送用機器", "プライム"),
        _row("67580", "ソニーグループ", "電気機器", "プライム"),
        _row("79740", "任天堂", "その他製品", "プライム"),  # 「その他製品」は普通株の33業種
        _row("130A0", "テスト英字コード", "サービス業", "グロース"),
        _row("29170", "テスト食品", "食料品", "スタンダード"),
        _row("25935", "テスト優先株式", "食料品", "プライム"),  # 予備桁が非0 → 4桁化不可
        _row("13060", "TOPIX連動型ETF", "その他", "その他"),  # ETF
        _row("89510", "テスト不動産投資法人", "その他", "その他"),  # REIT
    ]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# to_screen_code — 5桁 → 4桁変換
# ---------------------------------------------------------------------------


def test_to_screen_code_converts_5digit() -> None:
    assert bu.to_screen_code("72030") == "7203"
    assert bu.to_screen_code(" 67580 ") == "6758"


def test_to_screen_code_alpha_codes() -> None:
    # 2024年以降の英字入りコードも変換される（小文字は大文字化）
    assert bu.to_screen_code("130A0") == "130A"
    assert bu.to_screen_code("130a0") == "130A"


def test_to_screen_code_keeps_4char() -> None:
    assert bu.to_screen_code("7203") == "7203"
    assert bu.to_screen_code("130A") == "130A"


def test_to_screen_code_rejects_preferred_and_invalid() -> None:
    assert bu.to_screen_code("25935") is None  # 予備桁が非0（優先株等）
    assert bu.to_screen_code("720300") is None
    assert bu.to_screen_code("") is None
    assert bu.to_screen_code("^N225") is None


# ---------------------------------------------------------------------------
# build_universe — フィルタと集計
# ---------------------------------------------------------------------------


def test_build_default_excludes_etf_reit_and_skips_bad_codes() -> None:
    universe, stats = bu.build_universe(_listed_df())
    assert list(universe.columns) == ["code", "name", "sector"]
    assert list(universe["code"]) == ["130A", "2917", "6758", "7203", "7974"]  # code 昇順
    assert stats.total == 8
    assert stats.non_equity_excluded == 2  # ETF と REIT
    assert stats.code_skipped == 1
    assert stats.skipped_codes == ("25935",)
    assert stats.kept == 5
    # 「その他製品」（任天堂）は完全一致判定なので除外されない
    assert "7974" in set(universe["code"])


def test_build_include_etf_reit() -> None:
    universe, stats = bu.build_universe(_listed_df(), exclude_etf_reit=False)
    codes = set(universe["code"])
    assert {"1306", "8951"} <= codes
    assert stats.non_equity_excluded == 0
    assert stats.kept == 7  # 優先株のみスキップ


def test_build_market_filter_partial_match() -> None:
    universe, stats = bu.build_universe(_listed_df(), market="プライム")
    assert list(universe["code"]) == ["6758", "7203", "7974"]
    # グロース・スタンダード・「その他」x2 の4件が市場区分フィルタで除外される
    assert stats.market_excluded == 4
    assert stats.non_equity_excluded == 0  # 既に市場区分で除外済み
    assert stats.code_skipped == 1  # 優先株はプライム所属だが4桁化不可


def test_build_sector33_filter_partial_match() -> None:
    universe, stats = bu.build_universe(_listed_df(), sector33="食料")
    assert list(universe["code"]) == ["2917"]
    assert stats.sector_excluded == 6
    assert stats.code_skipped == 1  # 優先株（食料品）は4桁化不可でスキップ


def test_build_deduplicates_codes() -> None:
    df = pd.concat([_listed_df(), _listed_df()], ignore_index=True)
    universe, stats = bu.build_universe(df)
    assert list(universe["code"]) == ["130A", "2917", "6758", "7203", "7974"]
    assert stats.total == 16
    assert stats.kept == 5


def test_build_missing_sector_becomes_placeholder() -> None:
    df = pd.DataFrame(
        [{"Code": "72030", "CompanyName": "トヨタ自動車", "MarketCodeName": "プライム"}]
    )
    universe, _ = bu.build_universe(df)  # Sector33CodeName 列なし・exclude は市場区分のみで判定
    assert universe["sector"].iloc[0] == "-"


def test_build_missing_required_columns_raises() -> None:
    with pytest.raises(ValueError) as exc_info:
        bu.build_universe(pd.DataFrame([{"Code": "72030"}]))
    assert "CompanyName" in str(exc_info.value)


def test_build_market_filter_without_column_raises() -> None:
    df = pd.DataFrame([{"Code": "72030", "CompanyName": "トヨタ自動車"}])
    with pytest.raises(ValueError) as exc_info:
        bu.build_universe(df, market="プライム")
    assert "MarketCodeName" in str(exc_info.value)


def test_build_sector_filter_without_column_raises() -> None:
    df = pd.DataFrame([{"Code": "72030", "CompanyName": "トヨタ自動車"}])
    with pytest.raises(ValueError):
        bu.build_universe(df, sector33="食料")


# ---------------------------------------------------------------------------
# write_universe_csv — screen.py 互換性
# ---------------------------------------------------------------------------


def test_written_csv_is_loadable_by_screen(tmp_path: Path) -> None:
    universe, _ = bu.build_universe(_listed_df())
    out = tmp_path / "sub" / "universe.csv"  # ディレクトリ自動作成も確認
    bu.write_universe_csv(universe, out, header_comment="テスト生成")
    loaded = load_universe(out)  # screen.py のローダで読める（# コメント行は無視）
    assert list(loaded.columns) == ["code", "name", "sector"]
    assert list(loaded["code"]) == ["130A", "2917", "6758", "7203", "7974"]
    assert all(isinstance(c, str) for c in loaded["code"])  # code は文字列として保持される
    first_line = out.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("# ")


def test_write_universe_csv_rejects_multiline_comment(tmp_path: Path) -> None:
    universe, _ = bu.build_universe(_listed_df())
    with pytest.raises(ValueError):
        bu.write_universe_csv(universe, tmp_path / "u.csv", header_comment="a\nb")


def test_default_out_is_under_gitignored_data_dir() -> None:
    rel = bu.DEFAULT_OUT.relative_to(REPO_ROOT)
    assert rel.parts[:2] == ("data", "universe")


# ---------------------------------------------------------------------------
# main — CLI の入出力（fetch_listed_info をモック）
# ---------------------------------------------------------------------------


def _mock_listed(monkeypatch: pytest.MonkeyPatch, df: pd.DataFrame) -> list[object]:
    """fetch_listed_info をモックし、渡された date 引数の履歴を返す。"""
    dates: list[object] = []

    def fake(date: str | None = None) -> pd.DataFrame:
        dates.append(date)
        return df

    monkeypatch.setattr(bu, "fetch_listed_info", fake)
    return dates


def test_main_writes_csv_and_reports_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _mock_listed(monkeypatch, _listed_df())
    out = tmp_path / "universe" / "all.csv"
    assert bu.main(["--out", str(out)]) == 0
    captured = capsys.readouterr()
    assert str(out) in captured.out  # 生成した CSV パス
    assert "ユニバース: 5 銘柄" in captured.out  # 銘柄数
    assert "スキップ: 1 件" in captured.out  # 変換不可コードの報告
    assert "25935" in captured.out
    assert "ETF・REIT等（普通株以外）を除外: 2 件" in captured.out
    assert out.exists()
    assert list(load_universe(out)["code"]) == ["130A", "2917", "6758", "7203", "7974"]


def test_main_passes_filters_and_date(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dates = _mock_listed(monkeypatch, _listed_df())
    out = tmp_path / "prime.csv"
    assert bu.main(["--out", str(out), "--market", "プライム", "--date", "2025-01-06"]) == 0
    assert dates == ["2025-01-06"]  # --date が fetch_listed_info に渡る
    captured = capsys.readouterr()
    assert "市場区分フィルタ（--market プライム）で除外: 4 件" in captured.out
    assert list(load_universe(out)["code"]) == ["6758", "7203", "7974"]


def test_main_no_exclude_etf_reit_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _mock_listed(monkeypatch, _listed_df())
    out = tmp_path / "all-with-etf.csv"
    assert bu.main(["--out", str(out), "--no-exclude-etf-reit"]) == 0
    captured = capsys.readouterr()
    assert "ETF・REIT等" not in captured.out
    assert {"1306", "8951"} <= set(load_universe(out)["code"])


def test_main_auth_error_message_passthrough(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """JQuantsAuthError の導入手順メッセージを握りつぶさず stderr に表示する。"""

    def fake(date: str | None = None) -> pd.DataFrame:
        raise JQuantsAuthError("JQUANTS_API_KEY が未設定です（導入手順: https://jpx-jquants.com/）")

    monkeypatch.setattr(bu, "fetch_listed_info", fake)
    out = tmp_path / "never.csv"
    assert bu.main(["--out", str(out)]) == 1
    captured = capsys.readouterr()
    assert "JQUANTS_API_KEY" in captured.err
    assert "導入手順" in captured.err
    assert not out.exists()


def test_main_generic_jquants_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake(date: str | None = None) -> pd.DataFrame:
        raise JQuantsError("上場銘柄一覧が空でした")

    monkeypatch.setattr(bu, "fetch_listed_info", fake)
    assert bu.main(["--out", str(tmp_path / "never.csv")]) == 1
    assert "上場銘柄一覧が空でした" in capsys.readouterr().err


def test_main_empty_result_returns_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _mock_listed(monkeypatch, _listed_df())
    out = tmp_path / "empty.csv"
    assert bu.main(["--out", str(out), "--market", "存在しない市場"]) == 1
    captured = capsys.readouterr()
    assert "0件" in captured.err
    assert not out.exists()

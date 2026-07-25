#!/usr/bin/env python3
"""実データ経路（Yahoo Finance）のスモークテスト。

**pytest（`analysis/tests`）は全て `--synthetic` かモックで動くため、外部サービスの
実挙動は誰も見ていない。** このスクリプトは、そこに開いた穴——「テストは全部緑なのに
実データ取得は壊れている」——を最小コストで塞ぐためのものである。

実例（2026年）: `stocklib.data` の User-Agent をブラウザ偽装から自ツール名
（`stock-hacker/1.0`）に変更したところ、Yahoo が HTTP 429 を返して実データ取得が
完全に停止した。しかし pytest 682件は全て緑のままで、マージ後まで誰も気付かなかった。
対照実験（旧UA=200 / 新UA=429 / 旧UA再試行=200）で原因は確定している。

**ネットワークが必要なので CI には入れない。** `analysis/stocklib/data.py`・
`events.py`・`fundamentals.py`・`jquants.py`・`edinet.py` など外部通信に関わる実装を
変更したら、pytest が緑でも手元・リモート環境でこれを明示的に実行すること。

使い方（リポジトリルートから）::

    python3 scripts/smoke_realdata.py
    python3 scripts/smoke_realdata.py --code 6758 --max-stale-days 10

検査する経路（それぞれ成否・所要時間を報告し、1経路の失敗は他経路の実行を妨げない）:

1. ``fetch_prices("<code>", period="1mo")`` — Yahoo chart API（価格）
2. ``fetch_info("<code>")`` — Yahoo quoteSummary（cookie + crumb を経由する**別経路**。
   UA 変更で最初に壊れたのはここ）
3. ``fetch_prices("^N225", period="5d")`` — 指数ティッカー
4. ``fetch_prices("USDJPY=X", period="5d")`` — 為替ティッカー

各経路は「取得できたこと」だけでなく **取得した値が実データであること**まで検証する
（``--synthetic`` の合成値と一致しないこと・直近営業日に近い日付を含むこと・
基本情報が合成ダミー（"合成データ銘柄" / セクター "Synthetic"）でないこと）。

``data/cache/`` の当日キャッシュに当たると実際の疎通を検証できないため、価格取得は
``fetch_prices(..., use_cache=False)`` でキャッシュを**読まず・書かず**に迂回する
（``fetch_info`` はそもそもキャッシュを持たない）。

自動実行・手動運用のための機械可読な契約（他 CLI に倣う）:

- stdout の最終行に ``RESULT ok=<成功経路数>/<総経路数> data=<real|unavailable>``
  を出力する。1経路でも実データを検証できれば ``data=real``、全滅なら ``unavailable``
  （このスクリプトは合成データを一切使わないため ``data=synthetic`` は出さない）。
- exit code: 全経路成功=0 / 一部失敗=1 / 全滅=2。
- 失敗時は例外の種別と HTTP ステータスを分類して表示する（429 なのか DNS 失敗なのか
  接続 reset なのかで対処が変わるため）。加えて Yahoo の各エンドポイントへ生の
  GET を投げた素のステータスも診断として出す。
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "analysis") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "analysis"))

import pandas as pd  # noqa: E402  (sys.path 調整後に import する)

from stocklib import data as stockdata  # noqa: E402
from stocklib.data import (  # noqa: E402
    OHLCV_COLUMNS,
    fetch_info,
    fetch_prices,
    normalize_code,
    period_to_days,
    synthetic_prices,
)

# 価格経路の既定パラメータ（1経路1銘柄・短期間。スロットル 0.5 秒込みで10秒程度）
DEFAULT_CODE: str = "7203"
DEFAULT_STOCK_PERIOD: str = "1mo"
DEFAULT_SHORT_PERIOD: str = "5d"
INDEX_TICKER: str = "^N225"
FX_TICKER: str = "USDJPY=X"

# 最終足の日付が今日から何暦日離れていたら「古すぎる」とみなすか。
# 日本市場は連休（GW・年末年始）で最大10日近く開かないため既定は余裕を持たせる。
DEFAULT_MAX_STALE_DAYS: int = 7

RESULT_PREFIX: str = "RESULT"


@dataclass(frozen=True)
class ProbeResult:
    """1経路の検査結果。

    Attributes:
        name: 経路名（``"price:7203"`` / ``"info:7203"`` など）。
        ok: 取得に成功し、かつ実データとして検証できたか。
        elapsed_sec: 所要時間（秒）。
        detail: 成功時は取得内容の要約、失敗時は原因の説明。
        error_kind: 失敗の種別トークン（``"http_429"`` / ``"dns"`` / ``"not_real"`` 等）。
            成功時は ``None``。
    """

    name: str
    ok: bool
    elapsed_sec: float
    detail: str
    error_kind: str | None = None


# ----------------------------------------------------------------- エラー分類


_HTTP_RE = re.compile(r"HTTP\s+(\d{3})")


def classify_error(exc: BaseException) -> str:
    """例外を対処の分かれ目ごとの短いトークンに分類する。

    ``stocklib.data`` はネットワーク例外・非200ステータスを ``DataFetchError`` の
    メッセージ文字列に畳み込むため（``"HTTP 429"`` / ``"ConnectionError: ..."``）、
    型と文字列の両方を見て分類する。

    Returns:
        ``"http_429"``（レート制限・UA 拒否）/ ``"dns"`` / ``"conn_reset"`` /
        ``"timeout"`` / ``"tls"`` / ``"proxy"`` / ``"yahoo_disabled"`` /
        ``"empty"`` / ``"crumb"`` / それ以外は例外クラス名の小文字。
    """
    text = f"{type(exc).__name__}: {exc}"
    if "STOCK_HACKER_DISABLE_YAHOO" in text:
        return "yahoo_disabled"
    m = _HTTP_RE.search(text)
    if m is not None:
        return f"http_{m.group(1)}"
    lowered = text.lower()
    for needle, kind in (
        ("nameresolution", "dns"),
        ("gaierror", "dns"),
        ("name or service not known", "dns"),
        ("temporary failure in name resolution", "dns"),
        ("connection reset", "conn_reset"),
        ("connectionreseterror", "conn_reset"),
        ("proxyerror", "proxy"),
        ("timeout", "timeout"),
        ("timed out", "timeout"),
        ("sslerror", "tls"),
        ("certificate", "tls"),
    ):
        if needle in lowered:
            return kind
    if "crumb" in lowered:
        return "crumb"
    if "空データ" in text:
        return "empty"
    return type(exc).__name__.lower()


# ------------------------------------------------------------- 実データの検証


def verify_real_prices(
    code: str,
    period: str,
    df: pd.DataFrame,
    *,
    max_stale_days: int = DEFAULT_MAX_STALE_DAYS,
    today: dt.date | None = None,
) -> list[str]:
    """取得した OHLCV が「実データらしいか」を検証し、問題点を列挙する。

    Args:
        code: 呼び出しに使った銘柄コード。
        period: 取得期間（合成データとの一致判定で同じ本数を再現するのに使う）。
        df: :func:`fetch_prices` が返した DataFrame。
        max_stale_days: 最終足の日付が今日から離れていてよい暦日数の上限。
        today: 基準日（テスト用。既定は実行日）。

    Returns:
        問題点の説明文のリスト。空リストなら実データとして検証できたことを意味する。
    """
    problems: list[str] = []
    if df is None or len(df) == 0:
        return ["空の DataFrame が返った"]
    missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing:
        problems.append(f"OHLCV 列が欠落: {', '.join(missing)}")
    if "Close" not in df.columns:
        return problems

    closes = pd.to_numeric(df["Close"], errors="coerce")
    last_close = float(closes.iloc[-1]) if len(closes) else float("nan")
    if not math.isfinite(last_close) or last_close <= 0:
        problems.append(f"直近終値が不正: {last_close!r}")

    # 鮮度: 最終足が直近営業日に近いこと（当日キャッシュや古い保存物を掴んでいない）
    base = today or dt.date.today()
    try:
        last_date = pd.Timestamp(df.index[-1]).date()
    except (TypeError, ValueError):
        problems.append(f"インデックスが日付として解釈できない: {df.index[-1]!r}")
        last_date = None
    if last_date is not None:
        stale = (base - last_date).days
        if stale > max_stale_days:
            problems.append(
                f"最終足 {last_date} が {stale} 日前で古い（上限 {max_stale_days} 日）"
            )
        elif stale < 0:
            problems.append(f"最終足 {last_date} が未来日付")

    # 合成データ（--synthetic）と一致していないこと。fetch_prices は
    # synthetic_prices(normalize_code(code), days=period_to_days(period)) を返すため、
    # 同じ引数で再現して突き合わせる。
    try:
        fake = synthetic_prices(normalize_code(code), days=period_to_days(period))
    except Exception:  # noqa: BLE001 - 検証補助が失敗しても本体の判定は続ける
        fake = None
    if fake is not None and len(fake) and math.isfinite(last_close):
        fake_last = float(fake["Close"].iloc[-1])
        if math.isclose(last_close, fake_last, rel_tol=1e-9, abs_tol=0.0):
            problems.append("合成データ（--synthetic）と同じ値が返っている")
    return problems


def verify_real_info(info: dict[str, object]) -> list[str]:
    """取得した基本情報が「実データらしいか」を検証し、問題点を列挙する。

    ``fetch_info(..., synthetic=True)`` は名称 ``"合成データ銘柄 ..."`` ・
    セクター ``"Synthetic"`` のダミーを返すため、それらでないことを確認する。
    quoteSummary が空を返した場合（crumb は取れたが中身が無い）も検出する。
    """
    problems: list[str] = []
    if not info:
        return ["基本情報が空だった（quoteSummary の中身なし）"]
    name = info.get("名称")
    if isinstance(name, str) and name.startswith("合成データ銘柄"):
        problems.append("合成データのダミー名称が返っている")
    if info.get("セクター") == "Synthetic":
        problems.append("合成データのダミーセクターが返っている")
    if name is None:
        problems.append("名称が取得できていない")
    numeric_keys = ("時価総額", "PER（実績）", "PBR", "52週高値")
    if not any(k in info for k in numeric_keys):
        problems.append(
            f"ファンダ指標が1つも取れていない（{' / '.join(numeric_keys)} のいずれも無し）"
        )
    return problems


# ------------------------------------------------------------------- 各経路


def probe_prices(
    code: str,
    period: str,
    *,
    max_stale_days: int = DEFAULT_MAX_STALE_DAYS,
) -> ProbeResult:
    """価格取得経路（Yahoo chart API）を1銘柄で検査する。

    ``use_cache=False`` で ``data/cache/`` を読まず・書かずに迂回するため、
    当日キャッシュが残っていても実際の疎通を検証できる。
    """
    name = f"price:{code}"
    started = time.monotonic()
    try:
        frames = fetch_prices(code, period=period, use_cache=False)
        df = frames[code]
    except Exception as exc:  # noqa: BLE001 - 全経路を試すため握って結果に畳む
        return ProbeResult(
            name=name,
            ok=False,
            elapsed_sec=time.monotonic() - started,
            detail=str(exc).strip(),
            error_kind=classify_error(exc),
        )
    elapsed = time.monotonic() - started
    problems = verify_real_prices(code, period, df, max_stale_days=max_stale_days)
    if problems:
        return ProbeResult(
            name=name,
            ok=False,
            elapsed_sec=elapsed,
            detail="取得はできたが実データとして検証できず: " + " / ".join(problems),
            error_kind="not_real",
        )
    last_date = pd.Timestamp(df.index[-1]).date()
    last_close = float(pd.to_numeric(df["Close"]).iloc[-1])
    return ProbeResult(
        name=name,
        ok=True,
        elapsed_sec=elapsed,
        detail=f"period={period} {len(df)}本 最終足={last_date} 終値={last_close:,.2f}",
    )


def probe_info(code: str) -> ProbeResult:
    """基本情報取得経路（Yahoo quoteSummary、cookie + crumb）を検査する。

    価格の chart API とは別経路（cookie 取得 → crumb 取得 → quoteSummary）であり、
    User-Agent などリクエストヘッダの変更で真っ先に壊れるのはこちら。
    """
    name = f"info:{code}"
    started = time.monotonic()
    try:
        info = fetch_info(code)
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            name=name,
            ok=False,
            elapsed_sec=time.monotonic() - started,
            detail=str(exc).strip(),
            error_kind=classify_error(exc),
        )
    elapsed = time.monotonic() - started
    problems = verify_real_info(info)
    if problems:
        return ProbeResult(
            name=name,
            ok=False,
            elapsed_sec=elapsed,
            detail="取得はできたが実データとして検証できず: " + " / ".join(problems),
            error_kind="not_real",
        )
    label = info.get("名称") or code
    keys = "・".join(k for k in ("PER（実績）", "PBR", "時価総額") if k in info)
    return ProbeResult(
        name=name,
        ok=True,
        elapsed_sec=elapsed,
        detail=f"{label} / 取得項目 {len(info)}件（{keys or '基本項目'}）",
    )


def run_probes(
    code: str = DEFAULT_CODE,
    *,
    max_stale_days: int = DEFAULT_MAX_STALE_DAYS,
) -> list[ProbeResult]:
    """全経路を順に検査する（1経路の失敗が他経路の実行を妨げないこと）。"""
    return [
        probe_prices(code, DEFAULT_STOCK_PERIOD, max_stale_days=max_stale_days),
        probe_info(code),
        probe_prices(INDEX_TICKER, DEFAULT_SHORT_PERIOD, max_stale_days=max_stale_days),
        probe_prices(FX_TICKER, DEFAULT_SHORT_PERIOD, max_stale_days=max_stale_days),
    ]


# ------------------------------------------------------------------- 診断


def diagnose_yahoo_endpoints(code: str = DEFAULT_CODE) -> list[str]:
    """失敗時に Yahoo 各エンドポイントの**生の HTTP ステータス**を確認する。

    ``stocklib.data`` は非200を ``DataFetchError`` のメッセージに畳んでしまい、
    quoteSummary 経路では crumb 取得の失敗理由（429 なのか DNS なのか）が
    見えなくなる。対処を分けるために素の GET を1往復ずつ投げて確認する。

    ``stocklib.data`` と同じ手順（cookie 取得 → getcrumb）を踏むため、``getcrumb`` の
    401 は「cookie を持っていても弾かれた」という意味を持つ（cookie 無しの素の GET は
    常に 401 になり診断にならない）。``STOCK_HACKER_DISABLE_YAHOO`` が設定されている
    場合は利用者の意思を尊重して診断を行わない。

    Returns:
        ``"chart(query1...): HTTP 429"`` のような1行ずつの診断文リスト。
        診断できない場合はその理由の1行を返す。
    """
    try:
        stockdata.ensure_yahoo_allowed()
    except Exception as exc:  # noqa: BLE001 - DataFetchError（Yahoo 経路が無効）
        return [f"Yahoo 経路が無効化されているため生ステータスの診断をスキップ: {exc}"]
    try:
        import requests
    except ImportError:
        return ["requests 未導入のため生ステータスの診断をスキップ"]

    ua = getattr(stockdata, "_YAHOO_UA", "")
    hosts = getattr(stockdata, "_YAHOO_HOSTS", ("query1.finance.yahoo.com",))
    host = hosts[0]
    ticker = normalize_code(code)

    session = requests.Session()
    session.headers.update({"User-Agent": ua})
    lines: list[str] = [f"User-Agent: {ua[:60]}{'...' if len(ua) > 60 else ''}"]
    try:  # cookie 取得（data.py と同じ手順。404 でも Set-Cookie は返る）
        stockdata.yahoo_throttle()
        session.get("https://fc.yahoo.com", timeout=15)
    except Exception as exc:  # noqa: BLE001
        lines.append(f"cookie(fc.yahoo.com): {type(exc).__name__}: {exc}")

    targets: list[tuple[str, str]] = [
        ("crumb", f"https://{host}/v1/test/getcrumb"),
        ("chart", f"https://{host}/v8/finance/chart/{ticker}?range=5d&interval=1d"),
    ]
    for label, url in targets:
        try:
            stockdata.yahoo_throttle()
            resp = session.get(url, timeout=15)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{label}({host}): {type(exc).__name__}: {exc}")
            continue
        note = ""
        if resp.status_code == 429:
            note = "  ← レート制限 / UA 拒否。UA を変更していないか確認する"
        elif resp.status_code == 401:
            note = "  ← cookie を送っても認証されない（crumb 経路の劣化）"
        lines.append(f"{label}({host}): HTTP {resp.status_code}{note}")
    return lines


# --------------------------------------------------------------- 出力・契約


def format_result_line(results: list[ProbeResult]) -> str:
    """機械可読な最終行 ``RESULT ok=<成功>/<総数> data=<real|unavailable>`` を組み立てる。"""
    ok = sum(1 for r in results if r.ok)
    total = len(results)
    data = "real" if ok > 0 else "unavailable"
    return f"{RESULT_PREFIX} ok={ok}/{total} data={data}"


def exit_code_for(results: list[ProbeResult]) -> int:
    """exit code を決める（全経路成功=0 / 一部失敗=1 / 全滅=2）。"""
    ok = sum(1 for r in results if r.ok)
    if not results:
        return 2
    if ok == len(results):
        return 0
    return 1 if ok > 0 else 2


def render_report(results: list[ProbeResult], *, diagnostics: list[str] | None = None) -> str:
    """人間向けの本文（RESULT 行は含まない）を組み立てる。"""
    width = max((len(r.name) for r in results), default=0)
    lines: list[str] = []
    for r in results:
        tag = "OK  " if r.ok else "FAIL"
        lines.append(f"[{tag}] {r.name:<{width}}  {r.elapsed_sec:5.2f}s  {r.detail}")
    if diagnostics:
        lines.append("")
        lines.append("診断（失敗があったため Yahoo エンドポイントの生ステータスを確認）:")
        lines.extend(f"  - {d}" for d in diagnostics)
        lines.append("")
        lines.append(
            "対処の目安: http_429=UA/レート制限（stocklib.data の User-Agent を"
            "変更していないか確認）/ dns・conn_reset・proxy=ネットワーク到達性 / "
            "yahoo_disabled=STOCK_HACKER_DISABLE_YAHOO が設定されている / "
            "not_real=取得はできたが値が古い・合成値と一致"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="実データ経路（Yahoo Finance）が生きているかを最小コストで確認する"
        "（ネットワーク必須・CI 対象外）",
    )
    parser.add_argument(
        "--code", default=DEFAULT_CODE,
        help=f"価格・基本情報の検査に使う銘柄コード（既定: {DEFAULT_CODE}）",
    )
    parser.add_argument(
        "--max-stale-days", type=int, default=DEFAULT_MAX_STALE_DAYS, metavar="N",
        help=f"最終足が今日から離れていてよい暦日数の上限（既定: {DEFAULT_MAX_STALE_DAYS}。"
             "連休をまたぐ場合は大きめに）",
    )
    args = parser.parse_args(argv)
    if args.max_stale_days < 0:
        parser.error("--max-stale-days には 0 以上の整数を指定してください")

    print(
        f"実データ経路スモークテスト  {dt.datetime.now().isoformat(timespec='seconds')}\n"
        "  キャッシュ: 迂回（fetch_prices(use_cache=False)）/ 合成データは一切使わない"
    )
    results = run_probes(args.code, max_stale_days=args.max_stale_days)
    diagnostics = diagnose_yahoo_endpoints(args.code) if any(not r.ok for r in results) else None
    print(render_report(results, diagnostics=diagnostics))
    if any(not r.ok for r in results):
        kinds = sorted({r.error_kind for r in results if r.error_kind})
        print(f"失敗種別: {', '.join(kinds)}", file=sys.stderr)
    print(format_result_line(results))
    return exit_code_for(results)


if __name__ == "__main__":
    raise SystemExit(main())

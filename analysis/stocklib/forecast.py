"""翌営業日の機械予想・答え合わせ・実績台帳モジュール。

「夜間に翌営業日の予想を機械的に生成し、翌日の実績で答え合わせをして、
台帳（``forecasts/ledger.csv``）に蓄積していく」ループの中核ロジック。

このモジュールが提供するもの:

- :func:`make_forecast` — OHLCV から**固定ルールの合成スコア**で翌営業日の方向・
  上昇確率・予想リターン・予想レンジを算出する（:class:`Forecast`）。
- :func:`grade_forecast` — 予想を後日の実績（次営業日の終値）で採点する
  （方向的中・レンジ的中・Brier スコア・予想リターン誤差。:class:`GradeResult`）。
- 台帳 I/O（:func:`load_ledger` / :func:`upsert_forecast` / :func:`save_ledger`）。
- 集計（:func:`summarize` / :func:`calibration_table`）。

**重要（本リポジトリの規約）**: ここで算出する予想は、テクニカル指標を機械的に
合成しただけの**再現可能なベースライン**であり、将来の騰落の断定でも売買助言でも
ない。予想モデルの重みは過去データにフィットさせておらず（既定値は固定）、
「どのシグナルが当たるか」は蓄積した台帳を :func:`summarize` で継続測定して
初めて分かる、という設計思想（＝データの醸成）。合成データ（``--synthetic``）で
作った予想は台帳の ``data`` 列に ``synthetic`` と記録し、実データの実績で
採点しない（:func:`grade_forecast` の呼び出し側で ``data`` を突き合わせる）。
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import pandas as pd

from stocklib import indicators
from stocklib.data import REPO_ROOT

# --- 台帳の場所 ---
FORECASTS_DIR: Path = REPO_ROOT / "forecasts"
DEFAULT_LEDGER: Path = FORECASTS_DIR / "ledger.csv"

# --- 予想モデルのパラメータ（固定。過去データにフィットさせていない） ---
# 変更時は make_forecast の docstring と knowledge/math/forecast-evaluation-and-calibration.md も更新すること。
FAST_SMA: int = 25
SLOW_SMA: int = 75
MOMENTUM_WINDOW: int = 20
VOL_WINDOW: int = 20
RSI_WINDOW: int = 14
ATR_WINDOW: int = 14

# 方向スコアの重み（合計 1.0。トレンド追随を主、逆張り（平均回帰）を従とする素朴な既定）。
WEIGHT_TREND: float = 0.40
WEIGHT_MOMENTUM: float = 0.40
WEIGHT_MEANREV: float = 0.20

# score → 上昇確率のロジスティック変換の傾き。
LOGISTIC_K: float = 1.5
# 方向ラベルの閾値（上昇確率）。
PROB_UP_THRESHOLD: float = 0.55
PROB_DOWN_THRESHOLD: float = 0.45
# 「フラット」判定のリターン許容幅（採点時。±0.5%以内は横ばい的中とみなす）。
FLAT_BAND: float = 0.005

# make_forecast に最低限必要な終値の本数（SLOW_SMA を評価できる長さ + 余裕）。
MIN_HISTORY: int = SLOW_SMA + MOMENTUM_WINDOW

# 台帳の列（順序固定）。前半＝予想、後半＝採点結果。
LEDGER_COLUMNS: tuple[str, ...] = (
    "forecast_id",   # "<asof_date>:<code>"（同一営業日・同一銘柄は上書き）
    "made_on",       # 予想を生成した日（ローカル日付）
    "asof_date",     # 予想の基準となった最終終値の日付
    "target_date",   # 予想対象の翌営業日（暦日ベースの目安。採点は asof 超の最初の実データ日）
    "code",
    "name",
    "data",          # "real" / "synthetic"
    "direction",     # "up" / "down" / "flat"
    "prob_up",       # 上昇確率（0..1、Brier 用）
    "pred_return",   # 予想リターン（点推定）
    "asof_close",    # 基準終値
    "pred_low",      # 予想レンジ下限（価格）
    "pred_high",     # 予想レンジ上限（価格）
    "confidence",    # 0..1（|score|）
    "s_trend",       # トレンド分項（-1..1）
    "s_momentum",    # モメンタム分項（-1..1）
    "s_meanrev",     # 平均回帰分項（-1..1）
    "score",         # 合成方向スコア（-1..1）
    # --- 採点後に埋まる列 ---
    "status",        # "pending" / "graded"
    "graded_on",     # 採点した日
    "actual_date",   # 採点に使った実データ日（asof 超の最初の営業日）
    "actual_close",  # 実績終値
    "actual_return", # 実績リターン（actual_close / asof_close - 1）
    "dir_hit",       # 方向的中（bool）
    "in_range",      # 予想レンジ的中（bool）
    "abs_error",     # |actual_return - pred_return|
    "brier",         # (prob_up - 1{actual_return>0})^2
)

_DIRECTION_JP: dict[str, str] = {"up": "上昇", "down": "下落", "flat": "横ばい"}


class ForecastError(RuntimeError):
    """予想生成・採点・台帳操作の失敗を表す。"""


@dataclass(frozen=True)
class Forecast:
    """1銘柄・翌営業日の機械予想1件。"""

    code: str
    name: str
    asof_date: dt.date
    target_date: dt.date
    asof_close: float
    direction: str
    prob_up: float
    pred_return: float
    pred_low: float
    pred_high: float
    confidence: float
    s_trend: float
    s_momentum: float
    s_meanrev: float
    score: float
    data: str = "real"

    @property
    def forecast_id(self) -> str:
        return f"{self.asof_date.isoformat()}:{self.code}"

    @property
    def direction_jp(self) -> str:
        return _DIRECTION_JP.get(self.direction, self.direction)


@dataclass
class GradeResult:
    """採点結果1件。"""

    forecast_id: str
    actual_date: dt.date
    actual_close: float
    actual_return: float
    dir_hit: bool
    in_range: bool
    abs_error: float
    brier: float


# --------------------------------------------------------------------------
# 予想生成
# --------------------------------------------------------------------------

def _clip(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _next_business_day(day: dt.date) -> dt.date:
    """暦日ベースの翌営業日（土日をスキップ。祝日は考慮しない目安値）。"""
    nxt = day + dt.timedelta(days=1)
    while nxt.weekday() >= 5:  # 5=土, 6=日
        nxt += dt.timedelta(days=1)
    return nxt


def make_forecast(code: str, df: pd.DataFrame, *, name: str = "", data: str = "real") -> Forecast:
    """OHLCV DataFrame から翌営業日の機械予想を算出する。

    3つの正規化サブスコア（各 $[-1, 1]$）を固定重みで合成して方向スコア $s$ を作る:

    1. **トレンド** $s_{\\text{trend}} = \\tfrac12\\big(\\mathrm{sgn}(C_t - \\mathrm{SMA}^{25}_t)
       + \\mathrm{sgn}(\\mathrm{SMA}^{25}_t - \\mathrm{SMA}^{75}_t)\\big)$
       （終値の 25日線との上下 と 25/75日線の並び。2つの符号の平均で
       $\\{-1, -0.5, 0, 0.5, 1\\}$ を取る）。
    2. **モメンタム** $s_{\\text{mom}} = \\mathrm{clip}\\!\\big(m_{20} / (\\sigma_d\\sqrt{20}),\\ -1, 1\\big)$、
       $m_{20} = C_t / C_{t-20} - 1$、$\\sigma_d$ は日次リターンの20日標準偏差
       （20営業日モメンタムをその期間の標準偏差で正規化した z 値相当）。
    3. **平均回帰** $s_{\\text{rev}} = (50 - \\mathrm{RSI}_{14}) / 50$
       （売られすぎ RSI→翌日の反発方向に +、買われすぎ→ −。$[-1, 1]$）。

    合成: $s = \\mathrm{clip}(0.4\\,s_{\\text{trend}} + 0.4\\,s_{\\text{mom}} + 0.2\\,s_{\\text{rev}})$。
    上昇確率 $p = 1/(1+e^{-1.5 s})$。方向ラベルは $p \\ge 0.55$ で up、$p \\le 0.45$ で down、
    それ以外は flat。予想リターン $\\hat r = s \\cdot \\sigma_d$（1日ぶんの控えめな期待変化）。
    予想レンジは $C_t(1+\\hat r) \\pm \\mathrm{ATR}_{14}$。信頼度 $= |s|$。

    重み・傾き・閾値はいずれも**固定値で過去データにフィットしていない**。当たり外れは
    台帳に蓄積して :func:`summarize` で測定する（このモジュールの docstring 参照）。

    Args:
        code: 銘柄コード（表示・ID 用）。
        df: ``Open/High/Low/Close``（``atr`` のため High/Low 推奨）を持つ OHLCV。
            index は営業日 DatetimeIndex を想定（末尾を asof とする）。
        name: 表示名（任意）。
        data: ``"real"`` / ``"synthetic"``（台帳に記録し、採点時の突き合わせに使う）。

    Returns:
        :class:`Forecast`。

    Raises:
        ForecastError: 終値が :data:`MIN_HISTORY` 本に満たない、または Close 列が無い場合。
    """
    if "Close" not in df.columns:
        raise ForecastError("make_forecast には Close 列を持つ DataFrame を渡してください")
    close = df["Close"].dropna()
    if len(close) < MIN_HISTORY:
        raise ForecastError(
            f"{code}: 予想には終値が最低 {MIN_HISTORY} 本必要です（現在 {len(close)} 本）"
        )

    asof_close = float(close.iloc[-1])
    asof_date = _index_date(close.index[-1])
    target_date = _next_business_day(asof_date)

    # --- サブスコア ---
    sma_fast = float(indicators.sma(close, FAST_SMA).iloc[-1])
    sma_slow = float(indicators.sma(close, SLOW_SMA).iloc[-1])
    s_trend = 0.5 * (_sign(asof_close - sma_fast) + _sign(sma_fast - sma_slow))

    daily_ret = close.pct_change().dropna()
    sigma_d = float(daily_ret.iloc[-VOL_WINDOW:].std())
    if not math.isfinite(sigma_d) or sigma_d <= 0:
        sigma_d = float(daily_ret.std()) if len(daily_ret) > 1 else 0.0
    mom = asof_close / float(close.iloc[-1 - MOMENTUM_WINDOW]) - 1.0
    denom = sigma_d * math.sqrt(MOMENTUM_WINDOW)
    s_momentum = _clip(mom / denom) if denom > 0 else 0.0

    rsi_val = float(indicators.rsi(close, RSI_WINDOW).iloc[-1])
    s_meanrev = _clip((50.0 - rsi_val) / 50.0) if math.isfinite(rsi_val) else 0.0

    score = _clip(
        WEIGHT_TREND * s_trend + WEIGHT_MOMENTUM * s_momentum + WEIGHT_MEANREV * s_meanrev
    )
    prob_up = 1.0 / (1.0 + math.exp(-LOGISTIC_K * score))
    if prob_up >= PROB_UP_THRESHOLD:
        direction = "up"
    elif prob_up <= PROB_DOWN_THRESHOLD:
        direction = "down"
    else:
        direction = "flat"

    pred_return = score * sigma_d
    center = asof_close * (1.0 + pred_return)
    atr_val = _last_atr(df)
    band = atr_val if (math.isfinite(atr_val) and atr_val > 0) else asof_close * sigma_d
    pred_low = center - band
    pred_high = center + band

    return Forecast(
        code=code,
        name=name,
        asof_date=asof_date,
        target_date=target_date,
        asof_close=asof_close,
        direction=direction,
        prob_up=prob_up,
        pred_return=pred_return,
        pred_low=pred_low,
        pred_high=pred_high,
        confidence=abs(score),
        s_trend=s_trend,
        s_momentum=s_momentum,
        s_meanrev=s_meanrev,
        score=score,
        data=data,
    )


def _sign(x: float) -> float:
    if not math.isfinite(x) or x == 0:
        return 0.0
    return 1.0 if x > 0 else -1.0


def _last_atr(df: pd.DataFrame) -> float:
    if not {"High", "Low", "Close"}.issubset(df.columns):
        return float("nan")
    series = indicators.atr(df, ATR_WINDOW).dropna()
    return float(series.iloc[-1]) if len(series) else float("nan")


def _index_date(value: object) -> dt.date:
    """DatetimeIndex の要素を date に落とす。"""
    ts = pd.Timestamp(value)
    return ts.date()


# --------------------------------------------------------------------------
# 採点
# --------------------------------------------------------------------------

def grade_forecast(fc: Forecast, future_df: pd.DataFrame) -> GradeResult | None:
    """予想を後日の実データで採点する。

    ``future_df``（``fc.code`` の最新 OHLCV）から ``fc.asof_date`` **より後**の
    最初の営業日の終値を実績として採る。まだ存在しない（=市場が翌営業日の足を
    まだ作っていない）場合は ``None`` を返す（採点保留）。

    判定:
    - 方向的中 ``dir_hit``: up は実績リターン > 0、down は < 0、
      flat は $|r| \\le$ :data:`FLAT_BAND`。
    - レンジ的中 ``in_range``: ``pred_low <= actual_close <= pred_high``。
    - ``brier`` $= (p_{\\text{up}} - \\mathbf 1\\{r>0\\})^2$。
    - ``abs_error`` $= |r - \\hat r|$。
    """
    if "Close" not in future_df.columns:
        raise ForecastError("grade_forecast には Close 列を持つ DataFrame を渡してください")
    close = future_df["Close"].dropna().sort_index()  # 昇順を保証（未ソート入力での誤採用を防ぐ）
    if isinstance(close.index, pd.DatetimeIndex) and close.index.tz is not None:
        close.index = close.index.tz_localize(None)  # tz-aware 入力でも naive の asof と比較可能に
    asof_ts = pd.Timestamp(fc.asof_date)
    after = close[close.index > asof_ts]
    if len(after) == 0:
        return None  # 翌営業日の実データがまだ無い → 保留

    actual_date = _index_date(after.index[0])
    actual_close = float(after.iloc[0])
    if fc.asof_close <= 0:
        raise ForecastError(f"{fc.code}: asof_close が不正です（{fc.asof_close}）")
    actual_return = actual_close / fc.asof_close - 1.0

    if fc.direction == "up":
        dir_hit = actual_return > 0
    elif fc.direction == "down":
        dir_hit = actual_return < 0
    else:  # flat
        dir_hit = abs(actual_return) <= FLAT_BAND

    in_range = fc.pred_low <= actual_close <= fc.pred_high
    outcome_up = 1.0 if actual_return > 0 else 0.0
    brier = (fc.prob_up - outcome_up) ** 2
    abs_error = abs(actual_return - fc.pred_return)

    return GradeResult(
        forecast_id=fc.forecast_id,
        actual_date=actual_date,
        actual_close=actual_close,
        actual_return=actual_return,
        dir_hit=bool(dir_hit),
        in_range=bool(in_range),
        abs_error=abs_error,
        brier=brier,
    )


# --------------------------------------------------------------------------
# 台帳 I/O
# --------------------------------------------------------------------------

def load_ledger(path: Path = DEFAULT_LEDGER) -> pd.DataFrame:
    """台帳 CSV を読み込む（無ければ空の DataFrame）。"""
    if not path.exists():
        return pd.DataFrame(columns=list(LEDGER_COLUMNS))
    df = pd.read_csv(path, dtype={"code": str})
    # 将来列が増えても壊れないよう、既知列だけを既定順に整える（欠損列は補完）。
    for col in LEDGER_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    return df[list(LEDGER_COLUMNS)]


def _forecast_row(fc: Forecast, made_on: dt.date) -> dict[str, object]:
    return {
        "forecast_id": fc.forecast_id,
        "made_on": made_on.isoformat(),
        "asof_date": fc.asof_date.isoformat(),
        "target_date": fc.target_date.isoformat(),
        "code": fc.code,
        "name": fc.name,
        "data": fc.data,
        "direction": fc.direction,
        "prob_up": round(fc.prob_up, 6),
        "pred_return": round(fc.pred_return, 6),
        "asof_close": round(fc.asof_close, 4),
        "pred_low": round(fc.pred_low, 4),
        "pred_high": round(fc.pred_high, 4),
        "confidence": round(fc.confidence, 6),
        "s_trend": round(fc.s_trend, 6),
        "s_momentum": round(fc.s_momentum, 6),
        "s_meanrev": round(fc.s_meanrev, 6),
        "score": round(fc.score, 6),
        "status": "pending",
        "graded_on": pd.NA,
        "actual_date": pd.NA,
        "actual_close": pd.NA,
        "actual_return": pd.NA,
        "dir_hit": pd.NA,
        "in_range": pd.NA,
        "abs_error": pd.NA,
        "brier": pd.NA,
    }


def upsert_forecast(ledger: pd.DataFrame, fc: Forecast, made_on: dt.date) -> pd.DataFrame:
    """予想を台帳に追加する（同一 ``forecast_id`` があれば置き換え）。

    同じ営業日に複数回 forecast を回しても行が重複しないよう upsert する。
    既存行が採点済みでも、同一 asof の予想は上書きする（再生成の意図とみなす）。
    """
    row = _forecast_row(fc, made_on)
    ledger = ledger[ledger["forecast_id"] != fc.forecast_id]
    return pd.concat([ledger, pd.DataFrame([row])], ignore_index=True)


def apply_grade(ledger: pd.DataFrame, grade: GradeResult, graded_on: dt.date) -> pd.DataFrame:
    """採点結果を台帳の該当行に反映する（``status`` を graded に更新）。"""
    mask = ledger["forecast_id"] == grade.forecast_id
    if not mask.any():
        raise ForecastError(f"台帳に forecast_id={grade.forecast_id} がありません")
    updates = {
        "status": "graded",
        "graded_on": graded_on.isoformat(),
        "actual_date": grade.actual_date.isoformat(),
        "actual_close": round(grade.actual_close, 4),
        "actual_return": round(grade.actual_return, 6),
        "dir_hit": bool(grade.dir_hit),
        "in_range": bool(grade.in_range),
        "abs_error": round(grade.abs_error, 6),
        "brier": round(grade.brier, 6),
    }
    for col, val in updates.items():
        ledger.loc[mask, col] = val
    return ledger


def save_ledger(ledger: pd.DataFrame, path: Path = DEFAULT_LEDGER) -> Path:
    """台帳 CSV を保存する（列順を固定、asof_date→code で安定ソート）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = ledger.reindex(columns=list(LEDGER_COLUMNS))
    ordered = ordered.sort_values(["asof_date", "code"], kind="stable").reset_index(drop=True)
    ordered.to_csv(path, index=False)
    return path


def pending_rows(ledger: pd.DataFrame, *, data: str | None = None) -> pd.DataFrame:
    """採点保留（status=pending）の行を返す。``data`` 指定でソースを絞る。"""
    out = ledger[ledger["status"] == "pending"]
    if data is not None:
        out = out[out["data"].fillna("real") == data]
    return out


def row_to_forecast(row: Mapping[str, object]) -> Forecast:
    """台帳の1行（pending）を :class:`Forecast` に復元する（採点用）。"""
    return Forecast(
        code=str(row["code"]),
        name="" if pd.isna(row.get("name")) else str(row["name"]),
        asof_date=dt.date.fromisoformat(str(row["asof_date"])),
        target_date=dt.date.fromisoformat(str(row["target_date"])),
        asof_close=float(row["asof_close"]),
        direction=str(row["direction"]),
        prob_up=float(row["prob_up"]),
        pred_return=float(row["pred_return"]),
        pred_low=float(row["pred_low"]),
        pred_high=float(row["pred_high"]),
        confidence=float(row["confidence"]),
        s_trend=float(row["s_trend"]),
        s_momentum=float(row["s_momentum"]),
        s_meanrev=float(row["s_meanrev"]),
        score=float(row["score"]),
        data="real" if pd.isna(row.get("data")) else str(row["data"]),
    )


# --------------------------------------------------------------------------
# 集計・キャリブレーション
# --------------------------------------------------------------------------

@dataclass
class Summary:
    """採点済み予想の集計。"""

    n_graded: int
    n_directional: int  # flat を除いた方向予想の件数
    dir_hit_rate: float
    range_hit_rate: float
    mean_brier: float
    mean_abs_error: float
    baseline_brier: float  # 常に p=0.5 と予想した場合の Brier（0.25）
    per_direction: dict[str, tuple[int, float]] = field(default_factory=dict)


def _graded(ledger: pd.DataFrame, data: str | None = "real") -> pd.DataFrame:
    """採点済み（status=graded）行を返す。

    ``data`` を指定すると台帳の ``data`` 列（欠落は real 扱い）で絞る。既定 ``"real"`` は
    実データの track record を集計するため——合成（``--synthetic``）で書き込まれた採点行が
    同じ台帳に混ざっても、実データの的中率・Brier・較正に混入させない。``None`` で全件。
    """
    g = ledger[ledger["status"] == "graded"].copy()
    if data is not None and "data" in g.columns:
        g = g[g["data"].fillna("real") == data]
    for col in ("dir_hit", "in_range"):
        g[col] = g[col].map(_to_bool)
    for col in ("actual_return", "abs_error", "brier", "prob_up"):
        g[col] = pd.to_numeric(g[col], errors="coerce")
    return g


def _to_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "1.0", "yes"}


def summarize(ledger: pd.DataFrame, data: str | None = "real") -> Summary:
    """採点済み台帳の的中率・Brier・レンジ的中率などを集計する。

    ``data`` で集計対象を絞る（既定 ``"real"``。合成採点行を実データ成績に混ぜない）。

    Brier スコアは上昇確率の較正（calibration）の指標で、低いほど良い。
    無情報予想（常に $p=0.5$）の Brier は 0.25 で、これを下回れば予想が
    無情報より役立っていることを意味する（:data:`baseline_brier`）。
    """
    g = _graded(ledger, data)
    n = len(g)
    if n == 0:
        return Summary(0, 0, float("nan"), float("nan"), float("nan"), float("nan"), 0.25)

    directional = g[g["direction"] != "flat"]
    n_dir = len(directional)
    dir_hit_rate = float(directional["dir_hit"].mean()) if n_dir else float("nan")
    range_hit_rate = float(g["in_range"].mean())
    mean_brier = float(g["brier"].mean())
    mean_abs_error = float(g["abs_error"].mean())

    per_direction: dict[str, tuple[int, float]] = {}
    for label in ("up", "down", "flat"):
        sub = g[g["direction"] == label]
        if len(sub):
            per_direction[label] = (len(sub), float(sub["dir_hit"].mean()))

    return Summary(
        n_graded=n,
        n_directional=n_dir,
        dir_hit_rate=dir_hit_rate,
        range_hit_rate=range_hit_rate,
        mean_brier=mean_brier,
        mean_abs_error=mean_abs_error,
        baseline_brier=0.25,
        per_direction=per_direction,
    )


def calibration_table(
    ledger: pd.DataFrame, bins: Sequence[float] | None = None, data: str | None = "real"
) -> list[dict[str, object]]:
    """上昇確率のビンごとに「予想上昇確率」と「実際の上昇頻度」を集計する。

    較正が取れていれば、各ビンの予想確率の平均 ≒ 実際の上昇頻度になる。
    ``data`` で対象を絞る（既定 ``"real"``）。
    """
    g = _graded(ledger, data)
    if len(g) == 0:
        return []
    edges = list(bins) if bins is not None else [0.0, 0.4, 0.45, 0.5, 0.55, 0.6, 1.0001]
    g = g.dropna(subset=["prob_up"])
    g["_up"] = pd.to_numeric(g["actual_return"], errors="coerce") > 0
    rows: list[dict[str, object]] = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sub = g[(g["prob_up"] >= lo) & (g["prob_up"] < hi)]
        if len(sub) == 0:
            continue
        rows.append({
            "bucket": f"[{lo:.2f}, {hi:.2f})",
            "n": len(sub),
            "mean_prob_up": float(sub["prob_up"].mean()),
            "realized_up_freq": float(sub["_up"].mean()),
        })
    return rows


def per_code_hit_rate(ledger: pd.DataFrame, data: str | None = "real") -> list[dict[str, object]]:
    """銘柄ごとの方向的中率（flat 除く）と件数を返す（件数降順）。

    ``data`` で対象を絞る（既定 ``"real"``）。
    """
    g = _graded(ledger, data)
    directional = g[g["direction"] != "flat"]
    out: list[dict[str, object]] = []
    for code, sub in directional.groupby("code"):
        out.append({
            "code": str(code),
            "n": len(sub),
            "dir_hit_rate": float(sub["dir_hit"].mean()),
            "mean_brier": float(pd.to_numeric(sub["brier"], errors="coerce").mean()),
        })
    out.sort(key=lambda r: (-int(r["n"]), str(r["code"])))
    return out

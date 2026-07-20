#!/usr/bin/env bash
# M — Monitor（監視）。docs/mape-k.md。
# MAPE-K の主題は**株の解析の醸成**。本スクリプトは分析ドメインのシグナルを集めて
# $MAPE_STATE_DIR/monitor.env と monitor.md に書き出す（予測精度＋分析カバレッジ）。
# リポジトリ/システムの健全性は主題ではなく、pytest ゲートを**最小ガードレール**として測るのみ。
# 読み取り専用（--record を渡したときだけ mape/knowledge/HEALTH.md に1行追記する）。
#
# 使い方:
#   bash mape/monitor.sh              # 収集のみ（state/ に出力、HEALTH は触らない）
#   bash mape/monitor.sh --with-gate  # pytest（ガードレール）を実行して合否と所要秒も測る
#   bash mape/monitor.sh --with-gate --record   # HEALTH.md に推移を1行追記
#
# 注意: --with-gate は pytest を回す。pytest は analysis/tests/test_mape.py 経由で
#       この monitor を（run.sh 越しに）呼ぶため、テストからは --with-gate を使わない
#       （MAPE_NO_GATE=1 で無限再帰を防ぐ）。
set -u
# shellcheck source=lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

with_gate=0; record=0
for arg in "$@"; do
  case "$arg" in
    --with-gate) with_gate=1 ;;
    --record)    record=1 ;;
    *) mape_die "未知の引数: $arg" ;;
  esac
done

cd "$MAPE_ROOT" || mape_die "cd 失敗"
mape_ensure_state

# --- 分析ドメインのシグナル（予測精度＋分析カバレッジ）。読み取り専用・ネットワーク不使用。 ---
sig="$MAPE_STATE_DIR/analysis-signals.env"
today=$(date -u +%Y-%m-%d)
if [ -f mape/analysis_signals.py ] && python3 mape/analysis_signals.py "$MAPE_ROOT" "$today" "$MAPE_STATE_DIR" >"$sig" 2>/dev/null; then
  # shellcheck source=/dev/null
  . "$sig"
else
  : > "$sig"
fi
# 既定値（台帳/ジャーナル/ユニバースが無くても壊れない）
: "${MAPE_FC_GRADED:=0}"; : "${MAPE_FC_PENDING:=0}"; : "${MAPE_FC_HIT:=na}"; : "${MAPE_FC_BRIER:=na}"; : "${MAPE_FC_INRANGE:=na}"
: "${MAPE_JR_TOTAL:=0}"; : "${MAPE_JR_VERIFIED:=0}"; : "${MAPE_JR_HIT:=0}"; : "${MAPE_JR_DUE:=0}"
: "${MAPE_UNIVERSE:=0}"; : "${MAPE_COVERED:=0}"; : "${MAPE_COVERAGE:=na}"; : "${MAPE_UNANALYZED:=0}"
: "${MAPE_KNOW_DOCS:=0}"; : "${MAPE_STALE_DOCS:=0}"

# --- ガードレール（分析コードが動くことの担保。主題ではない）: pytest ---
# 再帰ガード: pytest 経由（= MAPE_NO_GATE=1）では --with-gate を無視する。
gate="skip"; gate_s="-"
if [ "$with_gate" -eq 1 ] && [ "${MAPE_NO_GATE:-0}" != "1" ]; then
  start=$(date +%s)
  if $MAPE_GATE_CMD >"$MAPE_STATE_DIR/gate.log" 2>&1; then gate="pass"; else gate="fail"; fi
  gate_s=$(( $(date +%s) - start ))
fi

ts=$(mape_now)

# サイクル番号: HEALTH の既存データ行数 + 1
cycle=1
if [ -f "$MAPE_HEALTH" ]; then
  existing=$(grep -cE '^\| [0-9]{4}-' "$MAPE_HEALTH" 2>/dev/null || true)
  existing=${existing:-0}
  cycle=$(( existing + 1 ))
fi

# --- 出力: monitor.env（sourceable） ---
{
  echo "MAPE_TS=$ts"
  echo "MAPE_CYCLE=$cycle"
  echo "MAPE_GATE=$gate"
  echo "MAPE_GATE_S=$gate_s"
  echo "MAPE_FC_GRADED=$MAPE_FC_GRADED"
  echo "MAPE_FC_PENDING=$MAPE_FC_PENDING"
  echo "MAPE_FC_HIT=$MAPE_FC_HIT"
  echo "MAPE_FC_BRIER=$MAPE_FC_BRIER"
  echo "MAPE_FC_INRANGE=$MAPE_FC_INRANGE"
  echo "MAPE_JR_TOTAL=$MAPE_JR_TOTAL"
  echo "MAPE_JR_VERIFIED=$MAPE_JR_VERIFIED"
  echo "MAPE_JR_HIT=$MAPE_JR_HIT"
  echo "MAPE_JR_DUE=$MAPE_JR_DUE"
  echo "MAPE_UNIVERSE=$MAPE_UNIVERSE"
  echo "MAPE_COVERED=$MAPE_COVERED"
  echo "MAPE_COVERAGE=$MAPE_COVERAGE"
  echo "MAPE_UNANALYZED=$MAPE_UNANALYZED"
  echo "MAPE_KNOW_DOCS=$MAPE_KNOW_DOCS"
  echo "MAPE_STALE_DOCS=$MAPE_STALE_DOCS"
} > "$MAPE_STATE_DIR/monitor.env"

# --- 出力: monitor.md（人が読む要約） ---
{
  echo "# Monitor レポート — $ts (cycle $cycle)"
  echo
  echo "## 🎯 予測精度（分析が当たっていたか）"
  echo
  echo "| 指標 | 値 |"
  echo "|---|---|"
  echo "| 予想 採点済み | $MAPE_FC_GRADED 件 |"
  echo "| 予想 方向的中率 | ${MAPE_FC_HIT}% |"
  echo "| 予想 平均Brier | $MAPE_FC_BRIER |"
  echo "| 予想 レンジ的中率 | ${MAPE_FC_INRANGE}% |"
  echo "| 予想 未採点 | $MAPE_FC_PENDING 件 |"
  echo "| ジャーナル 検証済み/総数 | $MAPE_JR_VERIFIED / $MAPE_JR_TOTAL |"
  echo "| ジャーナル 的中 | $MAPE_JR_HIT 件 |"
  echo "| ジャーナル 検証期日超過 | $MAPE_JR_DUE 件 |"
  echo
  echo "## 🗺️ 分析カバレッジ（分析資産をどれだけ広げ・新鮮に保てているか）"
  echo
  echo "| 指標 | 値 |"
  echo "|---|---|"
  echo "| ユニバース網羅 | $MAPE_COVERED / $MAPE_UNIVERSE（${MAPE_COVERAGE}%） |"
  echo "| 未分析銘柄 | $MAPE_UNANALYZED 件 |"
  echo "| ナレッジ文書 | $MAPE_KNOW_DOCS 件 |"
  echo "| 陳腐化文書（〜年時点が古い） | $MAPE_STALE_DOCS 件 |"
  echo
  echo "## 🛡️ ガードレール（主題ではない）"
  echo
  echo "- pytest: $gate ($gate_s s)  ← 分析コードが動くことの担保のみ"
} > "$MAPE_STATE_DIR/monitor.md"

# --- HEALTH.md へ追記（--record のときだけ） ---
row="| $ts | $cycle | $gate | $MAPE_FC_GRADED | $MAPE_FC_HIT | $MAPE_FC_BRIER | $MAPE_FC_PENDING | $MAPE_JR_VERIFIED | $MAPE_JR_HIT | $MAPE_JR_DUE | $MAPE_COVERAGE | $MAPE_UNANALYZED | $MAPE_KNOW_DOCS | $MAPE_STALE_DOCS | monitor |"
if [ "$record" -eq 1 ] && [ -f "$MAPE_HEALTH" ]; then
  printf '%s\n' "$row" >> "$MAPE_HEALTH"
  mape_log "HEALTH.md に cycle $cycle を追記"
fi

echo "$row"
mape_log "monitor 完了 → $MAPE_STATE_DIR/monitor.env"

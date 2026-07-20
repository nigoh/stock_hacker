#!/usr/bin/env bash
# MAPE-K の M→A→P を1周まわす統合ランナー。docs/mape-k.md。
# 読み取り中心（安価・安全）。夜通し何度でも回してよい。実装（Execute）は含まない。
#
# 使い方:
#   bash mape/run.sh            # M→A→P を回し、state/ に成果物を出す（mape/knowledge は触らない = ドライラン）
#   bash mape/run.sh --record   # HEALTH.md / BACKLOG.md / PROGRESS.md も更新する（本番の夜間周回）
#
# 出力の要: $MAPE_STATE_DIR/issue-body.md（Plan が作る掲示用チェックリスト）
set -u
# shellcheck source=lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

record=0
for arg in "$@"; do
  case "$arg" in
    --record) record=1 ;;
    *) mape_die "未知の引数: $arg" ;;
  esac
done

cd "$MAPE_ROOT" || mape_die "cd 失敗"

mon_args=(--with-gate); ana_args=()
if [ "$record" -eq 1 ]; then mon_args+=(--record); ana_args+=(--update-knowledge); fi

mape_log "M: monitor…"
bash mape/monitor.sh "${mon_args[@]}" >/dev/null

mape_log "A: analyze…"
bash mape/analyze.sh "${ana_args[@]}" >/dev/null

mape_log "P: plan…"
bash mape/plan.sh >/dev/null

# --record のときは PROGRESS に monitor サイクルを1件、末尾追記する
if [ "$record" -eq 1 ] && [ -f "$MAPE_PROGRESS" ]; then
  # shellcheck source=/dev/null
  . "$MAPE_STATE_DIR/monitor.env"
  {
    echo
    echo "## ${MAPE_TS} — monitor (cycle ${MAPE_CYCLE})"
    echo "- 対象: 株の解析の醸成シグナル観測（予測精度＋分析カバレッジ）"
    echo "- やったこと: M→A→P を実行し計画イシュー本文を生成"
    echo "- 結果: 方向的中率=${MAPE_FC_HIT}% / 網羅=${MAPE_COVERAGE}%（未分析${MAPE_UNANALYZED}）/ 陳腐化${MAPE_STALE_DOCS} / 提案 $(wc -l < "$MAPE_STATE_DIR/proposals.tsv" | tr -d ' ') 件（gate=${MAPE_GATE}）"
    echo "- 考察: HEALTH.md に cycle ${MAPE_CYCLE} を記録。推移は同ファイル参照"
    echo "- 次に必要になった作業: Execute がチェック済み項目を1件消化 / 答え合わせ（/overnight・/journal-review）で track record を醸成"
  } >> "$MAPE_PROGRESS"
  mape_log "PROGRESS.md に monitor サイクルを追記"
fi

echo "$MAPE_STATE_DIR/issue-body.md"
mape_log "M→A→P 完了。掲示用本文: $MAPE_STATE_DIR/issue-body.md"

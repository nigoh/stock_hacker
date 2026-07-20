#!/usr/bin/env bash
# A — Analyze（分析）。docs/mape-k.md。
# monitor.env の生シグナルを「症状」に変換し、根拠とインパクト×労力スコア付きの改善案を作る。
# POLICY.md の却下ログでフィルタし、リスク分類（consult 危険側優先）を付ける。
# 読み取り専用（--update-knowledge を渡したときだけ BACKLOG.md に新候補を追記する）。
#
# 出力: $MAPE_STATE_DIR/proposals.tsv（tier\tpriority\timpact\teffort\tscore\ttext）
#       $MAPE_STATE_DIR/analysis.md（人が読む症状＋提案）
set -u
# shellcheck source=lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

update_knowledge=0
for arg in "$@"; do
  case "$arg" in
    --update-knowledge) update_knowledge=1 ;;
    *) mape_die "未知の引数: $arg" ;;
  esac
done

cd "$MAPE_ROOT" || mape_die "cd 失敗"
mape_ensure_state

env_file="$MAPE_STATE_DIR/monitor.env"
[ -f "$env_file" ] || mape_die "monitor.env が無い。先に mape/monitor.sh を実行する"
# shellcheck source=/dev/null
. "$env_file"

raw="$MAPE_STATE_DIR/proposals.raw"
: > "$raw"
skipped="$MAPE_STATE_DIR/analysis.skipped"
: > "$skipped"

# 提案を1件追加する。却下ログにマッチしたら skip。consult キーワードは分類を危険側へ上書き。
emit() {
  local tier="$1" prio="$2" impact="$3" effort="$4" text="$5" score
  if mape_is_rejected "$text"; then
    printf '%s\n' "$text" >> "$skipped"; return 0
  fi
  # danger-first: 本文に consult キーワードがあれば分類を consult へ引き上げる
  if [ "$(mape_classify "$text")" = "consult" ]; then tier="consult"; fi
  score=$(mape_score "$impact" "$effort")
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$tier" "$prio" "$impact" "$effort" "$score" "$text" >> "$raw"
}

# --- 1. シグナル由来の提案（実測値を根拠にする） ---
if [ "${MAPE_GATE:-skip}" = "fail" ]; then
  emit auto P1 5 2 "pytest（analysis/tests）の赤を直す — 根拠: gate=fail（最優先。緑化するまで他を止める）"
fi
if [ "${MAPE_INDEX:-skip}" = "ng" ]; then
  emit auto P1 4 2 "knowledge 索引の不整合を直す — 根拠: check_knowledge_index.py --all=ng（未索引/リンク切れ/文書数ずれ）/ 高×小"
fi
if [ "${MAPE_TODO:-0}" -gt 0 ] 2>/dev/null; then
  emit auto P2 3 2 "TODO/FIXME を解消する（${MAPE_TODO} 件）— 根拠: 未完了マーカーが残存 / インパクト中×労力小"
fi
if [ "${MAPE_UNTESTED:-0}" -gt 0 ] 2>/dev/null; then
  mods=$(paste -sd, "$MAPE_STATE_DIR/untested-modules.txt" 2>/dev/null)
  emit auto P2 4 3 "テストの無い stocklib モジュール ${MAPE_UNTESTED} 件にテストを追加する（${mods}）— 根拠: 回帰の穴 / カバレッジ強化"
fi
if [ "${MAPE_MAX_SKILL:-0}" -ge 180 ] 2>/dev/null; then
  emit approve P2 3 3 "最長 SKILL.md を分割する（${MAPE_MAX_SKILL}/200 行）— 根拠: 200行予算に接近 / progressive disclosure"
fi
if [ -n "${MAPE_CHURN_TOP:-}" ] && [ "${MAPE_CHURN_TOP}" != "-" ]; then
  emit approve P3 3 3 "変更集中箇所 ${MAPE_CHURN_TOP} のテスト強化/整理を検討 — 根拠: 直近30コミットの churn 首位 / 回帰リスク"
fi

# --- 1'. 分析の答え合わせ（track record）由来の提案 ---
# (a) 運用: 未採点・検証期日超過を「答え合わせを回す」auto 項目として surface する（ループの駆動）。
if [ "${MAPE_FC_PENDING:-0}" -gt 0 ] 2>/dev/null; then
  emit auto P2 3 1 "夜間フォーキャストの答え合わせ（grade）が未処理 ${MAPE_FC_PENDING} 件 — 根拠: 台帳に pending / \`python3 analysis/overnight_forecast.py run\` で採点し track record を醸成（実データが無ければskip）"
fi
if [ "${MAPE_JR_DUE:-0}" -gt 0 ] 2>/dev/null; then
  emit auto P2 3 1 "検証期日が来たリサーチジャーナル仮説 ${MAPE_JR_DUE} 件の答え合わせ — 根拠: review_date 超過 / \`/journal-review\` で hit/miss を機械判定（実データが無ければskip）"
fi
# (b) 手法改善: 標本が十分たまり、かつ track record が弱いときだけ手法見直しを提案する
#     （少数標本での過剰反応を避ける＝knowledge/math/forecast-evaluation-and-calibration.md の統計的懐疑）。
FC_MIN="${MAPE_FC_MIN_SAMPLE:-20}"; JR_MIN="${MAPE_JR_MIN_SAMPLE:-5}"
if [ "${MAPE_FC_GRADED:-0}" -ge "$FC_MIN" ] 2>/dev/null && [ "${MAPE_FC_HIT:-na}" != "na" ]; then
  if [ "${MAPE_FC_HIT}" -lt 50 ] 2>/dev/null; then
    emit approve P2 4 3 "予想モデルの方向的中率が ${MAPE_FC_HIT}%（標本 ${MAPE_FC_GRADED}, <50%）— 根拠: stocklib.forecast の固定重み合成が効いていない / 重み見直し＋回帰テスト追加"
  fi
fi
if [ "${MAPE_FC_GRADED:-0}" -ge "$FC_MIN" ] 2>/dev/null && [ "${MAPE_FC_BRIER:-na}" != "na" ]; then
  if awk "BEGIN{exit !(${MAPE_FC_BRIER} > 0.25)}" 2>/dev/null; then
    emit approve P3 3 3 "予想が較正不良（平均Brier=${MAPE_FC_BRIER}, >0.25, 標本 ${MAPE_FC_GRADED}）— 根拠: prob_up が過信/鈍感 / 上昇確率の算出・較正を見直す"
  fi
fi
if [ "${MAPE_JR_VERIFIED:-0}" -ge "$JR_MIN" ] 2>/dev/null; then
  jr_pct=$(awk "BEGIN{printf \"%d\", 100*${MAPE_JR_HIT:-0}/${MAPE_JR_VERIFIED}}" 2>/dev/null)
  if [ -n "$jr_pct" ] && [ "$jr_pct" -lt 40 ] 2>/dev/null; then
    emit approve P3 3 3 "ジャーナル仮説の的中が低い（${MAPE_JR_HIT}/${MAPE_JR_VERIFIED} = ${jr_pct}%）— 根拠: 分析観点・反証条件の甘さ / behavioral-finance-japan.md の枠組みで観点を見直す"
  fi
fi

# --- 2. BACKLOG.md「## 候補」の未チェック項目を取り込む ---
if [ -f "$MAPE_BACKLOG" ]; then
  while IFS= read -r line; do
    prio=$(printf '%s' "$line" | grep -oE '\(P[0-9]' | tr -d '(')
    tier=$(printf '%s' "$line" | grep -oE ', [a-z]+\)' | sed -E 's/^, //; s/\)$//')
    text=$(printf '%s' "$line" | sed -E 's/^- \[[ xX]\] \(P[0-9], [a-z]+\)[[:space:]]*//')
    [ -z "$prio" ] && prio=P3
    [ -z "$tier" ] && tier=approve
    [ -z "$text" ] && continue
    case "$prio" in P1) impact=5;; P2) impact=3;; *) impact=2;; esac
    emit "$tier" "$prio" "$impact" 3 "$text"
  done < <(grep -E '^- \[ \] \(P[0-9], [a-z]+\)' "$MAPE_BACKLOG" 2>/dev/null)
fi

# --- 3. スコア降順で proposals.tsv を確定 ---
sort -t$'\t' -k5,5nr "$raw" > "$MAPE_STATE_DIR/proposals.tsv"
n=$(wc -l < "$MAPE_STATE_DIR/proposals.tsv" | tr -d ' ')
nskip=$(grep -c . "$skipped" 2>/dev/null || true); nskip=${nskip:-0}

# --- 4. analysis.md（人が読む） ---
{
  echo "# Analyze レポート — ${MAPE_TS:-?} (cycle ${MAPE_CYCLE:-?})"
  echo
  echo "## 症状（Monitor シグナルの解釈）"
  echo
  echo "- 品質ゲート(pytest): ${MAPE_GATE:-skip}（${MAPE_GATE_S:-?} s）"
  echo "- knowledge 索引整合: ${MAPE_INDEX:-skip}"
  echo "- 未完了マーカー TODO/FIXME: ${MAPE_TODO:-?} 件"
  echo "- テストの無い stocklib モジュール: ${MAPE_UNTESTED:-?} 件"
  echo "- 最長 SKILL.md: ${MAPE_MAX_SKILL:-?}/200 行"
  echo "- churn 首位: ${MAPE_CHURN_TOP:-?}"
  echo "- 【分析の答え合わせ】予想: 採点済み ${MAPE_FC_GRADED:-?} 件 / 方向的中率 ${MAPE_FC_HIT:-?}% / Brier ${MAPE_FC_BRIER:-?} / 未採点 ${MAPE_FC_PENDING:-?} 件"
  echo "- 【分析の答え合わせ】ジャーナル: 検証済み ${MAPE_JR_VERIFIED:-?}/${MAPE_JR_TOTAL:-?} / 的中 ${MAPE_JR_HIT:-?} / 検証期日超過 ${MAPE_JR_DUE:-?} 件"
  echo
  echo "## 改善案（スコア = インパクト×(6-労力)、降順）"
  echo
  echo "| # | tier | prio | impact | effort | score | 内容（根拠つき） |"
  echo "|---|---|---|---|---|---|---|"
  i=0
  while IFS=$'\t' read -r tier prio impact effort score text; do
    i=$((i+1))
    echo "| $i | $tier | $prio | $impact | $effort | $score | $text |"
  done < "$MAPE_STATE_DIR/proposals.tsv"
  if [ "$nskip" -gt 0 ]; then
    echo
    echo "## 却下ログにより除外（POLICY.md）"
    echo
    while IFS= read -r t; do [ -n "$t" ] && echo "- $t"; done < "$skipped"
  fi
} > "$MAPE_STATE_DIR/analysis.md"

# --- 5. 新候補を BACKLOG へ（--update-knowledge のときだけ・重複回避） ---
if [ "$update_knowledge" -eq 1 ] && [ -f "$MAPE_BACKLOG" ]; then
  added=0
  while IFS=$'\t' read -r tier prio impact effort score text; do
    # 既に BACKLOG 本文に含まれていれば追記しない（テキスト先頭40文字で判定）
    key=$(printf '%s' "$text" | cut -c1-40)
    if ! grep -qF "$key" "$MAPE_BACKLOG"; then
      # 「## アーカイブ」より前（＝候補セクション末尾）に挿入
      tmp=$(mktemp)
      awk -v ins="- [ ] ($prio, $tier) $text" '
        /^## アーカイブ/ && !done { print ins; print ""; done=1 }
        { print }
      ' "$MAPE_BACKLOG" > "$tmp" && mv "$tmp" "$MAPE_BACKLOG"
      added=$((added+1))
    fi
  done < "$MAPE_STATE_DIR/proposals.tsv"
  mape_log "BACKLOG.md に新候補 $added 件を追記"
fi

mape_log "analyze 完了 → 提案 $n 件 / 除外 $nskip 件 → $MAPE_STATE_DIR/proposals.tsv"

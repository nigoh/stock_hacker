#!/usr/bin/env bash
# A — Analyze（分析）。docs/mape-k.md。
# MAPE-K の主題は**株の解析の醸成**。monitor.env の分析シグナル（予測精度＋分析カバレッジ）を
# 「症状」に変換し、根拠とインパクト×労力スコア付きの改善案を作る。
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

emit() {
  local tier="$1" prio="$2" impact="$3" effort="$4" text="$5" score
  if mape_is_rejected "$text"; then
    printf '%s\n' "$text" >> "$skipped"; return 0
  fi
  if [ "$(mape_classify "$text")" = "consult" ]; then tier="consult"; fi
  score=$(mape_score "$impact" "$effort")
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$tier" "$prio" "$impact" "$effort" "$score" "$text" >> "$raw"
}

# サンプル抽出（未分析銘柄・陳腐化文書の先頭数件を例示に使う）
sample() { head -"${2:-5}" "$MAPE_STATE_DIR/$1" 2>/dev/null | paste -sd, - ; }

FC_MIN="${MAPE_FC_MIN_SAMPLE:-20}"; JR_MIN="${MAPE_JR_MIN_SAMPLE:-5}"

# --- 0. ガードレール（主題ではないが、分析コードが壊れていたら最優先で直す） ---
if [ "${MAPE_GATE:-skip}" = "fail" ]; then
  emit auto P1 5 2 "pytest（分析コードのガードレール）の赤を直す — 根拠: gate=fail（緑化まで分析が回らない。最優先）"
fi

# --- 1. 予測精度の醸成（answer-checking → 手法改善） ---
# (a) 運用: 答え合わせを回す（未採点・検証期日超過）。ループの駆動。
if [ "${MAPE_FC_PENDING:-0}" -gt 0 ] 2>/dev/null; then
  emit auto P2 4 1 "夜間フォーキャストの答え合わせ（grade）が未処理 ${MAPE_FC_PENDING} 件 — 根拠: 台帳に pending / \`python3 analysis/overnight_forecast.py run\` で採点し track record を醸成（実データが無ければskip）"
fi
if [ "${MAPE_JR_DUE:-0}" -gt 0 ] 2>/dev/null; then
  emit auto P2 4 1 "検証期日が来たリサーチジャーナル仮説 ${MAPE_JR_DUE} 件の答え合わせ — 根拠: review_date 超過 / \`/journal-review\` で hit/miss を機械判定（実データが無ければskip）"
fi
# (b) 手法改善: 標本が十分たまり、かつ精度が弱いときだけ（少数標本での過剰反応を避ける）。
if [ "${MAPE_FC_GRADED:-0}" -ge "$FC_MIN" ] 2>/dev/null && [ "${MAPE_FC_HIT:-na}" != "na" ] && [ "${MAPE_FC_HIT}" -lt 50 ] 2>/dev/null; then
  emit approve P2 4 3 "予想モデルの方向的中率が ${MAPE_FC_HIT}%（標本 ${MAPE_FC_GRADED}, <50%）— 根拠: stocklib.forecast の固定重み合成が効いていない / 重み見直し＋回帰テスト"
fi
if [ "${MAPE_FC_GRADED:-0}" -ge "$FC_MIN" ] 2>/dev/null && [ "${MAPE_FC_BRIER:-na}" != "na" ] && awk "BEGIN{exit !(${MAPE_FC_BRIER} > 0.25)}" 2>/dev/null; then
  emit approve P3 3 3 "予想が較正不良（平均Brier=${MAPE_FC_BRIER}, >0.25, 標本 ${MAPE_FC_GRADED}）— 根拠: prob_up が過信/鈍感 / 上昇確率の算出・較正を見直す"
fi
if [ "${MAPE_FC_GRADED:-0}" -ge "$FC_MIN" ] 2>/dev/null && [ "${MAPE_FC_INRANGE:-na}" != "na" ] && [ "${MAPE_FC_INRANGE}" -lt 60 ] 2>/dev/null; then
  emit approve P3 3 3 "予想レンジの的中が低い（${MAPE_FC_INRANGE}%, 標本 ${MAPE_FC_GRADED}）— 根拠: ATR ベースのレンジ幅が実勢と乖離 / レンジ算出の見直し"
fi
if [ "${MAPE_JR_VERIFIED:-0}" -ge "$JR_MIN" ] 2>/dev/null; then
  jr_pct=$(awk "BEGIN{printf \"%d\", 100*${MAPE_JR_HIT:-0}/${MAPE_JR_VERIFIED}}" 2>/dev/null)
  if [ -n "$jr_pct" ] && [ "$jr_pct" -lt 40 ] 2>/dev/null; then
    emit approve P3 3 3 "ジャーナル仮説の的中が低い（${MAPE_JR_HIT}/${MAPE_JR_VERIFIED} = ${jr_pct}%）— 根拠: 分析観点・反証条件の甘さ / behavioral-finance-japan.md の枠組みで観点を見直す"
  fi
fi

# --- 2. 分析カバレッジの醸成（分析資産を広げ・新鮮に保つ） ---
if [ "${MAPE_UNANALYZED:-0}" -gt 0 ] 2>/dev/null; then
  ex=$(sample unanalyzed-codes.txt 5)
  emit auto P2 4 2 "未分析銘柄 ${MAPE_UNANALYZED} 件をユニバースへ醸成する（例: ${ex}）— 根拠: 網羅率 ${MAPE_COVERAGE:-na}%（${MAPE_COVERED:-0}/${MAPE_UNIVERSE:-0}）/ \`python3 analysis/overnight_forecast.py run\` をユニバース全体で回すと台帳に記録され網羅が上がる"
fi
if [ "${MAPE_STALE_DOCS:-0}" -gt 0 ] 2>/dev/null; then
  ex=$(sample stale-docs.txt 3)
  emit approve P3 3 3 "陳腐化ナレッジ ${MAPE_STALE_DOCS} 件を更新する（例: ${ex}）— 根拠: 「〜年時点」の最新が2年以上前 / \`/learn\` で数値・制度を最新化し分析の土台を新鮮に保つ"
fi

# --- 3. BACKLOG.md「## 候補」の未チェック項目を取り込む ---
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

# --- 4. 重複排除（同一テキスト）＋スコア降順で proposals.tsv を確定 ---
sort -t$'\t' -k5,5nr "$raw" | awk -F'\t' '!seen[$6]++' > "$MAPE_STATE_DIR/proposals.tsv"
n=$(wc -l < "$MAPE_STATE_DIR/proposals.tsv" | tr -d ' ')
nskip=$(grep -c . "$skipped" 2>/dev/null || true); nskip=${nskip:-0}

# --- 5. analysis.md（人が読む） ---
{
  echo "# Analyze レポート — ${MAPE_TS:-?} (cycle ${MAPE_CYCLE:-?})"
  echo
  echo "## 症状（分析ドメインの解釈）"
  echo
  echo "### 🎯 予測精度"
  echo "- 予想: 採点済み ${MAPE_FC_GRADED:-?} 件 / 方向的中率 ${MAPE_FC_HIT:-?}% / Brier ${MAPE_FC_BRIER:-?} / レンジ的中 ${MAPE_FC_INRANGE:-?}% / 未採点 ${MAPE_FC_PENDING:-?} 件"
  echo "- ジャーナル: 検証済み ${MAPE_JR_VERIFIED:-?}/${MAPE_JR_TOTAL:-?} / 的中 ${MAPE_JR_HIT:-?} / 検証期日超過 ${MAPE_JR_DUE:-?} 件"
  echo "### 🗺️ 分析カバレッジ"
  echo "- ユニバース網羅: ${MAPE_COVERED:-?}/${MAPE_UNIVERSE:-?}（${MAPE_COVERAGE:-?}%）/ 未分析 ${MAPE_UNANALYZED:-?} 件"
  echo "- ナレッジ: ${MAPE_KNOW_DOCS:-?} 文書 / 陳腐化 ${MAPE_STALE_DOCS:-?} 件"
  echo "### 🛡️ ガードレール（主題外）: pytest=${MAPE_GATE:-skip}"
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

# --- 6. 新候補を BACKLOG へ（--update-knowledge のときだけ・重複回避） ---
if [ "$update_knowledge" -eq 1 ] && [ -f "$MAPE_BACKLOG" ]; then
  added=0
  while IFS=$'\t' read -r tier prio impact effort score text; do
    key=$(printf '%s' "$text" | cut -c1-40)
    if ! grep -qF "$key" "$MAPE_BACKLOG"; then
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

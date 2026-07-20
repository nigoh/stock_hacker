#!/usr/bin/env bash
# P — Plan（計画）。docs/mape-k.md。
# proposals.tsv を読み、リスク3分類（自動/承認/相談）のチェックリストとして
# GitHub イシュー本文（$MAPE_STATE_DIR/issue-body.md）を生成する。投稿はスキル側が MCP で行う。
# 完了項目は実行台帳（ledger.jsonl）の green から <details> に畳み、板をスリムに保つ。
set -u
# shellcheck source=lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

cd "$MAPE_ROOT" || mape_die "cd 失敗"
mape_ensure_state

tsv="$MAPE_STATE_DIR/proposals.tsv"
[ -f "$tsv" ] || mape_die "proposals.tsv が無い。先に mape/analyze.sh を実行する"
env_file="$MAPE_STATE_DIR/monitor.env"
# shellcheck source=/dev/null
[ -f "$env_file" ] && . "$env_file"

out="$MAPE_STATE_DIR/issue-body.md"

# 指定 tier の項目をチェックリスト行として出力
emit_section() {
  local want="$1" checkbox="$2" tier prio impact effort score text found=0
  while IFS=$'\t' read -r tier prio impact effort score text; do
    [ "$tier" = "$want" ] || continue
    echo "- [$checkbox] ($prio, score $score) $text"
    found=1
  done < "$tsv"
  [ "$found" -eq 0 ] && echo "- （なし）"
}

# 実行台帳（ledger.jsonl）の green を「完了ログ」の行として出力する（板をスリムに保つ）。
emit_done_log() {
  local ledger="$MAPE_STATE_DIR/ledger.jsonl"
  [ -f "$ledger" ] || return 0
  if command -v jq >/dev/null 2>&1; then
    jq -r 'select(.result=="green") | "- \(.item) → PR #\(.pr)（\(.ts)）"' "$ledger" 2>/dev/null
  else
    python3 - "$ledger" <<'PY' 2>/dev/null
import json,sys
for ln in open(sys.argv[1],encoding="utf-8"):
    ln=ln.strip()
    if not ln: continue
    try: r=json.loads(ln)
    except Exception: continue
    if r.get("result")=="green":
        print(f"- {r.get('item')} → PR #{r.get('pr')}（{r.get('ts')}）")
PY
  fi
}
done_log=$(emit_done_log)
done_n=$(printf '%s' "$done_log" | grep -c . 2>/dev/null || true); done_n=${done_n:-0}

{
  echo "# 🌙 MAPE-K 夜間改善レポート — ${MAPE_TS:-?} (cycle ${MAPE_CYCLE:-?})"
  echo
  echo "> 自動生成（\`mape/plan.sh\`, docs/mape-k.md）。夜間に Monitor→Analyze→Plan を回した結果です。"
  echo "> **あなたはチェックを入れるだけ**。Execute が「チェック済み・未着手」を1周1件だけ安全に実装します。"
  echo
  echo "健全性: gate=${MAPE_GATE:-skip}(${MAPE_GATE_S:-?}s) / 索引=${MAPE_INDEX:-skip} / TODO=${MAPE_TODO:-?} / 未テスト module=${MAPE_UNTESTED:-?} / 最長SKILL=${MAPE_MAX_SKILL:-?}/200 / ナレッジ=${MAPE_KNOW_DOCS:-?}文書"
  echo
  echo "## 📊 分析の答え合わせ（track record）"
  echo
  echo "分析が当たっていたかの継続測定。**手法改善の起点**（弱ければ下のチェックリストに手法見直しが並ぶ）。推移は \`mape/knowledge/HEALTH.md\`。"
  echo
  echo "| 対象 | 実績 |"
  echo "|---|---|"
  echo "| 夜間フォーキャスト | 採点済み ${MAPE_FC_GRADED:-?} 件 / 方向的中率 **${MAPE_FC_HIT:-?}%** / 平均Brier ${MAPE_FC_BRIER:-?} / 未採点 ${MAPE_FC_PENDING:-?} 件 |"
  echo "| リサーチジャーナル | 検証済み ${MAPE_JR_VERIFIED:-?}/${MAPE_JR_TOTAL:-?} / 的中 ${MAPE_JR_HIT:-?} 件 / 検証期日超過 ${MAPE_JR_DUE:-?} 件 |"
  echo
  echo "> 答え合わせを回す（このループの醸成）: \`python3 analysis/overnight_forecast.py run\`（採点→翌営業日予想）・\`/journal-review\`（検証期日の来た仮説を hit/miss 判定）。"
  echo "> **予想・仮説は将来の断定でも売買助言でもない**。的中率は少数標本では統計的に不安定（判断は標本が貯まってから）。"
  echo
  echo "## ✅ 自動（無害・可逆：チェック不要で PR まで実装。マージはしない）"
  echo
  emit_section auto "x"
  echo
  echo "> 自動項目は既定でチェック済み（\`[x]\`）です。実装してほしくないものは外してください。"
  echo
  echo "## 🟡 承認（挙動が変わる：**チェックした項目だけ**実装）"
  echo
  emit_section approve " "
  echo
  echo "## 🔴 相談（投資助言化/実データ・実発注/APIキー・秘密/課金/デプロイ：チェックしても、まず質問します）"
  echo
  emit_section consult " "
  echo
  echo "<details>"
  echo "<summary>✅ 完了ログ（${done_n} 件・実装 PR 済み。履歴の正本は mape/knowledge/PROGRESS.md）</summary>"
  echo
  if [ "$done_n" -gt 0 ]; then echo "$done_log"; else echo "- （まだありません）"; fi
  echo
  echo "</details>"
  echo
  echo "---"
  echo
  echo "### 使い方 / ガードレール"
  echo
  echo "- 実装してほしい項目にチェックを入れてください。Execute はポーリングで拾います。"
  echo "- **1周1件**・**トピックブランチ + PR**・**main は直接触らない**・**実データ/秘密/課金/実発注には触れない**。"
  echo "- **合成データ（--synthetic）で実市況・実データを偽装しない**（CLAUDE.md の絶対原則）。"
  echo "- **投資助言はしない**。レポートには必ず免責を入れる（\`stocklib.report.DISCLAIMER\`）。"
  echo "- 実装した項目には Execute が \`→ PR #N\` とコメントし、チェックを入れて二重実行を防ぎます（冪等性）。"
  echo "- テスト(pytest)が緑になった変更だけ PR にします。赤なら変更を破棄し、失敗を \`mape/knowledge/PROGRESS.md\` に記録します。"
  echo "- サーキットブレーカー: 同じ失敗や revert が続いたら Execute を止めて通知します。"
  echo "- 未チェックのまま時間が過ぎた項目は自動アーカイブされます（計画を腐らせない）。"
  echo
  echo "<!-- mape:cycle=${MAPE_CYCLE:-?} generated-by=mape/plan.sh -->"
} > "$out"

mape_log "plan 完了 → $out"
echo "$out"

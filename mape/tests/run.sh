#!/usr/bin/env bash
# MAPE-K の自己テスト（決定論部分の回帰防止）。analysis/tests/test_mape.py 経由で pytest から呼ばれる。
# docs/mape-k.md。
#
# 隔離: $MAPE_STATE_DIR を一時ディレクトリに向け、mape/knowledge/ は読み取りのみ。
# 注意: monitor は --with-gate を付けない（pytest 経由の再帰を避ける）。
set -u

# 再帰ガード: このテストは monitor/run を呼ぶ。monitor の --with-gate が pytest を回すと
# （pytest → このテスト → monitor → pytest …）無限再帰になるため、ゲート実行を抑止する。
export MAPE_NO_GATE=1

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAPE_DIR="$(cd "$TESTS_DIR/.." && pwd)"
REPO="$(cd "$MAPE_DIR/.." && pwd)"
cd "$REPO" || exit 1

pass=0; fail=0
ok()  { echo "  ok: $*"; pass=$((pass+1)); }
ng()  { echo "  NG: $*" >&2; fail=$((fail+1)); }
sec() { echo "== $* =="; }

# 隔離した state ディレクトリ（テストは mape/knowledge/ を変更しない）
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export MAPE_STATE_DIR="$TMP/state"

# ---------------------------------------------------------------------------
sec "0. 構文（bash -n）"
for f in "$MAPE_DIR"/*.sh "$TESTS_DIR"/*.sh; do
  [ -f "$f" ] || continue
  rel="${f#"$REPO"/}"
  if bash -n "$f" 2>/dev/null; then ok "syntax $rel"; else ng "syntax $rel"; fi
done

# lib を読み込んで関数を直接テスト
# shellcheck source=/dev/null
. "$MAPE_DIR/lib.sh"

# ---------------------------------------------------------------------------
sec "1. Monitor は読み取り専用でシグナルを出す"
health_before="$(cksum "$MAPE_HEALTH" 2>/dev/null)"
bash "$MAPE_DIR/monitor.sh" >/dev/null 2>&1
health_after="$(cksum "$MAPE_HEALTH" 2>/dev/null)"
[ -f "$MAPE_STATE_DIR/monitor.env" ] && ok "monitor.env 生成" || ng "monitor.env が無い"
missing=""
for k in MAPE_GATE MAPE_TODO MAPE_INDEX MAPE_KNOW_DOCS MAPE_CLI MAPE_MODULES MAPE_TESTS MAPE_UNTESTED MAPE_MAX_SKILL \
         MAPE_FC_GRADED MAPE_FC_PENDING MAPE_FC_HIT MAPE_FC_BRIER MAPE_JR_TOTAL MAPE_JR_VERIFIED MAPE_JR_HIT MAPE_JR_DUE; do
  grep -q "^$k=" "$MAPE_STATE_DIR/monitor.env" || missing="$missing $k"
done
[ -z "$missing" ] && ok "必須キーが揃う" || ng "monitor.env に欠落:$missing"
[ "$health_before" = "$health_after" ] && ok "--record 無しは HEALTH.md を変更しない" || ng "read-only 違反: HEALTH.md が変わった"

# ---------------------------------------------------------------------------
sec "2. Analyze は根拠つき提案をスコア降順で出す"
bash "$MAPE_DIR/analyze.sh" >/dev/null 2>&1
tsv="$MAPE_STATE_DIR/proposals.tsv"
[ -s "$tsv" ] && ok "proposals.tsv 生成（非空）" || ng "proposals.tsv が空"
# 5列目(score)が非増加か
if awk -F'\t' 'NR>1 && $5>prev{bad=1} {prev=$5} END{exit bad?1:0}' "$tsv"; then
  ok "スコア降順に整列"
else
  ng "スコアが降順でない"
fi
# 却下フィルタ（関数単体）
if mape_is_rejected "大規模な全面書き換えをする"; then ok "却下ログでフィルタされる"; else ng "却下フィルタが効かない"; fi
if mape_is_rejected "合成データで本番の市況を見せる"; then ok "合成データ偽装を却下"; else ng "合成データ偽装が却下されない"; fi

# ---------------------------------------------------------------------------
sec "3. Plan はリスク3分類チェックリストを出す"
bash "$MAPE_DIR/plan.sh" >/dev/null 2>&1
body="$MAPE_STATE_DIR/issue-body.md"
for h in "## ✅ 自動" "## 🟡 承認" "## 🔴 相談"; do
  grep -qF "$h" "$body" && ok "セクション: $h" || ng "セクション欠落: $h"
done
grep -qE '^- \[x\] ' "$body" && ok "自動項目は既定チェック済み[x]" || ng "自動項目に[x]が無い"
grep -qE '^- \[ \] ' "$body" && ok "承認/相談は未チェック[ ]あり" || ng "未チェック項目が無い"

# ---------------------------------------------------------------------------
sec "4. Execute のガードレール: 緑→記録 / 赤→記録・冪等"
cb="$MAPE_DIR/circuit-breaker.sh"
bash "$cb" record green "項目X" 42 mape/exec-x >/dev/null 2>&1
grep -q '"result":"green"' "$MAPE_STATE_DIR/ledger.jsonl" && ok "green を台帳へ記録" || ng "green 記録失敗"
grep -q '"pr":"42"' "$MAPE_STATE_DIR/ledger.jsonl" && ok "PR番号を記録" || ng "PR番号記録失敗"
if bash "$cb" done "項目X" >/dev/null 2>&1; then ok "done: 実装済み項目を検出（冪等性）"; else ng "done 判定失敗"; fi
if bash "$cb" done "未着手項目" >/dev/null 2>&1; then ng "done: 未着手を済みと誤判定"; else ok "done: 未着手は未実装と判定"; fi
bash "$cb" record red "項目Y" >/dev/null 2>&1
grep -q '"result":"red"' "$MAPE_STATE_DIR/ledger.jsonl" && ok "red を台帳へ記録" || ng "red 記録失敗"

# ---------------------------------------------------------------------------
sec "5. サーキットブレーカーが連鎖失敗で停止する"
rm -f "$MAPE_STATE_DIR/ledger.jsonl"
bash "$cb" status >/dev/null 2>&1 && ok "空台帳は ok(exit0)" || ng "空台帳で停止した"
bash "$cb" record red "同一項目" >/dev/null 2>&1
bash "$cb" record red "同一項目" >/dev/null 2>&1
if bash "$cb" status >/dev/null 2>&1; then ng "同一項目 red 2回で停止しない"; else ok "同一項目 red 2回で tripped(exit3)"; fi
bash "$cb" reset >/dev/null 2>&1
bash "$cb" record green "別項目" >/dev/null 2>&1
bash "$cb" status >/dev/null 2>&1 && ok "reset 後は ok に戻る" || ng "reset 後も停止のまま"

# ---------------------------------------------------------------------------
sec "6. リスク分類は危険側優先（consult）"
[ "$(mape_classify '投資助言を出す機能を追加する')" = "consult" ] && ok "投資助言→consult" || ng "投資助言が consult にならない"
[ "$(mape_classify 'JQUANTS_API_KEY を使って実データを取る')" = "consult" ] && ok "APIキー/実データ→consult" || ng "APIキーが consult にならない"
[ "$(mape_classify 'テスト追加する')" = "auto" ] && ok "テスト追加→auto" || ng "テスト追加が auto にならない"
[ "$(mape_classify '謎の変更')" = "approve" ] && ok "既定→approve（安全側）" || ng "既定が approve でない"

# ---------------------------------------------------------------------------
sec "7. mape/knowledge/ の機械可読構造が保たれる"
grep -qF '| ts(UTC) | cycle | gate | gate_s | todo | index | know_docs | cli | modules | tests | untested | max_skill | fc_graded | fc_hit | fc_brier | jr_verified | jr_hit | jr_due | note |' "$MAPE_HEALTH" \
  && ok "HEALTH 推移表ヘッダあり" || ng "HEALTH 推移表ヘッダが変わっている"
for h in '### consult' '### approve' '### auto'; do
  grep -qF "$h" "$MAPE_POLICY" && ok "POLICY 見出し: $h" || ng "POLICY 見出し欠落: $h"
done

# ---------------------------------------------------------------------------
sec "8. Plan はガードレール footer を掲示に含める"
grep -qF "### 使い方 / ガードレール" "$body" && ok "ガードレール見出しあり" || ng "ガードレール見出しが無い"
grep -qF "1周1件" "$body" && ok "1周1件の明記あり" || ng "1周1件の明記が無い"
grep -qF "投資助言はしない" "$body" && ok "投資助言禁止の明記あり" || ng "投資助言禁止の明記が無い"

# ---------------------------------------------------------------------------
sec "9. run.sh のドライランは mape/knowledge/ を一切変更しない"
snap_before="$(cat <(cksum "$MAPE_HEALTH") <(cksum "$MAPE_BACKLOG") <(cksum "$MAPE_PROGRESS") 2>/dev/null)"
bash "$MAPE_DIR/run.sh" >/dev/null 2>&1   # フラグ無し = ドライラン
snap_after="$(cat <(cksum "$MAPE_HEALTH") <(cksum "$MAPE_BACKLOG") <(cksum "$MAPE_PROGRESS") 2>/dev/null)"
[ "$snap_before" = "$snap_after" ] && ok "run.sh ドライランは HEALTH/BACKLOG/PROGRESS を変更しない" || ng "ドライランが knowledge を変更した"

# ---------------------------------------------------------------------------
sec "10. スコアは高インパクト・低労力ほど高い"
s_hi=$(mape_score 5 2); s_lo=$(mape_score 3 3)
[ "$s_hi" -gt "$s_lo" ] && ok "score(5,2)=$s_hi > score(3,3)=$s_lo" || ng "スコアの大小が逆転"
[ "$(mape_score 5 1)" -eq 25 ] && ok "score 上限=25" || ng "score 上限が 25 でない"

# ---------------------------------------------------------------------------
sec "11. Plan は完了ログを台帳から畳んで掲示する（green のみ）"
printf '%s\n' \
  '{"ts":"t1","item":"完了A","result":"green","pr":"5","branch":"b"}' \
  '{"ts":"t2","item":"完了B","result":"green","pr":"7","branch":"b"}' \
  '{"ts":"t3","item":"失敗C","result":"red","pr":"","branch":"b"}' > "$MAPE_STATE_DIR/ledger.jsonl"
bash "$MAPE_DIR/plan.sh" >/dev/null 2>&1
grep -qF '<summary>✅ 完了ログ（2 件' "$body"     && ok "完了ログの件数=green数(2)" || ng "完了ログ件数が合わない"
grep -qF '完了A → PR #5'  "$body"                  && ok "green を完了ログに掲示" || ng "green が完了ログに無い"
grep -qF '失敗C' "$body"                            && ng "red を完了ログに載せてはいけない" || ok "red は完了ログに載せない"
rm -f "$MAPE_STATE_DIR/ledger.jsonl"
bash "$MAPE_DIR/plan.sh" >/dev/null 2>&1
grep -qF '完了ログ（0 件' "$body" && ok "台帳が空でも完了ログ0件で壊れない" || ng "空台帳で完了ログが壊れる"

# ---------------------------------------------------------------------------
sec "12. 分析の答え合わせシグナル抽出（決定論・ネットワーク不使用）"
FIX="$TMP/fix"; mkdir -p "$FIX/forecasts" "$FIX/journal/2026"
cat > "$FIX/forecasts/ledger.csv" <<'CSV'
forecast_id,data,direction,prob_up,status,dir_hit,brier
a,real,up,0.6,graded,True,0.16
b,real,down,0.4,graded,False,0.36
c,real,up,0.7,graded,True,0.09
d,real,up,0.5,pending,,
e,synthetic,up,0.9,graded,True,0.01
CSV
cat > "$FIX/journal/2026/x.md" <<'MD'
---
review_date: 2026-01-01
outcome: hit
data: real
---
MD
cat > "$FIX/journal/2026/y.md" <<'MD'
---
review_date: 2026-06-01
outcome: pending
data: real
---
MD
sigout="$(python3 "$MAPE_DIR/analysis_signals.py" "$FIX" 2026-07-20)"
echo "$sigout" | grep -qx 'MAPE_FC_GRADED=3'  && ok "採点済み=3（synthetic 除外）" || ng "採点済み件数が違う"
echo "$sigout" | grep -qx 'MAPE_FC_PENDING=1' && ok "未採点=1" || ng "未採点件数が違う"
echo "$sigout" | grep -qx 'MAPE_FC_HIT=67'    && ok "方向的中率=67%（2/3）" || ng "方向的中率が違う"
echo "$sigout" | grep -qx 'MAPE_JR_DUE=1'     && ok "検証期日超過=1（未来分は除外）" || ng "検証期日超過が違う"
echo "$sigout" | grep -qx 'MAPE_JR_HIT=1'     && ok "ジャーナル的中=1" || ng "ジャーナル的中が違う"
# 空の台帳・ジャーナルでも na/0 で壊れない
empty="$TMP/empty"; mkdir -p "$empty"
python3 "$MAPE_DIR/analysis_signals.py" "$empty" 2026-07-20 | grep -qx 'MAPE_FC_HIT=na' && ok "空台帳は方向的中率 na" || ng "空台帳で壊れる"

# Analyze が未採点・検証期日超過を「答え合わせ」項目として surface する
sec "13. Analyze は答え合わせ（未採点・検証期日超過）を提案化する"
env2="$MAPE_STATE_DIR/monitor.env"
# monitor.env に track record シグナルを差し込んで analyze を回す
{ cat "$env2"; echo "MAPE_FC_PENDING=3"; echo "MAPE_JR_DUE=2"; } > "$env2.tmp" && mv "$env2.tmp" "$env2"
bash "$MAPE_DIR/analyze.sh" >/dev/null 2>&1
grep -qF '夜間フォーキャストの答え合わせ（grade）が未処理 3 件' "$MAPE_STATE_DIR/proposals.tsv" \
  && ok "未採点→答え合わせ提案" || ng "未採点が提案化されない"
grep -qF '検証期日が来たリサーチジャーナル仮説 2 件' "$MAPE_STATE_DIR/proposals.tsv" \
  && ok "検証期日超過→答え合わせ提案" || ng "検証期日超過が提案化されない"

# 弱い track record（十分な標本で的中率 < 50%）は手法見直しを提案する
sec "14. Analyze は弱い track record で手法見直しを提案する（標本十分時のみ）"
{ grep -vE '^MAPE_FC_' "$env2"; echo "MAPE_FC_GRADED=30"; echo "MAPE_FC_HIT=42"; echo "MAPE_FC_BRIER=0.30"; echo "MAPE_FC_PENDING=0"; } > "$env2.tmp" && mv "$env2.tmp" "$env2"
bash "$MAPE_DIR/analyze.sh" >/dev/null 2>&1
grep -qF '予想モデルの方向的中率が 42%' "$MAPE_STATE_DIR/proposals.tsv" && ok "的中率<50%→手法見直し提案" || ng "手法見直しが出ない"
grep -qF '較正不良' "$MAPE_STATE_DIR/proposals.tsv" && ok "Brier>0.25→較正見直し提案" || ng "較正見直しが出ない"
# 標本不足（graded < 20）では手法見直しを出さない（少数標本での過剰反応を避ける）
{ grep -vE '^MAPE_FC_' "$env2"; echo "MAPE_FC_GRADED=5"; echo "MAPE_FC_HIT=20"; echo "MAPE_FC_BRIER=0.40"; echo "MAPE_FC_PENDING=0"; } > "$env2.tmp" && mv "$env2.tmp" "$env2"
bash "$MAPE_DIR/analyze.sh" >/dev/null 2>&1
grep -qF '予想モデルの方向的中率' "$MAPE_STATE_DIR/proposals.tsv" && ng "標本不足でも手法見直しを出した" || ok "標本不足では手法見直しを出さない"

# ---------------------------------------------------------------------------
echo
echo "mape/tests: pass=$pass fail=$fail"
[ "$fail" -eq 0 ] || { echo "mape テスト失敗" >&2; exit 1; }
echo "mape テスト合格"

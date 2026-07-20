#!/usr/bin/env bash
# MAPE-K の自己テスト（決定論部分の回帰防止）。analysis/tests/test_mape.py 経由で pytest から呼ばれる。
# docs/mape-k.md。MAPE-K の主題は「株の解析の醸成」（予測精度＋分析カバレッジ）。
#
# 隔離: $MAPE_STATE_DIR を一時ディレクトリに向け、mape/knowledge/ は読み取りのみ。
# 注意: monitor は --with-gate を付けない（pytest 経由の再帰を避ける。MAPE_NO_GATE=1）。
set -u
export MAPE_NO_GATE=1

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAPE_DIR="$(cd "$TESTS_DIR/.." && pwd)"
REPO="$(cd "$MAPE_DIR/.." && pwd)"
cd "$REPO" || exit 1

pass=0; fail=0
ok()  { echo "  ok: $*"; pass=$((pass+1)); }
ng()  { echo "  NG: $*" >&2; fail=$((fail+1)); }
sec() { echo "== $* =="; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export MAPE_STATE_DIR="$TMP/state"

# ---------------------------------------------------------------------------
sec "0. 構文（bash -n / python -c）"
for f in "$MAPE_DIR"/*.sh "$TESTS_DIR"/*.sh; do
  [ -f "$f" ] || continue
  rel="${f#"$REPO"/}"
  if bash -n "$f" 2>/dev/null; then ok "syntax $rel"; else ng "syntax $rel"; fi
done
if python3 -c "import ast,sys; ast.parse(open('$MAPE_DIR/analysis_signals.py').read())" 2>/dev/null; then
  ok "syntax mape/analysis_signals.py"; else ng "syntax mape/analysis_signals.py"; fi

# shellcheck source=/dev/null
. "$MAPE_DIR/lib.sh"

# ---------------------------------------------------------------------------
sec "1. Monitor は読み取り専用で分析シグナルを出す"
health_before="$(cksum "$MAPE_HEALTH" 2>/dev/null)"
bash "$MAPE_DIR/monitor.sh" >/dev/null 2>&1
health_after="$(cksum "$MAPE_HEALTH" 2>/dev/null)"
[ -f "$MAPE_STATE_DIR/monitor.env" ] && ok "monitor.env 生成" || ng "monitor.env が無い"
missing=""
for k in MAPE_GATE MAPE_FC_GRADED MAPE_FC_HIT MAPE_FC_BRIER MAPE_FC_PENDING \
         MAPE_JR_VERIFIED MAPE_JR_HIT MAPE_JR_DUE \
         MAPE_UNIVERSE MAPE_COVERED MAPE_COVERAGE MAPE_UNANALYZED MAPE_KNOW_DOCS MAPE_STALE_DOCS; do
  grep -q "^$k=" "$MAPE_STATE_DIR/monitor.env" || missing="$missing $k"
done
[ -z "$missing" ] && ok "分析シグナルの必須キーが揃う" || ng "monitor.env に欠落:$missing"
[ "$health_before" = "$health_after" ] && ok "--record 無しは HEALTH.md を変更しない" || ng "read-only 違反"
# システム健全性シグナルは主題外＝出力しない
grep -qE '^MAPE_(TODO|INDEX|UNTESTED|MAX_SKILL|CHURN_TOP)=' "$MAPE_STATE_DIR/monitor.env" \
  && ng "システム健全性シグナルが残っている（主題外）" || ok "システム健全性シグナルは出力しない"

# ---------------------------------------------------------------------------
sec "2. Analyze は根拠つき提案をスコア降順・重複排除で出す"
bash "$MAPE_DIR/analyze.sh" >/dev/null 2>&1
tsv="$MAPE_STATE_DIR/proposals.tsv"
[ -s "$tsv" ] && ok "proposals.tsv 生成（非空）" || ng "proposals.tsv が空"
if awk -F'\t' 'NR>1 && $5>prev{bad=1} {prev=$5} END{exit bad?1:0}' "$tsv"; then ok "スコア降順に整列"; else ng "スコアが降順でない"; fi
if [ "$(cut -f6 "$tsv" | sort | uniq -d | grep -c .)" -eq 0 ]; then ok "提案テキストに重複がない"; else ng "提案テキストが重複している"; fi
if mape_is_rejected "大規模な全面書き換えをする"; then ok "却下ログでフィルタされる"; else ng "却下フィルタが効かない"; fi
if mape_is_rejected "合成データで本番の市況を見せる"; then ok "合成データ偽装を却下"; else ng "合成データ偽装が却下されない"; fi

# ---------------------------------------------------------------------------
sec "3. Plan は分析ダッシュボード＋リスク3分類を出す"
bash "$MAPE_DIR/plan.sh" >/dev/null 2>&1
body="$MAPE_STATE_DIR/issue-body.md"
for h in "## 🎯 予測精度" "## 🗺️ 分析カバレッジ" "## ✅ 自動" "## 🟡 承認" "## 🔴 相談"; do
  grep -qF "$h" "$body" && ok "セクション: $h" || ng "セクション欠落: $h"
done
grep -qF "主題は株の解析の醸成" "$body" && ok "主題（株の解析の醸成）を明記" || ng "主題の明記が無い"

# ---------------------------------------------------------------------------
sec "4. Execute のガードレール: 緑→記録 / 赤→記録・冪等"
cb="$MAPE_DIR/circuit-breaker.sh"
bash "$cb" record green "項目X" 42 mape/exec-x >/dev/null 2>&1
grep -q '"result":"green"' "$MAPE_STATE_DIR/ledger.jsonl" && ok "green を台帳へ記録" || ng "green 記録失敗"
if bash "$cb" done "項目X" >/dev/null 2>&1; then ok "done: 実装済みを検出（冪等性）"; else ng "done 判定失敗"; fi
if bash "$cb" done "未着手項目" >/dev/null 2>&1; then ng "done: 未着手を済みと誤判定"; else ok "done: 未着手は未実装と判定"; fi

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
sec "7. mape/knowledge/ の機械可読構造（分析スキーマ）が保たれる"
grep -qF '| ts(UTC) | cycle | gate | fc_graded | fc_hit | fc_brier | fc_pending | jr_verified | jr_hit | jr_due | coverage | unanalyzed | know_docs | stale_docs | note |' "$MAPE_HEALTH" \
  && ok "HEALTH 推移表ヘッダ（分析スキーマ）あり" || ng "HEALTH 推移表ヘッダが変わっている"
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
bash "$MAPE_DIR/run.sh" >/dev/null 2>&1
snap_after="$(cat <(cksum "$MAPE_HEALTH") <(cksum "$MAPE_BACKLOG") <(cksum "$MAPE_PROGRESS") 2>/dev/null)"
[ "$snap_before" = "$snap_after" ] && ok "run.sh ドライランは knowledge を変更しない" || ng "ドライランが knowledge を変更した"

# ---------------------------------------------------------------------------
sec "10. スコアは高インパクト・低労力ほど高い"
[ "$(mape_score 5 2)" -gt "$(mape_score 3 3)" ] && ok "score(5,2) > score(3,3)" || ng "スコアの大小が逆転"
[ "$(mape_score 5 1)" -eq 25 ] && ok "score 上限=25" || ng "score 上限が 25 でない"

# ---------------------------------------------------------------------------
sec "11. Plan は完了ログを台帳から畳んで掲示する（green のみ）"
printf '%s\n' \
  '{"ts":"t1","item":"完了A","result":"green","pr":"5","branch":"b"}' \
  '{"ts":"t2","item":"失敗C","result":"red","pr":"","branch":"b"}' > "$MAPE_STATE_DIR/ledger.jsonl"
bash "$MAPE_DIR/plan.sh" >/dev/null 2>&1
grep -qF '完了A → PR #5' "$body" && ok "green を完了ログに掲示" || ng "green が完了ログに無い"
grep -qF '失敗C' "$body" && ng "red を完了ログに載せてはいけない" || ok "red は完了ログに載せない"
rm -f "$MAPE_STATE_DIR/ledger.jsonl"

# ---------------------------------------------------------------------------
sec "12. 分析シグナル抽出（予測精度＋カバレッジ・決定論・ネットワーク不使用）"
FIX="$TMP/fix"; mkdir -p "$FIX/forecasts" "$FIX/journal/2026" "$FIX/analysis/universe" "$FIX/knowledge/x"
printf '# c\ncode,name,sector\n7203,a,x\n6758,b,y\n9984,c,z\n' > "$FIX/analysis/universe/liquid30.csv"
printf 'forecast_id,code,data,status,dir_hit,in_range,brier\n1,7203,real,graded,True,True,0.16\n2,7203,real,graded,False,False,0.36\n3,7203,real,pending,,,\n4,9984,synthetic,graded,True,True,0.01\n' > "$FIX/forecasts/ledger.csv"
printf -- '---\ncodes: ["6758"]\nreview_date: 2026-01-01\noutcome: hit\ndata: real\n---\n' > "$FIX/journal/2026/a.md"
printf -- '# t\n2024年時点\n' > "$FIX/knowledge/x/old.md"
printf -- '# t\n2026年時点\n' > "$FIX/knowledge/x/new.md"
so="$(python3 "$MAPE_DIR/analysis_signals.py" "$FIX" 2026-07-20)"
chk() { echo "$so" | grep -qx "$1" && ok "$1" || ng "期待 $1 / 実際: $(echo "$so" | tr '\n' ' ')"; }
chk 'MAPE_FC_GRADED=2'      # synthetic 除外
chk 'MAPE_FC_HIT=50'        # 1/2
chk 'MAPE_FC_PENDING=1'
chk 'MAPE_JR_HIT=1'
chk 'MAPE_UNIVERSE=3'
chk 'MAPE_COVERED=2'        # 7203(ledger)+6758(journal)
chk 'MAPE_COVERAGE=67'
chk 'MAPE_UNANALYZED=1'     # 9984 は real 記録なし
chk 'MAPE_STALE_DOCS=1'     # old.md(2024) のみ
python3 "$MAPE_DIR/analysis_signals.py" "$TMP/empty" 2026-07-20 2>/dev/null | grep -qx 'MAPE_COVERAGE=na' \
  && ok "ユニバース無しは coverage=na" || ok "空環境でも壊れない"

# ---------------------------------------------------------------------------
sec "13. Analyze は答え合わせ・カバレッジを提案化する"
env2="$MAPE_STATE_DIR/monitor.env"
{ grep -vE '^MAPE_(FC_PENDING|JR_DUE|UNANALYZED)=' "$env2"; echo "MAPE_FC_PENDING=3"; echo "MAPE_JR_DUE=2"; echo "MAPE_UNANALYZED=7"; } > "$env2.t" && mv "$env2.t" "$env2"
printf '9101\n9202\n' > "$MAPE_STATE_DIR/unanalyzed-codes.txt"
bash "$MAPE_DIR/analyze.sh" >/dev/null 2>&1
grep -qF '答え合わせ（grade）が未処理 3 件' "$tsv" && ok "未採点→答え合わせ提案" || ng "未採点が提案化されない"
grep -qF 'リサーチジャーナル仮説 2 件の答え合わせ' "$tsv" && ok "検証期日超過→答え合わせ提案" || ng "検証期日超過が提案化されない"
grep -qF '未分析銘柄 7 件をユニバースへ醸成' "$tsv" && ok "未分析銘柄→カバレッジ醸成提案" || ng "未分析銘柄が提案化されない"

# ---------------------------------------------------------------------------
sec "14. Analyze は弱い予測精度で手法見直しを提案する（標本十分時のみ）"
{ grep -vE '^MAPE_FC_' "$env2"; echo "MAPE_FC_GRADED=30"; echo "MAPE_FC_HIT=42"; echo "MAPE_FC_BRIER=0.30"; echo "MAPE_FC_INRANGE=90"; echo "MAPE_FC_PENDING=0"; } > "$env2.t" && mv "$env2.t" "$env2"
bash "$MAPE_DIR/analyze.sh" >/dev/null 2>&1
grep -qF '予想モデルの方向的中率が 42%' "$tsv" && ok "的中率<50%→手法見直し提案" || ng "手法見直しが出ない"
grep -qF '較正不良' "$tsv" && ok "Brier>0.25→較正見直し提案" || ng "較正見直しが出ない"
{ grep -vE '^MAPE_FC_' "$env2"; echo "MAPE_FC_GRADED=5"; echo "MAPE_FC_HIT=20"; echo "MAPE_FC_BRIER=0.40"; echo "MAPE_FC_INRANGE=90"; echo "MAPE_FC_PENDING=0"; } > "$env2.t" && mv "$env2.t" "$env2"
bash "$MAPE_DIR/analyze.sh" >/dev/null 2>&1
grep -qF '予想モデルの方向的中率' "$tsv" && ng "標本不足でも手法見直しを出した" || ok "標本不足では手法見直しを出さない"

# ---------------------------------------------------------------------------
echo
echo "mape/tests: pass=$pass fail=$fail"
[ "$fail" -eq 0 ] || { echo "mape テスト失敗" >&2; exit 1; }
echo "mape テスト合格"

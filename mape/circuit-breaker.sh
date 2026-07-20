#!/usr/bin/env bash
# ガードレール — サーキットブレーカー ＆ 実行台帳（ledger）。docs/mape-k.md。
# Execute の各試行を $MAPE_STATE_DIR/ledger.jsonl に追記し、危険な連鎖を検知して停止させる。
#
# 使い方:
#   bash mape/circuit-breaker.sh status                     # ok なら exit 0 / 停止条件なら exit 3
#   bash mape/circuit-breaker.sh record <green|red> <item> [pr] [branch]
#   bash mape/circuit-breaker.sh done <item>               # 実装済み(green)なら exit 0（冪等性クエリ）
#   bash mape/circuit-breaker.sh reset                      # 台帳を退避してブレーカーを解除
#
# 停止条件（POLICY 相当。環境変数で調整可能。lib.sh 参照）:
#   - 末尾が連続 red で MAPE_CB_CONSECUTIVE_FAIL 件に達した
#   - 同一 item の red が MAPE_CB_SAME_ITEM_FAIL 回に達した
#   - 直近 MAPE_CB_REVERT_WINDOW 件のうち red が MAPE_CB_REVERT_MAX 件以上
set -u
# shellcheck source=lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

mape_ensure_state
ledger="$MAPE_STATE_DIR/ledger.jsonl"

cmd="${1:-status}"

case "$cmd" in
  record)
    result="${2:-}"; item="${3:-}"; pr="${4:-}"; branch="${5:-}"
    [ "$result" = "green" ] || [ "$result" = "red" ] || mape_die "result は green|red"
    [ -n "$item" ] || mape_die "item が必要"
    ts=$(mape_now)
    if command -v jq >/dev/null 2>&1; then
      jq -cn --arg ts "$ts" --arg item "$item" --arg result "$result" --arg pr "$pr" --arg branch "$branch" \
        '{ts:$ts,item:$item,result:$result,pr:$pr,branch:$branch}' >> "$ledger"
    else
      python3 - "$ts" "$item" "$result" "$pr" "$branch" >> "$ledger" <<'PY'
import json,sys
ts,item,result,pr,branch=sys.argv[1:6]
print(json.dumps({"ts":ts,"item":item,"result":result,"pr":pr,"branch":branch},ensure_ascii=False))
PY
    fi
    mape_log "ledger 追記: $result — $item"
    ;;

  status)
    [ -f "$ledger" ] || { echo "ok: 台帳が空（試行なし）"; exit 0; }
    python3 - "$ledger" "$MAPE_CB_CONSECUTIVE_FAIL" "$MAPE_CB_SAME_ITEM_FAIL" "$MAPE_CB_REVERT_WINDOW" "$MAPE_CB_REVERT_MAX" <<'PY'
import json,sys
path,cons_max,same_max,win,win_max=sys.argv[1],int(sys.argv[2]),int(sys.argv[3]),int(sys.argv[4]),int(sys.argv[5])
rows=[]
for ln in open(path,encoding="utf-8"):
    ln=ln.strip()
    if not ln: continue
    try: rows.append(json.loads(ln))
    except Exception: pass
# 末尾連続 red
trail=0
for r in reversed(rows):
    if r.get("result")=="red": trail+=1
    else: break
# 同一 item の red 回数
from collections import Counter
c=Counter(r.get("item") for r in rows if r.get("result")=="red")
worst_item,worst_n=(c.most_common(1)[0] if c else (None,0))
# 直近ウィンドウの red 件数
window=rows[-win:]
win_red=sum(1 for r in window if r.get("result")=="red")
reasons=[]
if trail>=cons_max: reasons.append(f"末尾連続 red {trail}件（>= {cons_max}）")
if worst_n>=same_max: reasons.append(f"同一項目の red {worst_n}回（>= {same_max}）: {worst_item}")
if win_red>=win_max: reasons.append(f"直近{len(window)}件中 red {win_red}件（>= {win_max}）")
if reasons:
    print("tripped: "+" / ".join(reasons))
    sys.exit(3)
print(f"ok: 試行{len(rows)}件 / 末尾連続red {trail} / 直近window red {win_red}")
PY
    exit $?
    ;;

  done)
    # 冪等性クエリ: 指定 item が台帳に green で存在すれば exit 0（＝実装済み・再実行不要）
    item="${2:-}"; [ -n "$item" ] || mape_die "item が必要"
    [ -f "$ledger" ] || exit 1
    if command -v jq >/dev/null 2>&1; then
      jq -e --arg it "$item" 'select(.item==$it and .result=="green")' "$ledger" >/dev/null 2>&1 && exit 0 || exit 1
    else
      python3 - "$ledger" "$item" <<'PY'
import json,sys
path,item=sys.argv[1],sys.argv[2]
for ln in open(path,encoding="utf-8"):
    ln=ln.strip()
    if not ln: continue
    try: r=json.loads(ln)
    except Exception: continue
    if r.get("item")==item and r.get("result")=="green": sys.exit(0)
sys.exit(1)
PY
    fi
    ;;

  reset)
    if [ -f "$ledger" ]; then
      bak="$ledger.$(date -u +%Y%m%dT%H%M%SZ).bak"
      mv "$ledger" "$bak"
      mape_log "台帳を $bak に退避してブレーカーを解除"
    else
      mape_log "台帳は空。解除不要"
    fi
    ;;

  *)
    mape_die "未知のコマンド: $cmd（status|record|done|reset）"
    ;;
esac

#!/usr/bin/env bash
# M — Monitor（監視）。docs/mape-k.md。
# リポジトリのシグナルを集めて $MAPE_STATE_DIR/monitor.env と monitor.md に書き出す。
# 読み取り専用（--record を渡したときだけ mape/knowledge/HEALTH.md に1行追記する）。
#
# 使い方:
#   bash mape/monitor.sh              # 収集のみ（state/ に出力、HEALTH は触らない）
#   bash mape/monitor.sh --with-gate  # pytest（品質ゲート）を実行して合否と所要秒も測る
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

# --- シグナル収集 ---
# TODO/FIXME はコード（.py/.sh）のコメントマーカーに限定して数える（散文中の誤検知を避ける）。
# 対象は分析コードと運用スクリプトのみ（knowledge/・mape/・data/・reports/ は除外）。
todo=$(grep -rIn --include='*.py' --include='*.sh' \
        --exclude-dir=.git --exclude-dir=mape --exclude-dir=knowledge \
        -E '(#|//)[[:space:]]*(TODO|FIXME)\b|\b(TODO|FIXME):' \
        analysis scripts .claude .github 2>/dev/null | wc -l | tr -d ' ')

# 日本株ナレッジベースの文書数（00-index.md を除く）
know_docs=$(find knowledge -name '*.md' -type f 2>/dev/null | grep -cv '00-index.md' | tr -d ' ')

# 索引の整合（未索引・リンク切れ・文書数ずれ）を機械検査。ok/ng。
index="skip"
if [ -f scripts/check_knowledge_index.py ]; then
  if python3 scripts/check_knowledge_index.py --all >"$MAPE_STATE_DIR/index.log" 2>&1; then index="ok"; else index="ng"; fi
fi

# トップレベル分析 CLI（analysis/*.py）と stocklib モジュール数
cli=$(find analysis -maxdepth 1 -name '*.py' -type f 2>/dev/null | wc -l | tr -d ' ')
modules=$(find analysis/stocklib -name '*.py' -type f 2>/dev/null | grep -cv '__init__.py' | tr -d ' ')

# テストファイル数
tests=$(find analysis/tests -name 'test_*.py' -type f 2>/dev/null | wc -l | tr -d ' ')

# テストの無い stocklib モジュール（coverage の穴）。名前を state に、件数を指標に。
untested_list="$MAPE_STATE_DIR/untested-modules.txt"
: > "$untested_list"
for m in analysis/stocklib/*.py; do
  [ -f "$m" ] || continue
  base=$(basename "$m" .py)
  [ "$base" = "__init__" ] && continue
  if [ ! -f "analysis/tests/test_${base}.py" ]; then
    echo "$base" >> "$untested_list"
  fi
done
untested=$(grep -c . "$untested_list" 2>/dev/null || true); untested=${untested:-0}

# 最長 SKILL.md（予算 200 行 — progressive disclosure の目安）
max_skill=0
for s in .claude/skills/*/SKILL.md; do
  [ -f "$s" ] || continue
  n=$(wc -l < "$s" | tr -d ' ')
  [ "$n" -gt "$max_skill" ] && max_skill=$n
done

# 変更が集中する箇所（churn）: 直近30コミットで最も触られたファイル
churn_top=$(git log -n 30 --name-only --pretty=format: 2>/dev/null \
            | grep -v '^$' | sort | uniq -c | sort -rn | head -1 | sed -E 's/^[[:space:]]*[0-9]+[[:space:]]+//')
[ -z "$churn_top" ] && churn_top="-"

# 品質ゲート（任意）= pytest。
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
  echo "MAPE_TODO=$todo"
  echo "MAPE_INDEX=$index"
  echo "MAPE_KNOW_DOCS=$know_docs"
  echo "MAPE_CLI=$cli"
  echo "MAPE_MODULES=$modules"
  echo "MAPE_TESTS=$tests"
  echo "MAPE_UNTESTED=$untested"
  echo "MAPE_MAX_SKILL=$max_skill"
  echo "MAPE_CHURN_TOP=$churn_top"
} > "$MAPE_STATE_DIR/monitor.env"

# --- 出力: monitor.md（人が読む要約） ---
{
  echo "# Monitor レポート — $ts (cycle $cycle)"
  echo
  echo "| 指標 | 値 |"
  echo "|---|---|"
  echo "| gate (pytest) | $gate ($gate_s s) |"
  echo "| todo/fixme | $todo |"
  echo "| 索引整合 | $index |"
  echo "| ナレッジ文書 | $know_docs |"
  echo "| 分析 CLI | $cli |"
  echo "| stocklib モジュール | $modules |"
  echo "| テストファイル | $tests |"
  echo "| テスト無しモジュール | $untested |"
  echo "| 最長 SKILL 行 | $max_skill / 200 |"
  echo "| churn 首位 | $churn_top |"
} > "$MAPE_STATE_DIR/monitor.md"

# --- HEALTH.md へ追記（--record のときだけ） ---
row="| $ts | $cycle | $gate | $gate_s | $todo | $index | $know_docs | $cli | $modules | $tests | $untested | $max_skill | monitor |"
if [ "$record" -eq 1 ] && [ -f "$MAPE_HEALTH" ]; then
  printf '%s\n' "$row" >> "$MAPE_HEALTH"
  mape_log "HEALTH.md に cycle $cycle を追記"
fi

echo "$row"
mape_log "monitor 完了 → $MAPE_STATE_DIR/monitor.env"

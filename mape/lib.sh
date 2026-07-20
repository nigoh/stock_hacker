#!/usr/bin/env bash
# MAPE-K 共有ライブラリ（source して使う。単体実行はしない）。docs/mape-k.md。
# すべての mape スクリプトが読み込む共通関数と設定。
#
# 設計の約束:
#  - スクリプトは既定で「読み取り専用」= 書き込みは $MAPE_STATE_DIR 配下のみ。
#    mape/knowledge/ を変更するのは --record / --update-knowledge を渡したときだけ。
#  - $MAPE_STATE_DIR は環境変数で差し替え可能（テストは一時ディレクトリを指す）。
#  - MAPE-K の共有ナレッジ（K）は mape/knowledge/ に置く。これは日本株ナレッジベース
#    knowledge/（00-index.md で索引される90文書）とは別物であることに注意。

# リポジトリルート（このファイルの2つ上 = mape/ の親）
MAPE_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAPE_ROOT="$(cd "$MAPE_LIB_DIR/.." && pwd)"
export MAPE_ROOT

# 中間成果物の置き場（テストで差し替え可能）
MAPE_STATE_DIR="${MAPE_STATE_DIR:-$MAPE_ROOT/mape/state}"
export MAPE_STATE_DIR

# MAPE-K 知識ファイル（K）。日本株ナレッジベース knowledge/ とは別（mape/knowledge/ に置く）。
MAPE_KNOWLEDGE_DIR="${MAPE_KNOWLEDGE_DIR:-$MAPE_ROOT/mape/knowledge}"
export MAPE_KNOWLEDGE_DIR
MAPE_POLICY="$MAPE_KNOWLEDGE_DIR/POLICY.md"
MAPE_HEALTH="$MAPE_KNOWLEDGE_DIR/HEALTH.md"
MAPE_BACKLOG="$MAPE_KNOWLEDGE_DIR/BACKLOG.md"
MAPE_PROGRESS="$MAPE_KNOWLEDGE_DIR/PROGRESS.md"

# 品質ゲート（stock_hacker の唯一の自動検証は pytest。--with-gate で監視が測る）
MAPE_GATE_CMD="${MAPE_GATE_CMD:-python3 -m pytest analysis/tests -q}"

# サーキットブレーカーの閾値（POLICY で上書きしたくなったら環境変数で）
MAPE_CB_CONSECUTIVE_FAIL="${MAPE_CB_CONSECUTIVE_FAIL:-3}"   # 直近が連続 fail でこの数に達したら停止
MAPE_CB_SAME_ITEM_FAIL="${MAPE_CB_SAME_ITEM_FAIL:-2}"       # 同一項目がこの回数 fail したら停止
MAPE_CB_REVERT_WINDOW="${MAPE_CB_REVERT_WINDOW:-5}"          # 直近この件数を見る
MAPE_CB_REVERT_MAX="${MAPE_CB_REVERT_MAX:-3}"                # そのうち revert がこの数以上で停止

mape_log() { printf '[mape] %s\n' "$*" >&2; }
mape_die() { printf '[mape] ERROR: %s\n' "$*" >&2; exit 1; }

# 決定論的な UTC タイムスタンプ
mape_now() { date -u +%Y-%m-%dT%H:%MZ; }

mape_ensure_state() { mkdir -p "$MAPE_STATE_DIR"; }

# POLICY.md の「### <tier>」見出し直後の ``` フェンス内キーワードを1行ずつ出力する
mape_policy_keywords() {
  local tier="$1"
  [ -f "$MAPE_POLICY" ] || return 0
  awk -v t="$tier" '
    $0 ~ ("^### " t) { found=1; infence=0; next }
    found && /^```/   { if (infence) { exit } else { infence=1; next } }
    found && infence  { print }
  ' "$MAPE_POLICY"
}

# 提案テキストをリスク分類する（consult > approve > auto の危険側優先。既定 approve）。
mape_classify() {
  local text="$1" tier kw
  for tier in consult approve auto; do
    while IFS= read -r kw; do
      [ -z "$kw" ] && continue
      if printf '%s' "$text" | grep -qiF -- "$kw"; then
        printf '%s' "$tier"; return 0
      fi
    done < <(mape_policy_keywords "$tier")
  done
  printf 'approve'
}

# POLICY.md「## 却下ログ」の "- pattern: <正規表現> — <理由>" にマッチするか（マッチ=却下）
mape_is_rejected() {
  local text="$1" pat
  [ -f "$MAPE_POLICY" ] || return 1
  while IFS= read -r pat; do
    [ -z "$pat" ] && continue
    if printf '%s' "$text" | grep -qiE -- "$pat"; then return 0; fi
  done < <(grep -oE '^- pattern:[[:space:]]*[^—]+' "$MAPE_POLICY" 2>/dev/null | sed -E 's/^- pattern:[[:space:]]*//; s/[[:space:]]+$//')
  return 1
}

# インパクト(1-5) と 労力(1-5) からスコアを出す（高インパクト・低労力ほど高い。範囲 1-25）
mape_score() {
  local impact="$1" effort="$2"
  [ "$impact" -ge 1 ] 2>/dev/null || impact=3
  [ "$effort" -ge 1 ] 2>/dev/null || effort=3
  echo $(( impact * (6 - effort) ))
}

#!/bin/bash
# SessionStart フック: stock_hacker の分析環境をセットアップし、セッションに文脈を注入する。
#
# - 依存パッケージ（pandas / numpy / yfinance / pytest）が無ければ requirements.txt からインストール
# - 生成物ディレクトリ（reports/, data/cache/）を作成
# - stocklib のインポート確認（スモークチェック）
# - 環境の要点（CLI・スキル・ナレッジベース）を stdout に出力し、セッション開始時の文脈にする
#
# 冪等・非対話。ネットワーク不通でもセッションを止めないため、原則 exit 0 で終える
# （--synthetic フラグで全 CLI がオフライン動作するため）。
set -uo pipefail

REPO_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO_ROOT"

# --- 1. 生成物ディレクトリ（gitignore 済み領域） ---------------------------
mkdir -p reports data/cache

# --- 2. 依存パッケージの確認とインストール（冪等） -------------------------
DEPS_OK=1
if ! python3 -c "import pandas, numpy, yfinance, pytest" >/dev/null 2>&1; then
  echo "依存パッケージをインストールしています（requirements.txt）..."
  if ! pip install --quiet --disable-pip-version-warning -r requirements.txt >/dev/null 2>&1; then
    DEPS_OK=0
    echo "警告: pip install に失敗しました（ネットワーク不通の可能性）。" >&2
    echo "      既存環境にパッケージがあれば分析は動作します。無い場合は手動で 'pip install -r requirements.txt' を実行してください。" >&2
  fi
  # インストール後の再確認
  if python3 -c "import pandas, numpy, yfinance, pytest" >/dev/null 2>&1; then
    DEPS_OK=1
  fi
fi

# --- 3. stocklib のスモークチェック ----------------------------------------
LIB_OK=1
if ! python3 -c "import sys; sys.path.insert(0, 'analysis'); import stocklib" >/dev/null 2>&1; then
  LIB_OK=0
  echo "警告: analysis/stocklib のインポートに失敗しました。依存パッケージを確認してください。" >&2
fi

# --- 4. リサーチジャーナルの期日サマリー -----------------------------------
# `research_journal.py due` 相当の集計。due はネットワーク（価格取得）を行わない
# オフライン安全な処理なので SessionStart で実行してよい。
# python3 / stocklib が使えない場合は黙ってスキップし、セッションは止めない（exit 0 維持）。
JOURNAL_SECTION=""
if [ -d journal ] && find journal -mindepth 2 -name '*.md' -print -quit 2>/dev/null | grep -q .; then
  JOURNAL_COUNTS=$(python3 - <<'PY' 2>/dev/null
import sys

sys.path.insert(0, "analysis")
from stocklib import journal

entries = journal.iter_entries()
due = journal.due_entries(entries)
n_open = sum(1 for e in entries if e.status == "open")
print(f"{len(due)}\t{n_open}\t{len(entries)}")
PY
)
  if [ -n "$JOURNAL_COUNTS" ]; then
    IFS=$'	' read -r N_DUE N_OPEN N_ALL <<<"$JOURNAL_COUNTS"
    JOURNAL_SECTION="
## リサーチジャーナル（journal/）

- 検証期日到来 ${N_DUE} 件 / open ${N_OPEN} 件（全 ${N_ALL} 件）。詳細: python3 analysis/research_journal.py due"
    if [ "${N_DUE:-0}" -gt 0 ] 2>/dev/null; then
      JOURNAL_SECTION="${JOURNAL_SECTION}
- 検証期日を迎えた仮説があります。/journal-review で検証と振り返りを行ってください（仮説の記録しっぱなし防止）。"
    fi
    JOURNAL_SECTION="${JOURNAL_SECTION}
"
  fi
fi

# --- 5. セッション文脈の出力（stdout はセッション開始時の文脈に入る） ------
KNOWLEDGE_COUNT=$(find knowledge -name '*.md' ! -name '00-index.md' 2>/dev/null | wc -l | tr -d ' ')

cat <<EOF
# stock_hacker セッション開始

このリポジトリは日本株の総合分析環境です。あなたは日本株解析のプロフェッショナルとして振る舞ってください。
規約は CLAUDE.md、知識の索引は knowledge/00-index.md（${KNOWLEDGE_COUNT} 文書）にあります。分析・回答の前に必ず索引から関連文書を参照してください。

## 環境ステータス

- 依存パッケージ (pandas/numpy/yfinance/pytest): $([ "$DEPS_OK" = 1 ] && echo "OK" || echo "未整備（オフライン時は --synthetic で動作可）")
- stocklib インポート: $([ "$LIB_OK" = 1 ] && echo "OK" || echo "失敗")
${JOURNAL_SECTION}
## 分析 CLI（リポジトリルートから実行、ネットワーク不通時は --synthetic を付ける）

- 個別分析:       python3 analysis/analyze_stock.py 7203 [--period 2y] [--benchmark ^N225] [--synthetic]
- スクリーニング: python3 analysis/screen.py [--universe analysis/universe/liquid30.csv] [--rsi-below 30] [--price-above-sma 200] [--synthetic]
- 銘柄比較:       python3 analysis/compare.py 7203 6758 9984 [--period 1y] [--synthetic]
- バックテスト:   python3 analysis/run_backtest.py --strategy ma_cross --code 7203 [--fast 25 --slow 75] [--cost-bps 10] [--synthetic]

レポートは reports/ に出力されます。銘柄コードは4桁数字（内部で "7203.T" に正規化）。
テストは 'python3 -m pytest analysis/tests' で実行できます。

## スキル / コマンド

analyze-stock（/analyze）・screen-market（/screen）・compare-stocks（/compare）・backtest-strategy（/backtest）・market-review（/market）・portfolio-review（/portfolio）・daily-brief（/brief）・earnings-analysis（/earnings）・knowledge-doc（/learn）・research-journal（/journal）・journal-review（/journal-review）、ナレッジ検索は /kb、レポートの敵対的レビュー（外部共有前の品質ゲート）は /review-report。

注意: 本環境の出力は投資助言ではなく分析支援です。レポートには必ず免責の一文を入れてください。
EOF

exit 0

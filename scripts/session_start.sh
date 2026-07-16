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

# --- 4. セッション文脈の出力（stdout はセッション開始時の文脈に入る） ------
KNOWLEDGE_COUNT=$(find knowledge -name '*.md' ! -name '00-index.md' 2>/dev/null | wc -l | tr -d ' ')

cat <<EOF
# stock_hacker セッション開始

このリポジトリは日本株の総合分析環境です。あなたは日本株解析のプロフェッショナルとして振る舞ってください。
規約は CLAUDE.md、知識の索引は knowledge/00-index.md（${KNOWLEDGE_COUNT} 文書）にあります。分析・回答の前に必ず索引から関連文書を参照してください。

## 環境ステータス

- 依存パッケージ (pandas/numpy/yfinance/pytest): $([ "$DEPS_OK" = 1 ] && echo "OK" || echo "未整備（オフライン時は --synthetic で動作可）")
- stocklib インポート: $([ "$LIB_OK" = 1 ] && echo "OK" || echo "失敗")

## 分析 CLI（リポジトリルートから実行、ネットワーク不通時は --synthetic を付ける）

- 個別分析:       python3 analysis/analyze_stock.py 7203 [--period 2y] [--benchmark ^N225] [--synthetic]
- スクリーニング: python3 analysis/screen.py [--universe analysis/universe/liquid30.csv] [--rsi-below 30] [--price-above-sma 200] [--synthetic]
- 銘柄比較:       python3 analysis/compare.py 7203 6758 9984 [--period 1y] [--synthetic]
- バックテスト:   python3 analysis/run_backtest.py --strategy ma_cross --code 7203 [--fast 25 --slow 75] [--cost-bps 10] [--synthetic]

レポートは reports/ に出力されます。銘柄コードは4桁数字（内部で "7203.T" に正規化）。
テストは 'python3 -m pytest analysis/tests' で実行できます。

## スキル / コマンド

analyze-stock（/analyze）・screen-market（/screen）・compare-stocks（/compare）・backtest-strategy（/backtest）・market-review（/market）・knowledge-doc（/learn）、ナレッジ検索は /kb。

注意: 本環境の出力は投資助言ではなく分析支援です。レポートには必ず免責の一文を入れてください。
EOF

exit 0

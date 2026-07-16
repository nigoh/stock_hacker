# stock_hacker — 日本株 総合分析リポジトリ

日本株（東証上場株式）に関する分析を一手に担うためのリポジトリ。
知識ベース（`knowledge/`、73文書）を土台に、分析ライブラリ（`analysis/stocklib`）・CLI・スキル/エージェント/コマンド/hooks を備えた「日本株解析のプロフェッショナル」環境。

## リポジトリ構造

```
stock_hacker/
├── CLAUDE.md              # このファイル（Claude Code 向けガイド）
├── README.md              # 人間向け概要
├── requirements.txt       # 依存パッケージ（pandas/numpy/yfinance/pytest）
├── .claude/               # Claude Code 設定
│   ├── settings.json      # 権限・hooks（SessionStart / PostToolUse）
│   ├── skills/            # スキル6種（analyze-stock 等、下表参照）
│   ├── agents/            # サブエージェント4種（stock-analyst 等）
│   └── commands/          # スラッシュコマンド8種（/analyze、/review-report 等）
├── knowledge/             # 日本株ナレッジベース（Markdown、73文書）
│   ├── 00-index.md        # 全文書の索引（必ず最新に保つ）
│   ├── market-structure/  # 市場制度・取引所・売買の仕組み
│   ├── history/           # 日本株市場の歴史
│   ├── math/              # 数学・統計・クオンツ理論
│   ├── fundamental/       # ファンダメンタル分析
│   ├── technical/         # テクニカル分析
│   ├── macro/             # マクロ経済・金融政策・為替
│   ├── derivatives/       # 先物・オプション・デリバティブ
│   ├── regulation-tax/    # 規制・税制・NISA・開示制度
│   ├── data-sources/      # データソース・API・ツール
│   └── strategies/        # 投資戦略・ファクター・イベントドリブン
├── analysis/              # 分析コード（Python 3.11+）
│   ├── stocklib/          # 共通ライブラリ（data/indicators/metrics/backtest/report）
│   ├── analyze_stock.py   # CLI: 個別銘柄の総合分析
│   ├── screen.py          # CLI: 銘柄スクリーニング
│   ├── compare.py         # CLI: 複数銘柄の相対比較・相関
│   ├── run_backtest.py    # CLI: 戦略バックテスト
│   ├── universe/          # ユニバース定義（liquid30.csv: code,name,sector、2025年時点）
│   └── tests/             # pytest（python3 -m pytest analysis/tests）
├── scripts/               # hooks 用スクリプト
│   ├── session_start.sh   # SessionStart: 依存導入・ディレクトリ作成・環境文脈の注入
│   └── check_knowledge_index.py  # PostToolUse: knowledge 文書の索引未反映を検出
├── reports/               # レポート出力先（gitignore、.gitkeep のみコミット）
└── data/                  # ローカルデータ（gitignore対象。data/cache/ に価格CSVキャッシュ）
```

## 分析環境の使い方

### 分析 CLI（リポジトリルートから実行）

| 目的 | コマンド例 | 出力 |
|---|---|---|
| 個別銘柄の総合分析 | `python3 analysis/analyze_stock.py 7203 --period 2y --benchmark ^N225` | `reports/analyze-7203-<日付>.md`（パスを stdout に出力） |
| 銘柄スクリーニング | `python3 analysis/screen.py --universe analysis/universe/liquid30.csv --rsi-below 30 --price-above-sma 200` | 結果テーブルを stdout、`reports/screen-<日付>.md` |
| 複数銘柄の比較 | `python3 analysis/compare.py 7203 6758 9984 --period 1y` | 相対パフォーマンス・相関のレポート（`reports/compare-...`） |
| 戦略バックテスト | `python3 analysis/run_backtest.py --strategy ma_cross --code 7203 --fast 25 --slow 75 --cost-bps 10` | バックテスト統計レポート（`reports/backtest-...`） |

- 銘柄コードは4桁数字で渡す（内部で yfinance の `7203.T` に正規化）。`^N225`・`USDJPY=X` などの指数・為替ティッカーはそのまま渡せる。
- 価格データは `data/cache/` に CSV でキャッシュされる（gitignore 済み）。
- 共通ロジックは `analysis/stocklib/`（`data.py` / `indicators.py` / `metrics.py` / `backtest.py` / `report.py`）にある。新規スクリプトは車輪の再発明をせず stocklib を再利用する。
- テストは `python3 -m pytest analysis/tests` で実行できる。

### `--synthetic` フラグ

全 CLI 共通のオプション。ネットワーク不要の合成データ（シード固定の幾何ブラウン運動 + ボラティリティクラスタ）で全機能が動く。yfinance への接続に失敗したら `--synthetic` を付けて再実行すること。ただし合成データで作ったレポートには**「合成データによる手法デモであり実データではない」ことを必ず明記する**。

### J-Quants 接続（オプション、実データ全銘柄対応）

環境変数 `JQUANTS_REFRESH_TOKEN` にリフレッシュトークン（https://jpx-jquants.com/ の無料プラン登録で発行、有効期限約1週間）を設定すると、`stocklib.jquants` 経由で JPX 公式の J-Quants API が使える。`fetch_listed_info()` が全上場銘柄の一覧（コード・社名・33業種等）を返すので、liquid30 を超える**全銘柄スクリーニングのユニバース CSV 構築**に使える。`fetch_daily_quotes()` は `fetch_prices` と同じ OHLCV DataFrame 形式で日足を返す（分割・併合調整済み系列を優先。Free プランは12週間遅延データ、2025年時点）。トークン未設定・期限切れ時は `JQuantsAuthError` が導入手順つきのメッセージを出す。詳細・注意点（5桁コード、配当調整の非互換等）は `knowledge/data-sources/data-apis-and-tools.md` の J-Quants 節を参照。テストは `analysis/tests/test_jquants.py`（ネットワーク不要のモックで検証）。

### スラッシュコマンド・スキル・エージェントの一覧と使い分け

コマンドは対応するスキルを起動する薄いラッパー。手順の正はスキル本文（`.claude/skills/*/SKILL.md`）にある。

| コマンド | スキル | 用途 |
|---|---|---|
| `/analyze 7203` | analyze-stock | 個別銘柄1つの総合分析（テクニカル+ファンダ+リスク） |
| `/screen RSI30以下` | screen-market | 条件に合う銘柄の抽出（ユニバース: liquid30.csv） |
| `/compare 7203 6758` | compare-stocks | 指定した複数銘柄の相対パフォーマンス・相関比較 |
| `/backtest ゴールデンクロス 7203` | backtest-strategy | 売買ルールのバックテストと統計的検証 |
| `/market` | market-review | 市場全体（指数・為替・米国市場）の市況レビュー |
| `/learn 空売り規制` | knowledge-doc | knowledge/ への文書追加・更新（索引反映まで） |
| `/kb PERとPBRの関係` | （スキルなし） | ナレッジベースを検索し出典パス付きで回答 |
| `/review-report reports/backtest-....md` | （スキルなし。risk-officer に委譲） | レポートの敵対的レビュー（引数省略時は reports/ の最新ファイル） |

使い分けの原則: 1銘柄の深掘り → analyze-stock、複数銘柄の横比較 → compare-stocks、条件による絞り込み → screen-market、売買ルールの検証 → backtest-strategy、市場全体 → market-review、知識の追加 → knowledge-doc。**重要なレポートを外部共有・意思決定に使う前は必ず `/review-report` を通す**（risk-officer による品質ゲート。統計的誤り・ルックアヘッド・投資助言化・合成データ偽装を検出）。

サブエージェント（`.claude/agents/`。重い作業の委譲先）:

| エージェント | 役割 |
|---|---|
| stock-analyst | 個別銘柄の深掘り分析とレポート作成 |
| quant-researcher | 戦略研究・バックテスト・統計的検証（t統計量・多重検定・IS/OOS） |
| risk-officer | 成果物（レポート・スクリプト）の敵対的レビュー。外部共有前の品質ゲート（`/review-report` から起動） |
| knowledge-curator | knowledge/ の保守（索引整合・重複統合・陳腐化検出） |

### hooks（`.claude/settings.json`）

| フック | スクリプト | 動作 |
|---|---|---|
| SessionStart | `scripts/session_start.sh` | 依存パッケージの導入確認、`reports/`・`data/cache/` の作成、stocklib のスモークチェック、環境の要点をセッション文脈に注入 |
| PostToolUse (Write\|Edit) | `scripts/check_knowledge_index.py` | knowledge/ 配下の Markdown が `00-index.md` から参照されていないと exit 2 でブロックし、索引反映を促す |
| PostToolUse (Write\|Edit) | `scripts/check_report_disclaimer.py` | reports/ 配下の Markdown に免責文（「投資助言ではありません」または「免責」）が無いと exit 2 でブロックし、`stocklib.report.DISCLAIMER` の追記を促す |

### 免責（必須）

本環境の出力は投資助言ではなく分析支援。`reports/` に生成するレポートには必ず免責の一文を入れる（`stocklib.report.DISCLAIMER` を利用できる）。

## ナレッジベースの規約

- 1ファイル = 1トピック。ファイル名は英語ケバブケース（例: `nikkei-225-mechanics.md`）。
- 本文は日本語。冒頭に `# タイトル` と3行以内の要約を置く。
- 数式は LaTeX 記法（`$...$` / `$$...$$`）で書く。
- 文書を追加・更新したら **必ず `knowledge/00-index.md` に反映する**。
- 具体的な数値（税率、制度の閾値など）には「〜年時点」を付記する。制度は変わる。
- 投資助言ではなく知識の整理であることを意識し、断定的な将来予測は書かない。

## コード規約（analysis/ 以下）

- Python 3.11+、型ヒント必須。
- データ取得は `data-sources/` の文書に記載された公式・無料ソースを優先。
- ノートブックより素のスクリプト + Markdown レポートを優先。

## よくあるタスク

- 「〜について調べて」→ まず `knowledge/00-index.md` を読み、該当文書を参照。無ければ新規作成して索引に追加（knowledge-doc スキル / `/learn`）。
- 「〜を分析して」→ まず既存の CLI・スキル（上記「分析環境の使い方」）で対応できないか確認する。できない場合のみ `analysis/` にスクリプトを作り（stocklib を再利用）、結果は Markdown で要約。
- 銘柄コードは4桁数字（例: 7203 トヨタ自動車）。yfinance では `7203.T` 形式（CLI は4桁のまま受け付けて内部で正規化）。

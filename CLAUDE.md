# stock_hacker — 日本株 総合分析リポジトリ

日本株（東証上場株式）に関する分析を一手に担うためのリポジトリ。
知識ベース（`knowledge/`、90文書）を土台に、分析ライブラリ（`analysis/stocklib`）・CLI・スキル/エージェント/コマンド/hooks を備えた「日本株解析のプロフェッショナル」環境。

## リポジトリ構造

```
stock_hacker/
├── CLAUDE.md              # このファイル（Claude Code 向けガイド）
├── README.md              # 人間向け概要
├── requirements.txt       # 依存パッケージ（pandas/numpy/yfinance/pytest）
├── .claude/               # Claude Code 設定
│   ├── settings.json      # 権限・hooks（SessionStart / PostToolUse）
│   ├── skills/            # スキル18種（analyze-stock、mape-night 等、下表参照）
│   ├── agents/            # サブエージェント4種（stock-analyst 等）
│   └── commands/          # スラッシュコマンド20種（/analyze、/portfolio、/mape-night 等）
├── knowledge/             # 日本株ナレッジベース（Markdown、90文書）
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
│   ├── stocklib/          # 共通ライブラリ（data/indicators/metrics/backtest/report/portfolio/signals/charts/edinet/fundamentals/jquants/journal/adr/currency/planning/income/performance/forecast）
│   ├── analyze_stock.py   # CLI: 個別銘柄の総合分析
│   ├── screen.py          # CLI: 銘柄スクリーニング
│   ├── compare.py         # CLI: 複数銘柄の相対比較・相関
│   ├── run_backtest.py    # CLI: 戦略バックテスト
│   ├── portfolio_review.py # CLI: 保有ポートフォリオの評価・リスクレビュー
│   ├── daily_brief.py     # CLI: 市況+ウォッチリストのデイリーブリーフ
│   ├── fundamentals_report.py # CLI: 業績推移・決算分析
│   ├── research_journal.py # CLI: リサーチジャーナル（仮説の記録・期日確認・検証）
│   ├── overnight_forecast.py # CLI: 夜間フォーキャスト（翌営業日の機械予想→翌日答え合わせ→forecasts/台帳に蓄積→的中率・Brier・較正の集計）
│   ├── adr_parity.py      # CLI: ADRパリティ・モニタ（東証終値×ADR終値×ドル円の乖離）
│   ├── asset_plan.py      # CLI: 資産形成プランニング（積立予測・目標逆算・進捗確認・取り崩し、NISA非課税比較。ネットワーク不要）
│   ├── income_report.py   # CLI: 配当インカム・レポート（TTM実績配当・YOC・税引後手取り・NISA非課税メリット）
│   ├── tax_report.py      # CLI: 課税口座の含み損益 税価値ビュー（損出し・損益通算の判断材料の機械的整理）
│   ├── performance_report.py # CLI: 実運用パフォーマンス（取引履歴CSV → 金額加重リターン XIRR・損益内訳・ベンチマーク比較）
│   ├── build_universe.py  # CLI: J-Quants の全上場銘柄から screen.py 互換のユニバース CSV を構築（要 JQUANTS_API_KEY）
│   ├── universe/          # ユニバース定義（liquid30.csv: code,name,sector、2025年時点。adr_map.csv: ADR対応表）
│   ├── templates/         # portfolio/watchlist/transactions の CSV テンプレート
│   └── tests/             # pytest（python3 -m pytest analysis/tests）
├── scripts/               # hooks 用スクリプト
│   ├── session_start.sh   # SessionStart: 依存導入・ディレクトリ作成・環境文脈の注入
│   └── check_knowledge_index.py  # PostToolUse: knowledge 文書の索引未反映を検出
├── mape/                  # MAPE-K 夜間セルフ改善の決定論スクリプト（Monitor/Analyze/Plan＋サーキットブレーカー。docs/mape-k.md）
│   ├── knowledge/         # MAPE-K 共有ナレッジ K（BACKLOG/PROGRESS/POLICY/HEALTH。knowledge/ とは別物）
│   ├── state/             # 中間成果物（証跡）
│   └── tests/run.sh       # 決定論部分の自己テスト（pytest の test_mape.py から実行）
├── docs/                  # 運用ガイド（getting-started.md: 資産形成の通し順路、data-sources.md: 無料でデータを見る/取る実践ガイド、automation.md: デイリーブリーフの自動実行、overnight-forecast.md: 夜間フォーキャストの自動運用、mape-k.md: MAPE-K 夜間セルフ改善の設計と運用）
├── journal/               # リサーチジャーナル（分析仮説の記録と事後検証。git 管理対象。書式は journal/README.md）
│   └── <YYYY>/            # エントリ（<YYYY-MM-DD>-<slug>.md、YAML frontmatter + 本文）
├── forecasts/             # 夜間フォーキャストの実績台帳（ledger.csv。翌営業日予想と答え合わせの蓄積。git 管理対象。書式は forecasts/README.md）
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
| ポートフォリオ評価 | `python3 analysis/portfolio_review.py --file data/portfolio.csv --period 1y` | 損益・セクター配分・加重β・VaR・HHI のレポート（`reports/portfolio-...`） |
| デイリーブリーフ | `python3 analysis/daily_brief.py --watchlist data/watchlist.csv` | 市況サマリー+ウォッチ銘柄シグナル（`reports/brief-...`） |
| 業績・決算分析 | `python3 analysis/fundamentals_report.py 7203 --years 5` | 売上/利益推移・CAGR・マージンのレポート（`reports/fundamentals-...`） |
| ADRパリティ・モニタ | `python3 analysis/adr_parity.py 7203`（単銘柄）/ `--all`（`analysis/universe/adr_map.csv` の全銘柄） | 東証終値×ADR終値×ドル円の理論価格・乖離%・円換算ADR価格のレポート（`reports/adr-...`。終値の暦日ずれの注意付き） |
| リサーチジャーナル | `python3 analysis/research_journal.py new --codes 7203 --title "..." --direction up --review-days 60`（他に `due` / `verify <path>` / `list`） | `journal/<YYYY>/` に仮説エントリを生成（記録時点の終値を自動スナップショット）。`verify` が hit/miss/mixed を判定し検証結果を追記 |
| 夜間フォーキャスト | `python3 analysis/overnight_forecast.py run`（他に `forecast` / `grade` / `calibration`。ユニバースは `--universe CSV`） | 翌営業日の機械予想（RSI・移動平均の並び・モメンタムの固定重み合成。方向/上昇確率/予想リターン/ATRベースの予想レンジ）を `forecasts/ledger.csv` に蓄積し、翌日実績で答え合わせ（方向的中・Brier・レンジ的中）。`run` は「前回の答え合わせ→翌営業日予想」を一括。`calibration` は蓄積台帳から的中率・Brier・較正曲線・銘柄別成績を集計（`reports/forecast-...`。予想は将来の断定でも助言でもない旨・少数標本の統計的不安定さの注記付き。RESULT 行・exit code の自動実行契約あり） |
| 資産形成プランニング | `python3 analysis/asset_plan.py project --monthly 50000 --years 20 --return 5 --nisa`（他に `goal`: 目標額から必要積立額を逆算 / `progress`: 現在資産+積立ペースでの目標到達確認（要求リターン逆算・到達確率） / `decumulate`: 定額・定率取り崩し） | 決定論的複利+モンテカルロのファンチャート付きレポート（`reports/plan-<サブコマンド>-<日付>.md`。ネットワーク不要・価格データ不使用。率は%表記、想定リターンはユーザーの仮定である旨を自動明記） |
| 配当インカム・レポート | `python3 analysis/income_report.py --file data/portfolio.csv --period 1y` | 銘柄別の年間受取配当見込み（TTM実績ベース）・YOC・時価利回り・口座区分別の税引後手取り・NISA非課税メリット試算のレポート（`reports/income-...`。TTM≠会社予想・減配リスク・株式数比例配分方式の注記付き。`manual_price` 行は配当集計の対象外） |
| 含み損益の税価値ビュー | `python3 analysis/tax_report.py --file data/portfolio.csv --period 1y` | 課税口座の銘柄別含み損益と「実現した場合の税価値（試算）」= 含み損 × 20.315%（2025年時点）、NISA口座（損益通算・繰越控除の対象外）との対比、実務上の注意（同日買戻し・申告要件・行動バイアス）のレポート（`reports/tax-...`。売る銘柄の判断はしない条件付き試算） |
| 実運用パフォーマンス（XIRR） | `python3 analysis/performance_report.py --file data/transactions.csv`（配当込み比較は `--benchmark 1306.T`） | 取引履歴CSV（買付・売却・配当・入出金。テンプレート: `analysis/templates/transactions-example.csv`）から、入出金を調整した実績年率＝金額加重リターン（MWR = XIRR）・期間損益の内訳（実現+未実現+受取配当）・同じキャッシュフローをベンチマークに投じた場合の比較のレポート（`reports/performance-...`。入出金行があれば口座モード、約定のみならポジションモードに自動判定。既定の ^N225 は配当を含まない価格指数である旨の注記付き） |

- 上表のオプションは代表例。全オプションは各 CLI の `--help` で確認できる。
- `--horizon short|mid|long`（analyze_stock）: 投資時間軸フレーム。short=〜数週間（`--period` 未指定時 6mo）/ mid=数ヶ月〜1年（2y）/ long=数年〜（5y）。指定すると該当時間軸の「視点」節がレポートに追加され（short: 5/25日線・RSI・ATR倍数のストップ目安・出来高急増・直近20日高安値、mid: 25/75/200日線の並び・3/6/12ヶ月モメンタム・52週高値からの距離、long: 年率リターン/ボラ・最大DD・配当利回り・PBR/PER の長期文脈・積立適性）、出力ファイル名は `analyze-<code>-<horizon>-<日付>.md` になる。省略時は従来どおりの全部入り。
- `--strategy dca`（run_backtest）: 売買シグナル戦略ではなく毎月定額買付（ドルコスト平均法）の積立バックテスト。`--monthly`（毎月の買付金額、既定 30000円）と `--day-of-month`（目標買付日、非営業日は翌営業日に繰越、既定 1）で設定し、同一総投資額の期初一括投資との比較（最終評価額・損益率・平均取得単価等）をレポート化する。`--split` / `--sweep` / `--in-currency` は dca では使用不可（円建てのみ）。
- 保有 CSV の任意列 `account`（portfolio_review。`nisa_tsumitate` / `nisa_growth` / `taxable`、空欄・列なしは taxable 扱い）: 1銘柄でも指定があるとレポートに「NISA口座状況」節が追加される——口座区分別の内訳（簿価・評価額・損益）、年間投資枠（`acquired_date` の暦年で集計。つみたて投資枠120万円・成長投資枠240万円/年）と生涯投資枠（1,800万円、うち成長投資枠1,200万円。簿価残高方式）の使用率、非課税メリット推計（NISA含み益 × 20.315%、2025年時点の課税口座税率との比較。含み損なら0円）。いずれも2024年開始の新NISA制度の値。保有中の銘柄の簿価のみからの集計で、売却済み分・投資信託の買付を含まないため金融機関側の枠残高管理値とはずれうる。
- `--in-currency USD|EUR|GBP`（analyze_stock / compare / run_backtest / portfolio_review / screen / daily_brief 共通、海外投資家視点。`--in-usd` は `--in-currency USD` の後方互換エイリアス）: 基準通貨建て評価を併記する。換算はクロス円レート（USDJPY=X・EURJPY=X・GBPJPY=X、`stocklib.currency.SUPPORTED_CURRENCIES` のホワイトリスト）の同日終値・ヘッジなし近似。バックテストでは売買シグナルは常に円建て価格で計算し、確定した日次リターンを恒等式 $(1+r^{B})=(1+r^{JPY})/(1+r^{FX})$ で換算する。スクリーニング（screen）では各銘柄の O/H/L/C を換算してから RSI・SMA・リターン条件を評価する（PER/PBR/配当利回りは通貨に依存しない比率、出来高は無変換）。デイリーブリーフ（daily_brief）では市況テーブルに基準通貨建て ^N225 行を併記する（RESULT 行・exit code の自動実行契約は不変）。ポートフォリオでは基準通貨建ての評価額・年率ボラ・VaR は全銘柄で計算し、保有 CSV の任意列 `fx_at_cost`（取得時のクロス円レート、円/基準通貨。指定した基準通貨のレートで入力する）がある銘柄は損益も基準通貨建てで併記して「うち株価要因（円建て損益÷直近為替）」「うち為替要因（残差）」に分解する。`fx_at_cost` 未入力の銘柄の損益は円建てのみ（現在為替での近似換算はせず、レポートに「取得時為替未入力のため円建てのみ」と注記される）。
- 銘柄コードは4桁数字で渡す（内部で yfinance の `7203.T` に正規化）。`^N225`・`USDJPY=X` などの指数・為替ティッカーはそのまま渡せる。
- 価格データは `data/cache/` に CSV でキャッシュされる（gitignore 済み）。
- 既定のデータソースは yfinance（非公式 API）。日本株では分割・配当調整の不備が起きうるため、レポートで異常な騰落やギャップを見たらまずデータ品質を疑う。制約の詳細は `knowledge/data-sources/data-apis-and-tools.md` の「yfinance：手軽さと引き換えのリスク」「調整後株価とコーポレートアクションの注意点」を参照し、重要なレポートにはデータソースと取得日を明記する。
- 価格ソースは切替可能: 価格系列を扱う各 CLI の `--source jquants`（または環境変数 `STOCK_HACKER_SOURCE=jquants`）で日足 OHLCV を J-Quants から取得できる（既定 `yfinance`）。優先順位は `--source` > `STOCK_HACKER_SOURCE` > 既定。要 `JQUANTS_API_KEY`（V2・APIキー方式、2026年時点）・日足のみ・無料プランは12週間遅延（当日ブリーフ用途には不向き）。`^N225` 等の指数・`USDJPY=X` 等の為替は J-Quants 非対応のため自動で yfinance にフォールバックし、`fetch_info`（PER/PBR 等）は常に yfinance を使う（切り替わるのは OHLCV のみ）。実装は `stocklib.data.fetch_prices(source=...)` / `resolve_source` / `set_default_source` / `add_source_argument`。
- 共通ロジックは `analysis/stocklib/`（`data.py` / `indicators.py` / `metrics.py` / `backtest.py` / `report.py`）にある。新規スクリプトは車輪の再発明をせず stocklib を再利用する。
- テストは `python3 -m pytest analysis/tests` で実行できる。
- analyze/compare/backtest はローソク足・相対パフォーマンス・資産曲線のチャート PNG を `reports/img/` に生成しレポートへ埋め込む（`--no-charts` で無効化。matplotlib 未導入時は自動でチャートなしに縮退）。
- **個人データはコミットされない設計**: 保有情報 `data/portfolio.csv`・ウォッチリスト `data/watchlist.csv` は gitignore 対象の `data/` に置く。テンプレートは `analysis/templates/` にある。
- EDINET の法定開示書類（有報等）の検索・取得には環境変数 `EDINET_API_KEY`（2024年時点で API v2 はキー必須）を設定する。未設定でも業績分析は yfinance の財務データで動作する。

### `--synthetic` フラグ

価格データを使う全 CLI 共通のオプション（`asset_plan.py` はそもそも価格データ・ネットワークを使わないため対象外）。ネットワーク不要の合成データ（シード固定の幾何ブラウン運動 + ボラティリティクラスタ）で全機能が動く。yfinance への接続に失敗したら `--synthetic` を付けて再実行すること。ただし合成データで作ったレポートには**「合成データによる手法デモであり実データではない」ことを必ず明記する**。

### J-Quants 接続（オプション、実データ全銘柄対応）

環境変数 `JQUANTS_API_KEY` に API キー（https://jpx-jquants.com/ の無料プラン登録後にダッシュボードで発行。**V2・APIキー方式、無期限**。2025年12月の V2 移行でリフレッシュトークン方式は廃止。2026年時点）を設定すると、`stocklib.jquants` 経由で JPX 公式の J-Quants API（V2）が使える。`fetch_listed_info()`（V2 `/equities/master`）が全上場銘柄の一覧（コード・社名・33業種等。V2 短縮カラムは V1 相当の安定名に正規化して返す）を返すので、liquid30 を超える**全銘柄スクリーニングのユニバース CSV 構築**に使える（`python3 analysis/build_universe.py` が screen.py 互換の `code,name,sector` CSV を生成。`--market` / `--sector33` で絞り込み可）。`fetch_daily_quotes()`（V2 `/equities/bars/daily`）は `fetch_prices` と同じ OHLCV DataFrame 形式で日足を返す（分割・併合調整済み系列 `AdjC` 等を優先。Free プランは12週間遅延データ、2026年時点）。**価格取得 CLI で J-Quants を使うには `--source jquants` か `STOCK_HACKER_SOURCE=jquants` を指定する**（`fetch_prices` が env/引数を解決し、指数・為替は yfinance にフォールバック。上記「価格ソースは切替可能」参照）。API キーは `.env`（gitignore 済み、雛形は `.env.example`）に置き `set -a && source .env && set +a` で読み込む運用を推奨。API キーをコード・レポート・コミットに含めないこと。未設定・無効時は `JQuantsAuthError` が導入手順つきのメッセージを出す。詳細・注意点（5桁コード、配当調整の非互換、V2 の全エンドポイント・プラン等）は `knowledge/data-sources/data-apis-and-tools.md` の J-Quants 節と `knowledge/data-sources/market-data-apis-catalog.md` を参照。テストは `analysis/tests/test_jquants.py`（ネットワーク不要のモックで検証）。

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
| `/portfolio [ファイル]` | portfolio-review | 保有ポートフォリオの損益・リスク・集中度レビュー |
| `/brief` | daily-brief | 市況+ウォッチリストのシグナル定点観測 |
| `/earnings 7203` | earnings-analysis | 業績推移・決算の時系列深掘り |
| `/journal 7203 決算後の上方修正期待` | research-journal | 分析仮説を journal/ に記録（終値スナップショット・反証条件付き） |
| `/journal-review` | journal-review | 検証期日が来た仮説の機械判定（hit/miss/mixed）と振り返り |
| `/overnight` | overnight-forecast | 翌営業日の機械予想生成→翌日答え合わせ→forecasts/台帳に蓄積→的中率・Brier・較正の集計（夜間フォーキャスト） |
| `/plan 毎月5万円を20年` | asset-planning | 資産形成プランニング（積立シミュレーション・目標逆算・取り崩し・新NISA非課税メリット試算） |
| `/income [ファイル]` | dividend-income | 保有銘柄の配当インカム集計（年間受取見込み・YOC・税引後手取り・NISA非課税メリット） |
| `/tax [ファイル]` | tax-view | 課税口座の含み損益 税価値ビュー（損出し・損益通算の判断材料の機械的整理。売る銘柄の判断はしない） |
| `/performance [ファイル]` | performance-review | 取引履歴からの実運用パフォーマンス測定（入出金調整後の実績年率 XIRR・損益内訳・ベンチマーク比較） |
| `/kb PERとPBRの関係` | （スキルなし） | ナレッジベースを検索し出典パス付きで回答 |
| `/review-report reports/backtest-....md` | （スキルなし。risk-officer に委譲） | レポートの敵対的レビュー（引数省略時は reports/ の最新ファイル） |
| `/mape-night` | mape-night | MAPE-K 夜間周回（M→A→P）。リポジトリ健全性の改善案をリスク3分類のチェックリストで GitHub 計画イシューに掲示（docs/mape-k.md） |
| `/mape-execute` | mape-execute | MAPE-K の Execute。計画イシューのチェック済み・未着手を1周1件だけ安全に実装（pytest 緑→ドラフト PR／赤→破棄） |

使い分けの原則: 1銘柄の深掘り → analyze-stock、業績の時系列深掘り → earnings-analysis、複数銘柄の横比較 → compare-stocks、条件による絞り込み → screen-market、売買ルールの検証 → backtest-strategy、市場全体 → market-review、保有銘柄のレビュー → portfolio-review、ウォッチ銘柄の定点観測 → daily-brief、知識の追加 → knowledge-doc、仮説の記録 → research-journal、期日が来た仮説の検証 → journal-review、翌営業日の機械予想と答え合わせの継続測定 → overnight-forecast、積立額・目標額・取り崩し・NISA枠の試算 → asset-planning、保有銘柄の配当収入の集計 → dividend-income、含み損益の税務整理 → tax-view、取引履歴からの実績リターン（XIRR）測定 → performance-review、リポジトリ自身の健全化（テスト・索引整合・コード品質の夜間セルフ改善）→ mape-night（改善案の掲示）/ mape-execute（承認済みの実装）。**重要なレポートを外部共有・意思決定に使う前は必ず `/review-report` を通す**（risk-officer による品質ゲート。統計的誤り・ルックアヘッド・投資助言化・合成データ偽装を検出）。

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

### 自動実行（Routine / cron）

デイリーブリーフ（`daily_brief.py` / `/brief`）は定期自動実行を想定した機械可読な契約を持つ: stdout の最終行に `RESULT signals=<検出シグナル総数> watch=<取得成功数>/<ウォッチリスト総数> data=<real|synthetic|unavailable>` を出力し、実データが全滅した場合は exit 2（レポート非生成）、CSV 不正等は exit 1。セットアップ（ローカル cron / Claude Code の Routine、exit code と RESULT 行による通知の振り分け、シグナル検出時のみ通知する原則）は `docs/automation.md` を参照。**自動実行で実データが取れないときに `--synthetic` で代替して「今日の市況」のように見せることは禁止**（データ取得不可を明示して静かに終了する）。

夜間フォーキャスト（`overnight_forecast.py` / `/overnight`）も同じ機械可読契約（`RESULT` 行 + exit code 2 で実データ全滅）を持ち、`run`（前回予想の答え合わせ→翌営業日予想）を平日夜に定期実行して `forecasts/ledger.csv` を醸成する運用を想定する。予想モデル・スケジュール・通知の振り分け・台帳の扱いは `docs/overnight-forecast.md`、評価指標（方向的中率・Brier・較正）の理論は `knowledge/math/forecast-evaluation-and-calibration.md` を参照。ここでも合成データで予想・答え合わせを偽装しない原則は同じ。

MAPE-K 夜間セルフ改善（`mape/` / `/mape-night` / `/mape-execute`）は**リポジトリ自身の健全性**を夜間に改善する仕組み（分析ではない）。「安く読んで考える」Monitor→Analyze→Plan（決定論 Bash・読み取り専用）と「壊しうる」Execute（Claude スキル・1周1件・pytest 緑→ドラフト PR）を分離し、承認ゲートを GitHub 計画イシューのリスク3分類チェックリスト（自動/承認/相談）として挟む。監視シグナルは pytest ゲート・knowledge 索引整合・テストの無い stocklib モジュール・TODO・最長 SKILL 行数など。共有ナレッジ K は `mape/knowledge/`（日本株ナレッジベース `knowledge/` とは別物）。決定論部分は `mape/tests/run.sh` を `analysis/tests/test_mape.py` 経由で pytest（＝品質ゲート）に配線して回帰を守る。設計・分類・ガードレール・スケジュールは `docs/mape-k.md` を参照。投資助言化・実データ/実発注・秘密/課金は consult、合成データで実データを偽装する提案は却下、という stock_hacker の不変条件を分類に織り込んである。

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

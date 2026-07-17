# ゼロから始める資産形成ガイド — リスク許容度 → 計画 → 新NISA → 定点観測

「明日から資産形成を始める」個人が、このリポジトリを使って運用を立ち上げるための通しガイド。
何を先に決め、どのファイルを作り、何を定期実行するかを、具体的なコマンドとファイルパス付きで順路として示す。

> **このガイドの範囲（最初に明記）**
> 本ガイドは**どの商品（銘柄・投信）を買うか、いつ買うかを扱わない**。それらはユーザー自身の判断であり、
> 本リポジトリが提供するのは判断材料（数値・枠組み・トレードオフ）のみである。以下の全ステップの出力は
> 投資助言ではなく分析支援であり、具体的な商品名・銘柄の推奨は一切含まない。

## 全体の順路

| ステップ | 決めること・作るもの | 使う入口 |
|---|---|---|
| [1. 前提整理](#step-1-前提整理--投資に回してよいお金を確定する) | 投資に回してよい金額（生活防衛資金・近い将来の支出を除いた残り） | `knowledge/strategies/household-risk-capacity-and-allocation.md` |
| [2. 計画](#step-2-計画--積立額と期間の当たりを付ける) | 毎月の積立額と期間の当たり、想定リターンの根拠 | `/plan`（`analysis/asset_plan.py`） |
| [3. 制度](#step-3-制度--新nisaの枠設計) | 新NISAの枠の使い方（iDeCo併用の要否） | `knowledge/regulation-tax/nisa-practical-strategies.md` |
| [4. セットアップ](#step-4-セットアップ--自分のデータファイルを作る) | `data/portfolio.csv`・`data/watchlist.csv` | `analysis/templates/` |
| [5. 定点観測](#step-5-運用の定点観測--見る日を決めて見すぎない) | 月次レビューと自動ブリーフのリズム、仮説の記録 | `/portfolio`・`/brief`・`/journal` |
| [6. データの限界](#step-6-データの限界を知る) | 数値を解釈する前の前提知識 | `knowledge/data-sources/data-apis-and-tools.md` |

順序に意味がある。**商品や銘柄の検討は、1〜3（いくら・どのくらいの期間・どの器で）が決まった後**の話であり、
そこから先は本ガイドの範囲外である。

## Step 1: 前提整理 — 投資に回してよいお金を確定する

最初に決めるのは「何を買うか」ではなく「いくらまでなら価格変動に晒してよいか」。

1. **生活防衛資金を先に確保する。** 生活費の3〜6ヶ月分（収入が不安定な自営業等は6〜12ヶ月分）を現預金で置くのが一般的な目安とされる。これは下落局面での「生活費のための強制売却」を防ぎ、積立を継続する心理的余裕を買う保険料である。
2. **近い将来の支出を除外する。** 数年内に使途と時期が確定している資金（住宅頭金・学費・車の買い替え等）は価格変動資産に置かない、が原則的な整理である。
3. **リスク許容度（心理）とリスク受容力（財務）を区別する。** 前者は「含み損を抱えても継続できるか」、後者は「損失が出ても生活設計が壊れないか」。実務上の制約は**二つのうち低い方**で決まる。

枠組みの詳細（人的資本を含めた総資産視点、コア・サテライト構成、リバランスの規律）は
[`knowledge/strategies/household-risk-capacity-and-allocation.md`](../knowledge/strategies/household-risk-capacity-and-allocation.md) を読む。
Claude Code 上なら `/kb リスク許容度とリスク受容力の違い` のように質問すれば、出典パス付きで要点を引ける。

## Step 2: 計画 — 積立額と期間の当たりを付ける

Step 1 で決めた「投資に回してよい金額」を、積立額×期間×想定リターンの試算に落とす。
`/plan` コマンド（または `analysis/asset_plan.py` を直接実行）はネットワーク不要・価格データ不使用で、
決定論的複利+モンテカルロのファンチャート付きレポートを `reports/plan-<サブコマンド>-<日付>.md` に生成する。

```bash
# 毎月5万円を20年積み立てたら？（年率5%・ボラ15%を仮定、NISA非課税メリット併記）
python3 analysis/asset_plan.py project --monthly 50000 --years 20 --return 5 --vol 15 --nisa

# 25年で3,000万円に届くには毎月いくら必要？（目標からの逆算）
python3 analysis/asset_plan.py goal --target 30000000 --years 25 --return 5 --vol 15

# 信託報酬等のコストを想定リターンから控除して試算する場合
python3 analysis/asset_plan.py project --monthly 50000 --years 20 --return 5 --cost 0.5
```

Claude Code 上では `/plan 毎月5万円を20年` のように依頼すれば、パラメータの聞き取りから実行までを対話で行う。

**想定リターン（`--return`）はユーザー入力の仮定であり、保証ではない。** 根拠を必ず問い直すこと:

- 長期データ（Dimson–Marsh–Staunton、1900年以降、2024年時点のイヤーブック）では世界株の実質リターンは年率約5%、日本株は約4%（幾何平均）と報告されているが、これは数十年単位のばらつきを伴う**事後平均**である。
- 前提を±1〜2ポイント動かした感応度で幅を見る（`--return 3` と `--return 7` で再実行して比較する）。
- コストは複利で効く。年0.5%のコスト差は30年で終価を約13%押し下げる。

複利の数理・積立と一括のトレードオフ・「長期ならリスクは消える」という誤解への注意は
[`knowledge/strategies/long-term-wealth-building.md`](../knowledge/strategies/long-term-wealth-building.md) を参照。

## Step 3: 制度 — 新NISAの枠設計

計画ができたら、それをどの「器」（口座）で実行するかを決める。数値はいずれも2025年時点。

- 新NISA（2024年〜）: つみたて投資枠は年120万円（対象は金融庁基準を満たす投信等。個別株は買えない）、成長投資枠は年240万円（個別株・ETF・投信）、合計年360万円。生涯投資枠1,800万円（うち成長投資枠は上限1,200万円）。
- 非課税の価値は年率 $r$ で $T$ 年運用して売却する場合 $\Delta = \tau\left[(1+r)^T - 1\right]$（$\tau = 20.315\%$、2025年時点の税率）。期間に対して凸に増えるため、**長期資金ほどNISAを優先する論拠**になる。`asset_plan.py` の `--nisa` オプションがこの比較を併記する。
- 一方、**NISA口座の損失は税務上「なかったもの」になる**（損益通算・繰越控除の対象外）。高ボラティリティの個別株を非課税口座に置くことの非対称性は、[`knowledge/regulation-tax/nisa-practical-strategies.md`](../knowledge/regulation-tax/nisa-practical-strategies.md) の第4節で定量的に整理されている。枠の使い分け・埋めるペース設計・出口戦略も同文書を読む。制度自体の詳細は [`knowledge/regulation-tax/taxation-and-nisa.md`](../knowledge/regulation-tax/taxation-and-nisa.md)。
- **枠を埋めること自体は目的ではない。** 実務の順序は（1）Step 1 の生活防衛資金と近い将来の支出を先に確保し、（2）残った入金力で無理のない年間ペースを決める。
- 老後資金専用の器としての iDeCo（拠出時所得控除・60歳までの流動性ロック）との使い分けは [`knowledge/regulation-tax/ideco-and-corporate-dc.md`](../knowledge/regulation-tax/ideco-and-corporate-dc.md) を参照。

## Step 4: セットアップ — 自分のデータファイルを作る

運用を始めたら（あるいは既存の保有がある場合）、保有情報とウォッチリストをテンプレートから作る。

```bash
cp analysis/templates/portfolio-example.csv data/portfolio.csv
cp analysis/templates/watchlist-example.csv data/watchlist.csv
# 中身を自分の保有・ウォッチ銘柄に書き換える
```

**個人データはコミットされない設計になっている**: `data/.gitignore` が `data/` 配下のファイルをすべて git 管理外にするため、
保有情報・ウォッチリストが誤ってコミット・公開されることはない。逆に、リポジトリにコミットされる `journal/`（Step 5）には
保有情報を書かず、検証可能な分析仮説だけを書く。

`data/portfolio.csv` の主な列（テンプレートに記入例あり）:

| 列 | 内容 |
|---|---|
| `code` | 銘柄コード4桁（例: 7203）。投信・現金は任意の識別子でよい（例: `my-index-fund`、`cash`） |
| `shares` / `avg_cost` | 保有数量と平均取得単価 |
| `account` | 口座区分: `nisa_tsumitate` / `nisa_growth` / `taxable`。記入すると `/portfolio` が口座区分別損益・NISA年間/生涯投資枠の使用率・非課税メリット推計を併記する |
| `manual_price` | **投信など yfinance で価格が取れない資産は、1口あたりの現在価額をここに手入力する**。現金は `avg_cost=1`・`manual_price=1` |
| `target_weight` | 目標配分（%）。記入すると `/portfolio` が目標配分からのドリフトを判定する（リバランスの規律づけ） |
| `fx_at_cost` | 任意。`--in-currency` で外貨建て評価をする場合の取得時為替 |

`data/watchlist.csv` は `code`（4桁コード。`^N225` 等の指数も可）と `note`（メモ）の2列。`/brief` の監視対象になる。
売買・入出金の記録を付けたい場合は `analysis/templates/transactions-example.csv` を `data/transactions.csv` にコピーする（任意。
付けておくと Step 5 の年1回レビューで `/performance` による実績年率 XIRR の測定に使える）。

作ったら動作確認:

```bash
python3 analysis/portfolio_review.py --file data/portfolio.csv --period 1y
# ネットワークが使えない環境では --synthetic を付けると合成データで仕組みだけ確認できる（Step 6 参照）
```

## Step 5: 運用の定点観測 — 見る日を決めて、見すぎない

立ち上げ後の運用は「決まったリズムで機械的に見る」ことが規律になる。高頻度で見るほど短期の雑音に反応しやすくなる
（[`knowledge/strategies/behavioral-finance-japan.md`](../knowledge/strategies/behavioral-finance-japan.md) の認知バイアスの枠組みを参照）。

| リズム | やること | 入口 |
|---|---|---|
| 毎営業日（自動） | 市況+ウォッチ銘柄のシグナル検出。**Routine / cron で自動化し、シグナル検出時のみ通知** | `/brief`（セットアップは [`docs/automation.md`](automation.md)） |
| 月次 | 損益・セクター配分・集中度（HHI）・NISA枠使用率・目標配分ドリフトの確認 | `/portfolio`（`python3 analysis/portfolio_review.py`） |
| 随時（分析のたび） | 検証可能な仮説を、記録時点の終値スナップショット・反証条件付きで記録 | `/journal`（`python3 analysis/research_journal.py new --codes 7203 --title "..." --direction up --review-days 60`） |
| 仮説の期日到来時 | ベンチマーク対比の超過リターンで hit/miss/mixed を機械判定し、外れから学ぶ | `/journal-review`（書式・判定基準は [`journal/README.md`](../journal/README.md)） |
| 年1回 | 現在の資産と積立ペースで目標に届いているかを確認し、Step 2 の計画を見直す。取引履歴（`data/transactions.csv`）を付けていれば、入出金を調整した実績年率（金額加重リターン XIRR）とベンチマーク比較を測定し、**Step 2 で置いた想定リターンを実績で検証する**（実績 XIRR をそのまま次の想定リターンに使わない——短期間の実績は運と地合いの寄与を含む） | `python3 analysis/asset_plan.py progress --target 30000000 --years 20 --current 5000000 --monthly 50000 --return 5` と `/performance`（`python3 analysis/performance_report.py`） |

デイリーブリーフの自動実行は機械可読な契約（stdout の `RESULT` 行と exit code）を持ち、cron / Claude Code の Routine に組み込める。
セットアップ手順・通知の振り分け（「変化なし」は通知しない原則）は [`docs/automation.md`](automation.md) を参照。
**自動実行で実データが取れないときに `--synthetic` で代替して「今日の市況」のように見せることは禁止**である。

ジャーナル（`journal/`）は「分析のやりっぱなし」を防ぐ仕組みで、記録時に終値を自動スナップショットするため後知恵での
書き換えが効かず、検証はベンチマーク対比で行われる（地合いで上がっただけを的中扱いしない）。

## Step 6: データの限界を知る

レポートの数値を解釈する前に、使用データの制約を把握しておく（詳細は
[`knowledge/data-sources/data-apis-and-tools.md`](../knowledge/data-sources/data-apis-and-tools.md)）。

- **yfinance（既定のデータソース）は非公式 API** であり、日本株では分割・配当調整の不備や欠損が起きることが知られている。レポートで異常な騰落やギャップを見たら、まずデータ品質を疑う。
- J-Quants API（JPX 公式、任意設定）は全上場銘柄に対応するが、無料プランは12週間遅延データ（2025年時点）のため直近の相場観測には使えない。
- **`--synthetic` は「手法のデモ・オフライン検証」専用**。シード固定の合成データで全 CLI が動くため、ネットワークがない環境でも仕組みの学習や動作確認ができるが、実在の株価ではない（レポートにはその旨が自動で明記される）。実データの代替として使ってはならない。
- 重要な判断材料として使う・外部に共有する前のレポートは `/review-report` にかける（risk-officer による敵対的レビュー。統計的誤り・ルックアヘッド・投資助言化・合成データ偽装を検出する品質ゲート）。

## このガイドが扱わないこと（再掲）

冒頭に述べた通り、**どの商品・銘柄を買うか、いつ買うかは本ガイドの範囲外**である。本リポジトリの各ツールは
「毎月いくらなら無理がないか」「非課税の価値はいくらか」「保有は目標配分からどれだけずれているか」といった
判断材料を数値と枠組みで提供するが、最終的な投資判断はユーザー自身が行う。

本ガイドおよび生成されるすべてのレポートは投資助言ではなく分析支援である。投資判断は自己責任で行うこと。
制度の数値（NISA の枠・税率等）は記載時点のものであり、変更されうる。最新の制度は公式情報で確認すること。

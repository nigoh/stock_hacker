---
name: market-review
description: 日本株の市況レビューを行うスキル。ユーザーが「今日の市況は？」「マーケットの状況をまとめて」「最近の相場を教えて」「日経平均どうなってる？」のように、個別銘柄ではなく市場全体（指数・為替・金利・セクター動向）の把握やレビューを依頼したときに使う。日経平均・TOPIX（1306.T で代替）・ドル円・米国指数の直近推移を定量取得し、knowledge/macro/ の文書で騰落を文脈化し、可能なら WebSearch で当日材料を補足して、事実と解釈を分離した市況メモを reports/market-<日付>.md に生成する。個別銘柄の深掘り（→ analyze-stock）や条件スクリーニング（→ screen-market）には使わない。
argument-hint: "[期間（省略時 3mo）]"
---

# 市況レビュー

引数（あれば期間指定）: $ARGUMENTS

日本株市場の現況を、指数・為替・米国市場・セクターの4視点から定量的に把握し、マクロ文脈を添えた簡潔な市況メモを作成する。**事実（数値・騰落）と解釈（背景・文脈）を必ず分離して書く**。

## 手順

### 1. 対象期間と日付の確認

- 期間は引数で指定があればそれを使い、無ければ `3mo` を既定とする（直近の推移把握が目的。長期文脈が必要なら `1y` を追加実行してよい）。
- `date +%Y-%m-%d` で本日日付を確認し、レポートファイル名 `reports/market-<日付>.md` に使う。

### 2. 指数・為替・米国市場の定量データ取得

リポジトリルートから compare.py を実行する。ティッカーは4桁コード以外（`^N225`、`USDJPY=X` 等）もそのまま渡せる:

```bash
# 日本市場: 日経平均・TOPIX連動ETF（1306.T をTOPIX代替として使用）
python3 analysis/compare.py ^N225 1306 --period 3mo

# 為替・米国市場: ドル円・S&P500・NASDAQ
python3 analysis/compare.py USDJPY=X ^GSPC ^IXIC --period 3mo

# 日本株と外部要因の連動性（相関行列が目的）
python3 analysis/compare.py ^N225 USDJPY=X ^GSPC --period 3mo
```

- 各実行で相対パフォーマンス（期首=100、1ヶ月/3ヶ月/6ヶ月前比較）、年率ボラティリティ、最大ドローダウン、日次リターン相関行列が `reports/compare-<日付>.md` と stdout に出る。これを市況メモの定量的土台にする。
- より細かい直近の値動き（前日比・週間騰落など）が必要なら、`stocklib` を直接使う短いスクリプトで補足する:

```bash
python3 - <<'EOF'
import sys; sys.path.insert(0, "analysis")
from stocklib.data import fetch_prices
prices = fetch_prices(["^N225", "1306", "USDJPY=X", "^GSPC"], period="1mo")
for t, df in prices.items():
    c = df["Close"]
    print(t, f"直近 {c.iloc[-1]:,.2f} 前日比 {c.iloc[-1]/c.iloc[-2]-1:+.2%} 週間 {c.iloc[-1]/c.iloc[-6]-1:+.2%}" if len(c) > 6 else "データ不足")
EOF
```

- **ネットワークが使えない場合**は各コマンドに `--synthetic` を付けて再実行し、レポート冒頭に「合成データによる手法デモであり、実際の市況ではない」ことを必ず明記する。この場合、手順4のWeb材料収集はスキップする。

### 3. ナレッジベースで騰落を文脈化する

数値を見たら、解釈を書く前に必ず以下を読む:

- `knowledge/00-index.md` — 索引。当日の論点に関連する文書を洗い出す
- `knowledge/macro/boj-and-equities.md` — 日銀の金融政策と株式市場の関係（利上げ・ETF保有の文脈）
- `knowledge/macro/fx-and-japanese-stocks.md` — 円高/円安が輸出株・内需株に与える影響の枠組み
- `knowledge/macro/interest-rates-jgb-and-equities.md` — 金利・JGB利回りと株価（特にバリュエーション・銀行株）の関係
- `knowledge/macro/global-markets-and-japan-linkage.md` — 米国市場・海外要因と日本株の連動メカニズム

さらに論点に応じて追加で読む:

- 経済指標（CPI・GDP・短観等）が材料 → `knowledge/macro/japan-macro-indicators.md`
- 商品市況（原油・資源）が材料 → `knowledge/macro/commodities-and-japan-equities.md`
- 地政学リスク・災害が材料 → `knowledge/macro/disasters-geopolitics-and-japan-equities.md`
- NT倍率・指数の特性に言及する場合 → `knowledge/market-structure/indices-nikkei-topix.md`
- 需給（海外投資家・個人の売買動向）に言及する場合 → `knowledge/market-structure/investor-composition-and-flows.md`

### 4. 当日材料の収集（ネットワーク可の場合のみ）

- WebSearch で「日経平均 本日 材料」「日本株 市況 <日付>」「日銀 金融政策 最新」等を検索し、当日〜直近数日の材料（金融政策イベント、決算集中日、米雇用統計・FOMC、地政学ニュース等）を収集する。
- 収集した事実には必ず出典と日付を付記する。検索で確認できなかったことは書かない。**憶測で材料を創作しない**。
- WebSearch が使えない場合は「材料情報: 取得不可（オフライン環境）」と明示し、定量データとナレッジの枠組みだけでメモを構成する。

### 5. セクター動向の把握

- `analysis/universe/liquid30.csv`（code,name,sector の主要30銘柄）を使い、スクリーナーで横断的な過熱感・トレンドを見る:

```bash
python3 analysis/screen.py --universe analysis/universe/liquid30.csv
```

- 出力（各銘柄のリターン・RSI・SMA位置）を CSV の sector 列で頭の中でグルーピングし、「どのセクターが強い/弱いか」を1〜3行で要約する。個別銘柄の深掘りはしない（必要なら analyze-stock スキルを案内する）。

### 6. 市況メモの作成

`reports/market-<日付>.md` に以下の章立てで書く。簡潔さを最優先し、全体で長くても100行程度に収める:

1. **ヘッドライン** — 3行以内。市場の現況を一言で
2. **事実（数値）** — 指数・為替・米指数の水準と騰落（1週間/1ヶ月/3ヶ月）、ボラティリティ、相関。compare.py の出力から表形式で転記。出所（yfinance / 合成データ）と取得日を明記
3. **事実（材料）** — 手順4で収集したニュース・イベントを箇条書き。各項目に出典・日付。取得不可ならその旨
4. **セクター動向** — 手順5の要約
5. **解釈・文脈** — ここで初めて解釈を書く。knowledge/macro/ の枠組み（金利→バリュエーション、為替→輸出株、米国市場→リスクオン/オフ等）に沿って、観測された騰落の背景仮説を2〜4点。「〜の可能性がある」「〜と整合的」という表現を使い、断定的な将来予測（「上がる」「下がる」）は書かない
6. **注目イベント** — 判明している範囲の今後の日程（日銀会合、FOMC、決算集中日等）。不明なら省略
7. **免責** — 「本メモは情報整理を目的とした分析であり、投資助言ではありません。投資判断は自己責任で行ってください。」の一文を必ず入れる

- 「事実」の章に解釈を混ぜない。「解釈」の章に出典のない新しい事実を書かない。
- 完成したらレポートのパスを提示し、ヘッドライン（章1）を会話でも要約して伝える。

## 注意事項

- TOPIX そのもの（^TPX 等）は yfinance で取得できないことが多いため、TOPIX連動ETF `1306`（1306.T）を代替指標として使い、その旨をレポートに明記する。
- 米国市場と日本市場は営業日・時間帯がずれる。compare.py は共通取引日で揃えるため直近数日の比較には注意し、「米国市場は前営業日終値ベース」等の但し書きを付ける。
- 具体的な数値（政策金利水準など）には「〜年〜月時点」を付記する。

---
id: 2026-07-16-sample-synthetic-golden-cross
date: 2026-07-16
title: 【サンプル】ゴールデンクロス後のモメンタム継続（合成データ）
codes: ["7203"]
direction: up
review_date: 2026-09-14
status: open
outcome: pending
data: synthetic
benchmark: ^N225
benchmark_entry: 4449.7451
entry_prices:
  "7203": 7994.3202
---

> **これは書式を示すためのサンプルエントリ**であり、実際の分析仮説ではない。
> entry_prices は合成データ（--synthetic）によるスナップショットであり実際の株価ではない。
> 実運用のエントリは `python3 analysis/research_journal.py new ...` で作成する（書式は journal/README.md 参照）。

## 仮説

7203 は25日線が75日線を上抜け（ゴールデンクロス）した直後であり、今後60日で
市場平均（^N225）を上回るリターン（超過リターン +2% 以上）を出す——という
「クロス後のモメンタム継続」仮説。検証は verify コマンドの機械判定
（ベンチマーク調整後リターンの符号、±2%未満は mixed）に委ねる。

## 根拠

- `reports/analyze-7203-2026-07-16.md`（分析レポートを引用する。ここはサンプルのためダミーパス）
- `knowledge/technical/indicators-and-ichimoku.md` — 移動平均クロスの教科書的解釈とダマシの注意点
- `knowledge/strategies/market-anomalies-and-seasonality.md` — 日本市場ではモメンタムが弱いことが知られており、この仮説は事前分布としては分が悪い（だからこそ記録して検証する価値がある）

## 反証条件

以下のいずれかが起きたら、検証予定日を待たずにこの仮説は棄却する:

- 終値が25日線を5%以上下回って引ける（クロスのダマシ確定とみなす）
- 決算・業績修正など、テクニカル仮説の前提を無効化するファンダメンタル材料が出る
- 出来高を伴わないままクロスが解消（デッドクロス）する

## 検証結果

（未検証。検証予定日以降に `python3 analysis/research_journal.py verify <このファイル>` を実行すると判定結果が追記される）

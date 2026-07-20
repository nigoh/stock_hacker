# HEALTH — 健全性指標のベースラインと推移

Monitor（`mape/monitor.sh`）が**毎周回1行**を「指標の推移」の表に追記する（docs/mape-k.md）。
「N 周で 未テスト module 5→2」のように、実際に良くなっているかを定量で示すための記録。

stock_hacker は Python の**日本株分析**リポジトリなので、指標は2系統ある:

1. **リポジトリ健全性**: pytest の合否と所要秒、knowledge 索引の整合、テストの無い stocklib モジュール数、
   TODO/FIXME 数、CLI/モジュール/テスト数、最長 SKILL.md 行数。
2. **分析の答え合わせ（track record）**: 夜間フォーキャストの方向的中率・Brier、リサーチジャーナルの
   検証済み/的中/検証期日超過。**分析が当たっていたかを周回ごとに定量記録**し、手法改善の効き目を測る。

## 指標の定義

| 指標 | 意味 | 良い方向 |
|---|---|---|
| gate | `python3 -m pytest analysis/tests` の合否（pass/fail） | pass |
| gate_s | pytest 所要秒（テスト時間相当） | 小 |
| todo | TODO/FIXME コメント数（analysis/scripts/.claude/.github） | 小 |
| index | `check_knowledge_index.py --all` の合否（ok/ng） | ok |
| know_docs | 日本株ナレッジベースの文書数（`knowledge/`、索引を除く） | 参考値 |
| cli | トップレベル分析 CLI 数（`analysis/*.py`） | 参考値 |
| modules | stocklib モジュール数（`analysis/stocklib/*.py`） | 参考値 |
| tests | テストファイル数（`analysis/tests/test_*.py`） | 参考値/大 |
| untested | テストの無い stocklib モジュール数 | 小（0 が理想） |
| max_skill | 最長 SKILL.md の行数（予算 200） | 200 未満 |
| fc_graded | 採点済みの翌営業日予想件数（`forecasts/ledger.csv`, data=real） | 大（標本） |
| fc_hit | 予想の方向的中率 %（採点済みの平均。標本 0 なら na） | 大（>50） |
| fc_brier | 予想の平均 Brier スコア（採点済み。標本 0 なら na） | 小（<0.25） |
| jr_verified | 検証済みリサーチジャーナル仮説数（outcome=hit/miss/mixed, data=real） | 大（標本） |
| jr_hit | 的中（outcome=hit）した仮説数 | 参考値 |
| jr_due | 検証期日超過かつ未検証の仮説数 | 小（0 が理想） |

## 指標の推移

<!-- mape/monitor.sh が下の表に1行/周回で追記する。列順は固定（監視スクリプトが依存）。
     ヘッダ行と区切り行はリネーム・削除しない。 -->

| ts(UTC) | cycle | gate | gate_s | todo | index | know_docs | cli | modules | tests | untested | max_skill | fc_graded | fc_hit | fc_brier | jr_verified | jr_hit | jr_due | note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-07-20T02:50Z | 1 | pass | 61 | 0 | ok | 90 | 15 | 18 | 29 | 1 | 126 | 0 | na | na | 0 | 0 | 0 | monitor |
| 2026-07-20T09:14Z | 2 | pass | 79 | 0 | ok | 90 | 15 | 18 | 29 | 1 | 126 | 0 | na | na | 0 | 0 | 0 | monitor |
| 2026-07-20T09:58Z | 3 | pass | 98 | 0 | ok | 90 | 15 | 18 | 29 | 1 | 126 | 0 | na | na | 0 | 0 | 0 | monitor |
| 2026-07-20T09:59Z | 4 | pass | 90 | 0 | ok | 90 | 15 | 18 | 29 | 1 | 126 | 0 | na | na | 0 | 0 | 0 | monitor |
| 2026-07-20T10:01Z | 5 | pass | 89 | 0 | ok | 90 | 15 | 18 | 29 | 1 | 126 | 0 | na | na | 0 | 0 | 0 | monitor |
| 2026-07-20T10:02Z | 6 | pass | 84 | 0 | ok | 90 | 15 | 18 | 29 | 1 | 126 | 0 | na | na | 0 | 0 | 0 | monitor |
| 2026-07-20T10:04Z | 7 | pass | 87 | 0 | ok | 90 | 15 | 18 | 29 | 1 | 126 | 0 | na | na | 0 | 0 | 0 | monitor |
| 2026-07-20T10:05Z | 8 | pass | 92 | 0 | ok | 90 | 15 | 18 | 29 | 1 | 126 | 0 | na | na | 0 | 0 | 0 | monitor |
| 2026-07-20T10:07Z | 9 | pass | 88 | 0 | ok | 90 | 15 | 18 | 29 | 1 | 126 | 0 | na | na | 0 | 0 | 0 | monitor |
| 2026-07-20T10:08Z | 10 | pass | 91 | 0 | ok | 90 | 15 | 18 | 29 | 1 | 126 | 0 | na | na | 0 | 0 | 0 | monitor |
| 2026-07-20T10:10Z | 11 | pass | 93 | 0 | ok | 90 | 15 | 18 | 29 | 1 | 126 | 0 | na | na | 0 | 0 | 0 | monitor |
| 2026-07-20T10:11Z | 12 | pass | 93 | 0 | ok | 90 | 15 | 18 | 29 | 1 | 126 | 0 | na | na | 0 | 0 | 0 | monitor |

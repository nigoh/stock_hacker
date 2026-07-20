# HEALTH — 健全性指標のベースラインと推移

Monitor（`mape/monitor.sh`）が**毎周回1行**を「指標の推移」の表に追記する（docs/mape-k.md）。
「N 周で 未テスト module 5→2」のように、実際に良くなっているかを定量で示すための記録。

stock_hacker は Python の分析リポジトリなので、土台の実態に即した指標（pytest の合否と所要秒、
knowledge 索引の整合、テストの無い stocklib モジュール数、TODO/FIXME 数、CLI/モジュール/テスト数、
最長 SKILL.md 行数）を用いる。

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

## 指標の推移

<!-- mape/monitor.sh が下の表に1行/周回で追記する。列順は固定（監視スクリプトが依存）。
     ヘッダ行と区切り行はリネーム・削除しない。 -->

| ts(UTC) | cycle | gate | gate_s | todo | index | know_docs | cli | modules | tests | untested | max_skill | note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-07-20T02:50Z | 1 | pass | 61 | 0 | ok | 90 | 15 | 18 | 29 | 1 | 126 | monitor |

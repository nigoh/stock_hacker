# mape/ — MAPE-K 夜間セルフ改善（決定論スクリプト）

「安く読んで考える」M/A/P を決定論の Bash として実装したもの（docs/mape-k.md）。
「壊しうる」Execute は Claude 起動スキル `/mape-execute`、夜間周回は `/mape-night`。
共有ナレッジ（K）は `./knowledge/`（リポジトリルートの日本株ナレッジベース `knowledge/` とは別物）。

## スクリプト

| ファイル | フェーズ | 役割 |
|---|---|---|
| `monitor.sh` | M | シグナル収集 → `state/monitor.env`・`monitor.md`（`--record` で HEALTH.md 追記） |
| `analyze.sh` | A | 症状化＋根拠つき提案（スコア順）→ `state/proposals.tsv`・`analysis.md`（`--update-knowledge` で BACKLOG 追記） |
| `plan.sh` | P | リスク3分類チェックリスト → `state/issue-body.md` |
| `run.sh` | M→A→P | 上記を1周まわす統合ランナー（`--record` で knowledge も更新） |
| `circuit-breaker.sh` | ガードレール | 実行台帳（`state/ledger.jsonl`）と連鎖失敗の停止判定・冪等性クエリ |
| `lib.sh` | 共通 | ルート解決・分類・却下判定・スコア（source 用） |
| `tests/run.sh` | 検証 | 決定論部分の自己テスト（`analysis/tests/test_mape.py` 経由で pytest から実行） |

## 使い方

```bash
bash mape/run.sh            # ドライラン（state/ にだけ出力。knowledge は触らない）
bash mape/run.sh --record   # 本番の夜間周回（HEALTH/BACKLOG/PROGRESS も更新）

bash mape/circuit-breaker.sh status          # 実行可否（tripped なら exit 3）
bash mape/circuit-breaker.sh done "<項目>"   # 実装済み(green)なら exit 0（冪等性）
```

## 設計の約束

- スクリプトは既定で**読み取り専用**（書き込みは `state/` のみ）。`knowledge/` を変えるのはフラグ指定時だけ。
- 品質ゲートは stock_hacker の唯一の自動検証である **pytest**（`python3 -m pytest analysis/tests`）。
- `$MAPE_STATE_DIR` を差し替えれば隔離実行できる（テストは一時ディレクトリを使う）。
- リスク分類・却下ログ・閾値は `knowledge/POLICY.md` と `lib.sh` の環境変数で調整する。
- MAPE-K の K（`mape/knowledge/`）は日本株ナレッジベース（`knowledge/`）とは別管理で、
  索引フック（`check_knowledge_index.py`）の対象外。

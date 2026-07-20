# MAPE-K 夜間セルフ改善ガイド（設計と運用）

stock_hacker 自身のリポジトリ健全性を、夜間に自動で観測・分析・計画し、承認済みの改善だけを安全に
実装していく仕組みのガイド。自律計算の古典アーキテクチャ **MAPE-K**（Monitor / Analyze / Plan / Execute
＋共有 Knowledge）に沿う。deb（開発土台リポジトリ）の MAPE-K を stock_hacker の実態（Python + pytest +
日本株ナレッジベース）に合わせて移植したもの。

大原則は1つ: **「安く読んで考える」M/A/P と「高コストで壊しうる」Execute を分離し、人間の承認ゲートを
GitHub イシューのチェックボックスとして挟む**。読み取りは全自動、破壊的操作はゲート付き自動、マージだけ人間。

## なぜこの設計か（背景と決定）

stock_hacker を使い続けると、小さな改善（テスト追加・整形・索引整合・免責の徹底・軽微なリファクタ）が
溜まる。状態観測と改善案の立案は**読み取り中心で安全・低コスト**であり夜間に自動で回せる。危険なのは
「実際に変更を加える実装」だけ。そこで:

1. **K（共有ナレッジ）** を `mape/knowledge/`（人が読める Markdown）に置く: `BACKLOG.md` / `PROGRESS.md` /
   `POLICY.md` / `HEALTH.md`。全フェーズがここを読み書きし、周回ごとに判断が良くなる「記憶」とする。
   これは日本株ナレッジベース `knowledge/`（`00-index.md` で索引される90文書）とは**別物**。
2. **M / A / P は決定論的な Bash スクリプト**（`mape/monitor.sh` / `analyze.sh` / `plan.sh`、統合 `mape/run.sh`）。
   読み取り専用・低コストで、夜通し何度でも回してよい。出力は `mape/state/` の中間成果物と `mape/knowledge/` の更新。
3. **P は GitHub イシューにリスク3分類のチェックリスト**を掲示する（`mape/plan.sh` が本文を生成し、
   スキルが MCP で投稿/更新）。分類は **自動 / 承認 / 相談**（後述）。
4. **E（実行）は Claude 起動スキル** `/mape-execute`。ポーリング型でイシューを読み、「チェック済み・未着手」の
   項目を **1周1件** だけ安全なループで実装する。pytest 緑→ブランチ + PR、赤→変更破棄して失敗を記録。
5. **ガードレール**: 1周1件・ブランチ + PR・main を直接触らない・PR まで（マージしない）・
   投資助言化/実データ/実発注/秘密/課金に触れない・合成データで実データを偽装しない・
   サーキットブレーカー・冪等性・自動アーカイブ。

決定論部分（M/A/P・サーキットブレーカー）は `mape/tests/run.sh` で機械検証し、`analysis/tests/test_mape.py`
経由で **pytest（＝このリポジトリの品質ゲート）に配線**してある。CI（`.github/workflows/tests.yml`）が
pytest を回すので、MAPE-K の回帰も常時守られる。

### 検討した代替案

- **単一の Claude スキルが M〜E をすべて対話的に実行**: 決定論部分まで LLM に委ねると再現性・機械検証性が
  下がり pytest で守れない。安全フェーズと破壊フェーズの分離が曖昧になり、コスト/リスク上限を置きにくい。
- **GitHub Actions（cron）に個別ロジックを直書き**: 品質ゲートは pytest 単一入口という方針から外れ、実行環境が
  二重化して Claude Code の steering 資産（skills/hooks）を活かせない。

## 構成

```
mape/
├── README.md            # サブシステムの案内
├── lib.sh               # 共通（ルート解決・分類・却下判定・スコア）
├── monitor.sh           # M: シグナル収集 → state/monitor.env・monitor.md（--record で HEALTH 追記）
├── analyze.sh           # A: 症状化＋根拠つき提案（スコア順）→ proposals.tsv（--update-knowledge で BACKLOG 追記）
├── plan.sh              # P: リスク3分類チェックリスト → state/issue-body.md
├── run.sh               # M→A→P 統合ランナー（--record で knowledge も更新）
├── circuit-breaker.sh   # ガードレール: 実行台帳 ledger.jsonl と連鎖失敗の停止判定・冪等性クエリ
├── knowledge/           # 共有ナレッジ K（BACKLOG / PROGRESS / POLICY / HEALTH）
├── state/               # 中間成果物（証跡。真の一時物は .gitignore）
└── tests/run.sh         # 決定論部分の自己テスト（pytest から呼ばれる）
```

スキル/コマンド: `/mape-night`（M→A→P を回してイシュー掲示）・`/mape-execute`（承認済みを1件実装）。

## 監視シグナル（Monitor）

stock_hacker の実態に即した健全性指標を `mape/knowledge/HEALTH.md` の推移表に**毎周回1行**記録する
（列順は固定＝監視スクリプトが依存）。

| 指標 | 意味 | 良い方向 |
|---|---|---|
| gate | `python3 -m pytest analysis/tests` の合否 | pass |
| gate_s | pytest 所要秒 | 小 |
| todo | TODO/FIXME コメント数（analysis/scripts/.claude/.github） | 小 |
| index | `check_knowledge_index.py --all` の合否（未索引/リンク切れ/文書数ずれ） | ok |
| know_docs | 日本株ナレッジベース文書数 | 参考値 |
| cli | トップレベル分析 CLI 数 | 参考値 |
| modules | stocklib モジュール数 | 参考値 |
| tests | テストファイル数 | 大 |
| untested | テストの無い stocklib モジュール数 | 小（0 が理想） |
| max_skill | 最長 SKILL.md 行数（予算 200） | 200 未満 |
| fc_graded / fc_hit / fc_brier | 夜間フォーキャストの採点済み件数・方向的中率%・平均Brier（`forecasts/ledger.csv`, data=real） | hit 大 / Brier 小 |
| jr_verified / jr_hit / jr_due | リサーチジャーナルの検証済み数・的中数・**検証期日超過（未検証）数**（`journal/`, data=real） | due 小（0） |

track record（fc_*, jr_*）は `mape/analysis_signals.py` が `forecasts/ledger.csv` と `journal/` を
**stdlib のみ・ネットワーク不使用**で集計する（価格データ・pandas に依存しない）。合成データ（`data=synthetic`）の
予想・サンプル仮説は track record に数えない（実市況の偽装防止）。

Analyze はこれらを「症状」に変換し、インパクト×労力スコア（`impact * (6 - effort)`、範囲 1–25）の降順で
提案化する。`gate=fail` と `index=ng` は P1（最優先）。`POLICY.md` の却下ログにマッチする類の提案は除外される。

## 分析の答え合わせループ（この移植の主眼）

stock_hacker の MAPE-K は**リポジトリ健全化だけでなく、日本株分析そのものの精度向上**を回す。既存の2つの
track record ストア——**リサーチジャーナル**（`journal/`: 仮説を終値スナップショット付きで記録→期日に
hit/miss/mixed 判定）と**夜間フォーキャスト**（`forecasts/ledger.csv`: 翌営業日予想→翌日採点→的中率/Brier/較正）
——を MAPE-K に接続し、次の閉ループを回す:

```
   ①分析・予想        ②記録               ③答え合わせ            ④測定(Monitor)        ⑤改善(Analyze→Plan→Execute)
  analyze/overnight → journal / ledger → overnight run /      → mape/analysis_       → 弱ければ手法見直しを提案し
  /journal 等         にコミットで蓄積     journal-review で採点   signals.py で集計       Execute が実装（重み・観点＋テスト）
                                                                (的中率/Brier/hit率)     → 次周④で効き目を測る
```

- **③答え合わせの実行**（採点・検証。ネットワーク必須）は `/overnight`・`/journal-review` の担当。MAPE-K は
  **④測定と⑤改善**を担う。Monitor が pending（未採点）や検証期日超過を見つけると、Analyze が
  「答え合わせを回す」運用項目として surface し、Plan の「📊 分析の答え合わせ」ダッシュボードに実績を掲示する。
- **手法が統計的に弱いときだけ改善を提案**する（少数標本での過剰反応を避ける。既定閾値: 予想は採点済み
  20 件以上、ジャーナルは検証済み 5 件以上。`MAPE_FC_MIN_SAMPLE` / `MAPE_JR_MIN_SAMPLE` で調整可）:
  - 方向的中率 < 50%（標本十分）→ `stocklib.forecast` の固定重み合成の見直し＋回帰テスト（approve）。
  - 平均 Brier > 0.25 → `prob_up` の算出・較正の見直し（approve）。
  - ジャーナル hit 率が低い（検証済み標本十分）→ 分析観点・反証条件を `knowledge/strategies/behavioral-finance-japan.md` の枠組みで見直す（approve）。
- 効き目は `HEALTH.md` の推移（例: 方向的中率 48→53%）で周回ごとに定量追跡する。**予想・仮説は将来の断定でも
  売買助言でもない**ため、掲示・提案には必ずその旨と少数標本の不安定さを明記する（既存 CLI と同じ不変条件）。

## リスク3分類（POLICY.md で定義）

`mape/knowledge/POLICY.md` の「リスク分類ルール」のキーワードで提案を分類する（consult > approve > auto の
危険側優先。既定は approve）。

| tier | 意味 | Execute の扱い |
|---|---|---|
| 🟢 auto | 無害・可逆（整形・テスト追加・ドキュメント同期・索引修正・免責追記） | チェック不要で実装（PR まで。マージしない） |
| 🟡 approve | 挙動が変わる（新 CLI・予想/スコアモデル変更・リファクタ・依存更新） | 人がチェックした項目だけ実装 |
| 🔴 consult | 投資助言化・実データ/実発注・APIキー/秘密・課金・デプロイ | チェックされても即実装せず、まず質問 |

stock_hacker 固有の不変条件を分類に織り込んである: **投資助言はしない**・**合成データで実市況/実データを
偽装しない**（POLICY の却下ログでブロック）・**レポートには免責を入れる**（auto に免責追記を含む）。

## ガードレール（Execute の安全境界）

- **1周1件**・**トピックブランチ + PR**・**main を直接触らない**・**PR まで（マージは常に人間）**。
- **サーキットブレーカー**（`mape/circuit-breaker.sh`）: 各試行を `state/ledger.jsonl` に記録し、
  同一項目 red 2回・末尾連続 red・直近窓の失敗多発で `status` が exit 3（tripped）。tripped 中は Execute しない。
- **冪等性**: 台帳に green のある項目は済みと判定し二度実装しない（`circuit-breaker.sh done "<項目>"`）。
- **無人（cron）実行**: consult は実装しない。auto と人がチェックした approve のみ。pytest 緑のときだけドラフト PR。
- **自動アーカイブ**: 未チェックのまま数周過ぎた提案は BACKLOG の「アーカイブ」へ退避（計画を腐らせない）。

## 使い方

```bash
# 手動でドライラン（state/ にだけ出力。knowledge は触らない）
bash mape/run.sh

# 本番の夜間周回（HEALTH/BACKLOG/PROGRESS も更新。pytest ゲートを1回まわす）
bash mape/run.sh --record

# サーキットブレーカー
bash mape/circuit-breaker.sh status          # 実行可否（tripped なら exit 3）
bash mape/circuit-breaker.sh reset           # 台帳を退避して解除
```

対話では `/mape-night`（掲示まで）・`/mape-execute`（承認済みを1件実装）を使う。

## 自動実行（Routine / cron）

`docs/automation.md`・`docs/overnight-forecast.md` と同じ運用思想。

- **M→A→P**: 毎晩1回、cron/Routine で `/mape-night`（または `bash mape/run.sh --record`）を起動。読み取り専用ゆえ安全・安価。
- **Execute**: 毎晩、M→A→P の後に `/mape-execute` を起動。auto と人がチェックした approve のみ・1周1件・PR まで。
- **既知の制約**: headless の cron セッションでは GitHub 等の対話認証系 MCP が使えないことがある。その場合、
  夜間 M/A/P はスクリプト実行と `mape/knowledge/` のコミットまでを行い、イシュー掲示は MCP が使える次の対話へ委ねる。

自律レベルは `mape/knowledge/POLICY.md` の「自律レベル」で管理し、いつでも手動運用へ戻せる。

## 再検討のトリガー

- 計画イシューが1本で収まらなくなったら「1項目=1イシュー」へ移行を検討する。
- サーキットブレーカーが頻発、または無人 Execute の PR 品質が低いなら、自動 Execute を停止し手動へ戻す。
- 即時性が必要になったら `issues.edited` Webhook による即時 Execute を検討する（現状はポーリング型）。

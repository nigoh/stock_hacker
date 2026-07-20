---
name: mape-execute
description: MAPE-K の Execute フェーズ。GitHub 計画イシューを読み、チェック済み・未着手の改善項目を1周1件だけ安全なループで実装し（pytest 緑→PR／赤→破棄）、冪等にチェック＋コメントする。夜間の実装パスや「チェックした項目を進めて」「計画イシューを消化して」と言われたときに使う。個別銘柄の分析や新規レポート作成には使わない。
disable-model-invocation: true
argument-hint: "（引数なし。計画イシューのチェック済み・未着手を1件だけ実装する）"
---

# mape-execute — 承認済み項目の安全な実装（docs/mape-k.md）

「高コストで壊しうる」フェーズ。**1周1件**だけ実装する。ガードレールを厳守すること。

## 不変条件（破ってはいけない）

- **1周1件**。複数を一度に実装しない。
- **トピックブランチ + PR**。`main` を直接触らない。force push しない。
- **投資助言化・実データ/実発注・秘密・課金・デプロイには触れない**（consult 項目は実装前に必ず質問）。
- **合成データ（--synthetic）で実データ・実市況を偽装しない**（CLAUDE.md の絶対原則）。
- **レポートを生成する変更なら免責を必ず入れる**（`stocklib.report.DISCLAIMER`）。
- **pytest が緑のときだけ PR**。赤なら変更を破棄して失敗を記録する。
- **冪等**。対応済み項目（`→ PR #N` コメント or 台帳 green）は二度実装しない。

## 実行モード

- **有人（対話）**: consult 項目は `AskUserQuestion` で確認してよい。通常の手動 `/mape-execute`。
- **無人（自動・cron）**: 毎晩スケジュール起動する自動実行。対話できないため次を厳守する:
  - **consult 項目は実装しない**。スキップし、イシューに「consult は有人実行で判断」と一言残す。
  - 対象は **auto（既定チェック済み）と、人がチェックした approve のみ**。1周1件。
  - **緑のときだけドラフト PR。マージは絶対にしない**（人間がレビューしてマージ）。
  - サーキットブレーカー tripped なら Execute せず、イシュー/通知に理由を残して終了。

## 手順

### 0. サーキットブレーカー確認（最初に必ず）

```bash
bash mape/circuit-breaker.sh status
```

exit 3（tripped）なら **Execute を止めて通知する**。原因（連続失敗/revert 連鎖）を報告し、
ユーザーが `bash mape/circuit-breaker.sh reset` するまで実装しない。

### 1. 計画イシューを読み、対象を1件選ぶ

1. `mcp__github__get_me` → `mcp__github__search_issues` で `label:mape is:open` の計画イシューを取得。
2. `mcp__github__issue_read` で body とコメントを取得し、項目を分類する:
   - **auto**（✅ セクション, 既定 `[x]`）… 承認不要。実装対象になり得る。
   - **approve**（🟡 セクション）… `[x]` が付いた項目だけ対象。
   - **consult**（🔴 セクション）… `[x]` でも**即実装しない**。`AskUserQuestion` で実装可否・範囲を質問し、
     許可が出るまでスキップ。
3. **未着手フィルタ（冪等性）**: 次のいずれかに該当する項目は済みとみなしスキップ:
   - その項目に `→ PR #N` のコメントが付いている
   - `mape/state/ledger.jsonl` に同項目の `green` がある（`bash mape/circuit-breaker.sh done "<項目>"` が exit 0）
4. 残った対象のうち**スコア最上位を1件**選ぶ。無ければ「対象なし」を報告して終了。

### 2. ベースラインの緑を確認

```bash
python3 -m pytest analysis/tests -q
```

- 緑 → 次へ。
- 赤 → **赤を直すことを最優先**にする。選んだ項目は一旦保留し、pytest 赤の修正を今周の1件として扱う
  （それ自体が auto 項目「pytest の赤を直す」に相当する）。

### 3. トピックブランチで実装

```bash
git checkout -b mape/exec-<短いスラッグ>
```

**項目の種類を見分ける（すべて「株の解析の醸成」のための作業）:**

- **運用系（答え合わせ・カバレッジ醸成）** — 「夜間フォーキャストの答え合わせ（grade）…」「検証期日が来た…
  ジャーナル仮説…」「未分析銘柄 N 件をユニバースへ醸成…」。コード変更ではなく CLI 実行で分析資産を育てる項目。
  `python3 analysis/overnight_forecast.py run`（採点＋ユニバース網羅↑）または `/journal-review` を実行し、
  成長した `forecasts/ledger.csv`・`journal/` の差分をコミットする（どちらも git 管理対象）。**実データが
  取れない（ネットワーク不可）ときは合成で偽装せず skip し、その旨をイシューに残す（red は記録しない＝
  実装失敗ではない）。** pytest 緑を確認して PR。
- **手法改善・コード変更系** — 「予想モデルの方向的中率が…重み見直し」「較正不良…」「レンジ的中が低い…」等。
  分析ロジック（`stocklib.forecast` 等）に触れるので **stocklib を再利用**（車輪の再発明をしない）、型ヒント必須。
  **その機能のテストを必ず書く**（`analysis/tests/test_*.py`）。テストが無い変更は PR にしない。予想/スコア
  モデルの重み・較正・レンジ幅を変えるときは、変更前後の的中率・Brier・レンジ的中を `mape/knowledge/PROGRESS.md`
  に残し、次周の monitor が効き目を測れるようにする（＝醸成ループの完結）。
- **ナレッジ更新系** — 「陳腐化ナレッジ N 件を更新…」。`/learn` で「〜年時点」の数値・制度を最新化し、
  `knowledge/00-index.md` の整合まで取る（分析の土台を新鮮に保つ）。

### 4. 全テスト（品質ゲート）を実行して分岐

```bash
python3 -m pytest analysis/tests -q
```

**緑の場合:**
1. コミット（明確なメッセージ）→ `git push -u origin <branch>`（ネットワーク失敗は指数バックオフで最大4回）。
2. `mcp__github__create_pull_request` で**ドラフト PR** を作成（`main` へマージはしない）。
3. `bash mape/circuit-breaker.sh record green "<項目テキスト>" <PR番号> <branch>`
4. `mcp__github__add_issue_comment` で「`→ PR #N で対応`」を投稿し、`mcp__github__issue_write`（update）で
   その項目のチェックボックスを `[x]` にする（**二重実行防止**）。

**赤の場合:**
1. 変更を破棄する: `git reset --hard HEAD && git clean -fd`、ベースブランチへ戻り作業ブランチを削除。
2. `bash mape/circuit-breaker.sh record red "<項目テキスト>"`
3. イシューの当該項目に「失敗：<要因>。再試行は要因解消後」とコメント（チェックは入れない）。

### 5. 知識を更新（ブランチ上で・main は触らない）

- `mape/knowledge/PROGRESS.md` に今周の記録を**末尾追記**（対象/やったこと/結果/考察/次の作業）。
- `mape/knowledge/BACKLOG.md`: 消化した候補を `[x]` にし `→ PR #N` を付す。派生作業は候補に追加。
- `HEALTH.md` は次回 monitor が推移を記録する（ここでは触らなくてよい）。

### 6. 事後のブレーカー再確認

`bash mape/circuit-breaker.sh status` を再実行。tripped になったら通知して止める。

## 停止・エスカレーション

- consult 項目、破壊的変更、投資助言化・実データ/実発注・認証/課金の兆候 → `AskUserQuestion` で必ず確認。
- サーキットブレーカー tripped、または同一項目が繰り返し赤 → 実装を止めてユーザーに報告する。
- 未チェックのまま時間が過ぎた計画項目は、`/mape-night` 側でアーカイブされる（腐らせない）。

---
name: mape-night
description: MAPE-K の夜間周回（Monitor→Analyze→Plan）を1周まわし、リスク3分類のチェックリストを GitHub 計画イシューに掲示/更新する。夜間の自律周回や「改善案を出して」「リポジトリを健全化して」と言われたときに使う。読み取り中心で安全。実装（Execute）は含まない。個別銘柄の分析（→ analyze-stock）や市況レビュー（→ market-review）には使わない。
disable-model-invocation: true
argument-hint: "（引数なし。M→A→P を1周まわして計画イシューを更新する）"
---

# mape-night — 夜間の M→A→P 周回（docs/mape-k.md）

「安く読んで考える」フェーズ。リポジトリ（stock_hacker）を観測して改善案を作り、GitHub イシューに
チェックリストで掲示する。**壊しうる実装はしない**（それは `/mape-execute`）。夜通し何度でも安全に回してよい。

## 位置づけ（何を回すか）

夜間に2系統を観測して改善案を出す:

1. **リポジトリ健全性**: pytest ゲート・knowledge 索引整合・テストの無い stocklib モジュール・TODO・最長 SKILL 行数。
2. **分析の答え合わせ（track record）**: 夜間フォーキャスト（`forecasts/ledger.csv`）の方向的中率・Brier と、
   リサーチジャーナル（`journal/`）の hit 率・検証期日超過を継続測定する。**分析が当たっていたかを測り、
   弱ければ手法改善（予想モデルの重み・仮説の観点）を提案へ回す**——これが「分析→記録→答え合わせ→改善」の閉ループ。

答え合わせの**実行**（採点・検証）自体は `/overnight`（`overnight_forecast.py run`）と `/journal-review` が担う
（ネットワーク必須）。本スキルはその実績を**読み取り専用で測定・掲示し、手法改善を提案**する（銘柄の新規分析はしない）。

## 前提

- GitHub 操作は MCP（`mcp__github__*`）。まず `mcp__github__get_me` で自分と権限を確認する。
- 計画イシューは**1本を使い回す**（毎回新規作成しない）。識別はラベル `mape` ＋タイトル接頭辞 `🌙 MAPE-K`。
- MAPE-K の共有ナレッジ（K）は `mape/knowledge/`。日本株ナレッジベース `knowledge/`（索引フック対象）とは別物。

## 手順

### 1. M→A→P を実行

```bash
bash mape/run.sh --record
```

これで以下が更新される（`mape/state/` に証跡、`mape/knowledge/` に記録）:
- `mape/state/monitor.env` / `monitor.md` … 観測シグナル（pytest ゲート・索引整合・未テストモジュール・**分析の track record**）
- `mape/state/analysis-signals.env` … 分析の答え合わせシグナル（`mape/analysis_signals.py` が生成）
- `mape/state/analysis.md` / `proposals.tsv` … 症状と根拠つき提案（スコア順）
- `mape/state/issue-body.md` … 掲示用チェックリスト（先頭に「📊 分析の答え合わせ」ダッシュボード＋リスク3分類）
- `mape/knowledge/HEALTH.md`（推移1行）/ `BACKLOG.md`（新候補）/ `PROGRESS.md`（monitor サイクル）

> `--record` は監視で pytest（`python3 -m pytest analysis/tests`）を1回まわすため1分ほどかかる。

### 2. 計画イシューを掲示/更新

1. `mcp__github__search_issues` で `repo:<owner>/<repo> is:issue is:open label:mape 🌙 MAPE-K` を検索。
2. 見つかれば `mcp__github__issue_write`（update）でそのイシューの body を `mape/state/issue-body.md` の内容で更新する。
   - **板はスリムに保つ**: 完了項目は `plan.sh` が実行台帳から `<details>「✅ 完了ログ」` に自動で畳む。
     イシューは「今のアクティブな提案＋畳んだ完了ログ」だけを表示する。
   - **人間のチェックは保持する**: 承認セクションで人が付けた `[x]` は、`plan.sh` の再生成で `[ ]` に
     戻さないこと（承認状態はリポジトリ外の人手情報。現 body を見て carry over する）。
   - 未チェックのまま数周過ぎた提案は `mape/knowledge/BACKLOG.md` の「アーカイブ」へ退避する（腐らせない）。
3. 見つからなければ `mcp__github__issue_write`（create）でラベル `mape` を付けて新規作成する
   （ラベルが無ければ作成してよい。タイトル: `🌙 MAPE-K 夜間改善レポート`）。

### 3. 知識の変更をコミット（ブランチ経由・main は直接触らない）

`mape/knowledge/` と `mape/state/` の差分をトピックブランチにコミットして push する
（例: `chore/mape-cycle-<N>`）。**main へは直接コミット・push しない**。
ドラフト PR を開くかはお好みで（知識更新は無害だが、レビュー可能にしておくと良い）。

### 4. 報告

- 何件の提案を出し、どのリスク分類に何件入ったかを1〜2行で要約する。
- `HEALTH.md` の前回→今回の変化（例: 未テストmodule 2→1、gate pass 継続、**方向的中率 48→53%**）があれば添える。
- 分析の track record（採点済み件数・方向的中率・Brier・検証期日超過）と、未採点/期日超過があれば
  「答え合わせを回す」よう一言添える（`/overnight`・`/journal-review`）。少数標本の断定は避ける。

## やらないこと（境界）

- 実装・リファクタ・依存更新などの**変更は一切しない**（Execute の担当）。
- consult 項目でも、ここでは提案を並べるだけ（実装可否の質問は Execute 側で行う）。
- 銘柄・市場の分析はしない（それは analyze-stock / market-review 等）。

## 実行タイミング

- 本スキルは **毎晩1回 cron/Routine で自動起動**する想定（M→A→P は読み取り専用ゆえ安全・安価）。手動起動も可。
- **既知の制約**: headless の cron セッションでは GitHub 等の対話認証系 MCP が使えないことがある。その場合は
  `bash mape/run.sh --record` と knowledge のコミットまでを行い、イシュー掲示は MCP が使える次の対話で補う。

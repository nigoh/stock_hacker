---
name: mape-night
description: MAPE-K の夜間周回（Monitor→Analyze→Plan）を1周まわし、リスク3分類のチェックリストを GitHub 計画イシューに掲示/更新する。夜間の自律周回や「改善案を出して」「リポジトリを健全化して」と言われたときに使う。読み取り中心で安全。実装（Execute）は含まない。個別銘柄の分析（→ analyze-stock）や市況レビュー（→ market-review）には使わない。
disable-model-invocation: true
argument-hint: "（引数なし。M→A→P を1周まわして計画イシューを更新する）"
---

# mape-night — 夜間の M→A→P 周回（docs/mape-k.md）

「安く読んで考える」フェーズ。リポジトリ（stock_hacker）を観測して改善案を作り、GitHub イシューに
チェックリストで掲示する。**壊しうる実装はしない**（それは `/mape-execute`）。夜通し何度でも安全に回してよい。

## 位置づけ（隣接スキルとの違い）

- 対象は**リポジトリ自身の健全性**（テスト・索引整合・コード品質）であって、日本株の分析ではない。
- 銘柄・市場の分析（analyze-stock / market-review / screen-market 等）とは無関係。混同しないこと。

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
- `mape/state/monitor.env` / `monitor.md` … 観測シグナル（pytest ゲート・索引整合・未テストモジュール等）
- `mape/state/analysis.md` / `proposals.tsv` … 症状と根拠つき提案（スコア順）
- `mape/state/issue-body.md` … 掲示用チェックリスト（リスク3分類）
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
- `HEALTH.md` の前回→今回の変化（例: 未テストmodule 2→1、gate pass 継続）があれば添える。

## やらないこと（境界）

- 実装・リファクタ・依存更新などの**変更は一切しない**（Execute の担当）。
- consult 項目でも、ここでは提案を並べるだけ（実装可否の質問は Execute 側で行う）。
- 銘柄・市場の分析はしない（それは analyze-stock / market-review 等）。

## 実行タイミング

- 本スキルは **毎晩1回 cron/Routine で自動起動**する想定（M→A→P は読み取り専用ゆえ安全・安価）。手動起動も可。
- **既知の制約**: headless の cron セッションでは GitHub 等の対話認証系 MCP が使えないことがある。その場合は
  `bash mape/run.sh --record` と knowledge のコミットまでを行い、イシュー掲示は MCP が使える次の対話で補う。

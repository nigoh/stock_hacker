---
description: MAPE-K 夜間周回（Monitor→Analyze→Plan）を1周まわし、リスク3分類の改善チェックリストを GitHub 計画イシューに掲示/更新する（読み取り中心・安全）
argument-hint: "（引数なし）"
---

**mape-night スキル**を必ず起動し（Skill ツールで `mape-night` を呼び出す）、その手順に厳密に従うこと。

要点（スキルの手順が正）:
- `bash mape/run.sh --record` で Monitor→Analyze→Plan を1周まわし、`mape/state/issue-body.md`（掲示用チェックリスト）と `mape/knowledge/`（HEALTH/BACKLOG/PROGRESS）を更新する。
- 対象は**リポジトリ自身の健全性**（pytest ゲート・knowledge 索引整合・テストの無い stocklib モジュール・TODO・最長 SKILL 行数）であり、日本株の分析ではない。
- 計画イシューは**1本を使い回す**（ラベル `mape` ＋タイトル `🌙 MAPE-K`）。完了項目は `<details>` に畳み、板をスリムに保つ。人が付けた承認チェック `[x]` は再生成で消さない。
- **壊しうる実装はしない**（それは `/mape-execute`）。`mape/knowledge/` の更新はトピックブランチ経由でコミットし、**main は直接触らない**。
- headless の cron で GitHub MCP が使えない場合は、スクリプト実行と knowledge コミットまでで止め、掲示は次の対話で補う。

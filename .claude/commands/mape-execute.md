---
description: MAPE-K の Execute。GitHub 計画イシューのチェック済み・未着手を1周1件だけ安全に実装する（pytest 緑→ドラフト PR／赤→破棄・記録）
argument-hint: "（引数なし）"
---

**mape-execute スキル**を必ず起動し（Skill ツールで `mape-execute` を呼び出す）、その手順に厳密に従うこと。

要点（スキルの手順が正）:
- 最初に `bash mape/circuit-breaker.sh status` を確認。tripped（exit 3）なら実装せず理由を報告して止める。
- 計画イシュー（ラベル `mape`）を読み、**auto（既定チェック済み）と人がチェックした approve のみ**を対象に、未着手（`→ PR #N` コメントや台帳 green が無い）の**スコア最上位1件**を選ぶ。**consult は無人では実装しない**（有人で `AskUserQuestion`）。
- ベースラインと実装後の両方で `python3 -m pytest analysis/tests` を実行。**緑のときだけドラフト PR**（`main` へマージはしない）、赤なら変更を破棄して台帳に red を記録。
- **不変条件**: 1周1件・トピックブランチ + PR・main を直接触らない・投資助言化/実データ/実発注/秘密/課金に触れない・合成データで実データを偽装しない・レポート変更なら免責を入れる・冪等（二重実行しない）。
- 実装後は `bash mape/circuit-breaker.sh record green|red ...`、イシューへの `→ PR #N` コメント＋チェック、`mape/knowledge/PROGRESS.md`・`BACKLOG.md` の更新まで行う。

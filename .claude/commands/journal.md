---
description: 分析仮説をリサーチジャーナル（journal/）に記録する（終値スナップショット付き）
argument-hint: "[銘柄コード...] [仮説の内容]（例: 7203 決算後の上方修正期待）"
---

引数（対象銘柄と仮説の内容）: $ARGUMENTS

**research-journal スキル**を必ず起動し（Skill ツールで `research-journal` を呼び出し、引数として「$ARGUMENTS」を渡す）、その手順に厳密に従って仮説を記録すること。

要点（スキルの手順が正）:
- 仮説を検証可能な形（銘柄・direction: up/down/neutral・検証期日）に落とし、`python3 analysis/research_journal.py new` で雛形を生成する（記録時点の終値と ^N225 を自動スナップショット）。
- 生成後、本文の **## 仮説 / ## 根拠 / ## 反証条件** を必ず記入する。特に反証条件（何が起きたら仮説を捨てるか）は空のまま完了にしない。
- journal/ は git 管理対象。個人の売買記録（株数・金額）は書かない。
- 期日が来たら `/journal-review` で検証することを案内する。

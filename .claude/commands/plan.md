---
description: 資産形成プランニング（積立シミュレーション・目標逆算・取り崩し・新NISA活用）を行う
argument-hint: "[毎月5万円を20年 / 目標3000万円を25年 / 3000万円を月12万円で取り崩し]"
---

対象: $ARGUMENTS

**asset-planning スキル**を必ず起動し（Skill ツールで `asset-planning` を呼び出し、引数として「$ARGUMENTS」を渡す）、その手順に厳密に従って資産形成プランニングを行うこと。

要点（スキルの手順が正）:
- 試算の種類（積立予測 / 目標逆算 / 取り崩し）と、金額・期間・想定リターン/ボラ・インフレ率・NISA併記の有無を聞き取る。不明なパラメータは推測せずユーザーに確認する。
- 計算は `python3 analysis/asset_plan.py project|goal|decumulate ...` をリポジトリルートから実行する（ネットワーク不要、率は%表記）。`reports/plan-<サブコマンド>-<日付>.md` にファンチャート付きレポートが生成される。
- 解釈の枠組みとして `knowledge/strategies/long-term-wealth-building.md` と `knowledge/strategies/household-risk-capacity-and-allocation.md` を参照する。
- **想定リターンはユーザー入力の仮定であり将来の保証ではない**。「その想定の根拠は何か」を問い直し、前提を±1〜2ポイント動かした感応度で幅を見せる。
- 特定商品の購入指示は書かない（投資助言ではない）。免責の一文が残っていることを確認し、重要な判断に使う前は `/review-report` を促す。

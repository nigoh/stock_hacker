---
description: 課税口座の含み損益 税価値ビュー（損出し・損益通算の判断材料の機械的整理）を作成する
argument-hint: "[ポートフォリオCSVのパス（省略時: data/portfolio.csv）]"
---

対象: $ARGUMENTS

**tax-view スキル**を必ず起動し（Skill ツールで `tax-view` を呼び出し、引数として「$ARGUMENTS」を渡す）、その手順に厳密に従って含み損益の税価値ビューを作成すること。

要点（スキルの手順が正）:
- 保有 CSV は引数のパス、省略時は `data/portfolio.csv`（portfolio_review.py と同一形式。任意列 `account` が無い・空欄の行はすべて課税口座扱い）。無ければテンプレート `analysis/templates/portfolio-example.csv` を案内する。
- 定量整理は `python3 analysis/tax_report.py [--file <パス>] [--period 1y]` をリポジトリルートから実行する（ネットワーク不可なら `--synthetic` を付け、合成データである旨をレポートに明記）。
- 解釈の枠組みとして `knowledge/regulation-tax/taxation-and-nisa.md` と `knowledge/strategies/behavioral-finance-japan.md` を読み、損出しクロスの同日買戻し注意・繰越控除の確定申告要件・tax tail wagging the dog（税価値のために投資判断を歪めない）の3点を必ず添える。
- 税価値は**実現した場合の条件付き試算**に徹し、**どの銘柄を売るか・保有を続けるかの判断はしない**（投資助言ではない）。免責の一文を必ず含め、重要な判断に使う前は `/review-report` を促す。

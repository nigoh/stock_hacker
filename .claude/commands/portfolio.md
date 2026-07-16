---
description: 保有ポートフォリオの評価・リスクレビュー（損益・セクター配分・β・集中度）を行う
argument-hint: "[ポートフォリオCSVのパス（省略時: data/portfolio.csv）]"
---

対象: $ARGUMENTS

**portfolio-review スキル**を必ず起動し（Skill ツールで `portfolio-review` を呼び出し、引数として「$ARGUMENTS」を渡す）、その手順に厳密に従ってポートフォリオの評価・リスクレビューを行うこと。

要点（スキルの手順が正）:
- 保有 CSV は引数のパス、省略時は `data/portfolio.csv`（列: code,shares,avg_cost,acquired_date,memo,fx_at_cost。memo と fx_at_cost は省略可）。無ければテンプレート `analysis/templates/portfolio-example.csv` を案内する。`data/` は gitignore 対象なので保有情報はコミットされない。
- 定量評価は `python3 analysis/portfolio_review.py [--file <パス>] [--period 1y]` をリポジトリルートから実行する（ネットワーク不可なら `--synthetic` を付け、合成データである旨をレポートに明記）。
- 解釈の枠組みとして `knowledge/math/portfolio-theory.md` と `knowledge/strategies/risk-management-and-position-sizing.md` を読み、集中リスク・セクター偏り・βの意味を文脈化する。
- 改善観点は提示してよいが、特定銘柄の売買指示は書かない（投資助言ではない）。
- 成果物は `reports/portfolio-<日付>.md`。免責の一文を必ず含め、重要な判断に使う前は `/review-report` を促す。

---
description: 保有ポートフォリオの配当インカム・レポート（年間受取配当見込み・YOC・NISA非課税メリット）を作成する
argument-hint: "[ポートフォリオCSVのパス（省略時: data/portfolio.csv）]"
---

対象: $ARGUMENTS

**dividend-income スキル**を必ず起動し（Skill ツールで `dividend-income` を呼び出し、引数として「$ARGUMENTS」を渡す）、その手順に厳密に従って配当インカム・レポートを作成すること。

要点（スキルの手順が正）:
- 保有 CSV は引数のパス、省略時は `data/portfolio.csv`（portfolio_review.py と同一形式。任意列 `account` で NISA/課税口座を区分）。無ければテンプレート `analysis/templates/portfolio-example.csv` を案内する。`data/` は gitignore 対象なので保有情報はコミットされない。
- 定量集計は `python3 analysis/income_report.py [--file <パス>] [--period 1y]` をリポジトリルートから実行する（ネットワーク不可なら `--synthetic` を付け、合成データである旨をレポートに明記）。
- 解釈の枠組みとして `knowledge/strategies/dividend-income-investing.md` と `knowledge/regulation-tax/taxation-and-nisa.md` を読み、TTM実績と会社予想の違い・高利回りの罠・株式数比例配分方式の確認を必ず添える。
- 高配当銘柄の購入推奨・銘柄の入れ替え指示は書かない（投資助言ではない）。免責の一文を必ず含め、重要な判断に使う前は `/review-report` を促す。

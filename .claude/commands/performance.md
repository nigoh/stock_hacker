---
description: 取引履歴からの実運用パフォーマンス・レポート（入出金調整後の実績年率 XIRR・損益内訳・ベンチマーク比較）を作成する
argument-hint: "[取引履歴CSVのパス（省略時: data/transactions.csv）]"
---

対象: $ARGUMENTS

**performance-review スキル**を必ず起動し（Skill ツールで `performance-review` を呼び出し、引数として「$ARGUMENTS」を渡す）、その手順に厳密に従って実運用パフォーマンス・レポートを作成すること。

要点（スキルの手順が正）:
- 取引履歴 CSV は引数のパス、省略時は `data/transactions.csv`（列: `date,code,side,shares,price,fee,account,memo`。side は buy/sell/dividend/deposit/withdraw、配当の源泉徴収税は fee に計上）。無ければテンプレート `analysis/templates/transactions-example.csv` を案内する。`data/` は gitignore 対象なので取引履歴はコミットされない。
- 測定は `python3 analysis/performance_report.py [--file <パス>] [--benchmark ^N225]` をリポジトリルートから実行する（配当込み比較には `--benchmark 1306.T` 等を推奨。ネットワーク不可なら `--synthetic` を付け、合成データである旨をレポートに明記）。
- 解釈の枠組みとして `knowledge/math/performance-measurement-and-attribution.md` を読み、XIRR は金額加重（入金タイミングの影響を含む）で時間加重のベンチマークリターンとの直接比較には限界がある点、短期間の実績では運とスキルを区別できない点を必ず添える。
- 実績 XIRR は asset-planning の想定リターンの現実チェック材料として使えるが、`--return` にそのまま入力しない。
- 「勝っていた/負けていた」の断定・売買指示は書かない（投資助言ではない）。免責の一文を必ず含め、重要な判断に使う前は `/review-report` を促す。

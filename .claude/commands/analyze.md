---
description: 個別銘柄（日本株）の総合分析レポートを作成する
argument-hint: "[銘柄コード（例: 7203）]"
---

対象: $ARGUMENTS

**analyze-stock スキル**を必ず起動し（Skill ツールで `analyze-stock` を呼び出し、引数として「$ARGUMENTS」を渡す）、その手順に厳密に従って個別銘柄の総合分析を行うこと。

要点（スキルの手順が正）:
- 銘柄コードは4桁数字（例: 7203）。社名で指定された場合は `analysis/universe/liquid30.csv` や knowledge/ を参照してコードを特定する。
- 定量分析は `python3 analysis/analyze_stock.py <コード>` をリポジトリルートから実行する（ネットワーク不可なら `--synthetic` を付ける）。
- 分析の枠組みとして `knowledge/00-index.md` から関連文書（テクニカル・ファンダメンタル・リスク指標）を読み、レポートに反映する。
- 成果物は `reports/analyze-<コード>-<日付>.md`。免責の一文を必ず含める。

引数が空の場合は、分析したい銘柄コードをユーザーに確認してから進めること。

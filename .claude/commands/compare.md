---
description: 複数の日本株銘柄の相対パフォーマンス・相関を比較する
argument-hint: "[銘柄コード2つ以上（例: 7203 6758 9984）]"
---

対象: $ARGUMENTS

**compare-stocks スキル**を必ず起動し（Skill ツールで `compare-stocks` を呼び出し、引数として「$ARGUMENTS」を渡す）、その手順に厳密に従って複数銘柄の相対比較を行うこと。

要点（スキルの手順が正）:
- 銘柄コードは4桁数字を2つ以上（例: 7203 6758）。社名で指定された場合は `analysis/universe/liquid30.csv` を参照してコードを特定する。
- 定量比較は `python3 analysis/compare.py <code1> <code2> ... --period 1y` をリポジトリルートから実行する（ネットワーク不可なら `--synthetic` を付ける）。
- 解釈の枠組みとして `knowledge/00-index.md` から関連文書（ポートフォリオ理論・リターン分布・セクター構造）を読み、レポートに反映する。
- 成果物は `reports/compare-<codes>-<日付>.md`。免責の一文を必ず含める。

引数が1銘柄以下の場合は、比較したい銘柄をユーザーに確認してから進めること。

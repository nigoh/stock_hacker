---
description: 条件に合う日本株をスクリーニングして候補リストを作成する
argument-hint: "[条件（例: RSI30以下 200日線より上）]"
---

指定条件: $ARGUMENTS

**screen-market スキル**を必ず起動し（Skill ツールで `screen-market` を呼び出し、引数として「$ARGUMENTS」を渡す）、その手順に厳密に従って銘柄スクリーニングを行うこと。

要点（スキルの手順が正）:
- 自然言語の条件を `python3 analysis/screen.py` のオプション（例: `--rsi-below 30`、`--price-above-sma 200`）に翻訳して実行する。ユニバースは既定で `analysis/universe/liquid30.csv`。ネットワーク不可なら `--synthetic` を付ける。
- 条件の意味づけには `knowledge/technical/`・`knowledge/strategies/` の関連文書を読んで文脈を添える。
- 結果は「結論」ではなく「深掘りすべき仮説の候補リスト」として解釈し、`reports/screen-<日付>.md` に免責付きでまとめる。

条件が曖昧・空の場合は、どの指標・閾値でスクリーニングしたいかをユーザーに確認してから進めること。

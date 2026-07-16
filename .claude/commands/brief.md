---
description: ウォッチリスト銘柄のデイリーブリーフ（市況＋シグナル検出）を作成する
argument-hint: "[ウォッチリストCSVパス（省略時 data/watchlist.csv）]"
---

引数（あればウォッチリスト CSV のパス）: $ARGUMENTS

**daily-brief スキル**を必ず起動し（Skill ツールで `daily-brief` を呼び出し、引数として「$ARGUMENTS」を渡す）、その手順に厳密に従ってデイリーブリーフを作成すること。

要点（スキルの手順が正）:
- `python3 analysis/daily_brief.py` で市況（^N225・1306.T・USDJPY=X・^GSPC）とウォッチリスト銘柄のシグナルを取得する（ネットワーク不可なら `--synthetic` を付け、合成データである旨を明記）。
- ウォッチリストが無ければ `analysis/templates/watchlist-example.csv` を `data/watchlist.csv` にコピーする方法を案内し、市況のみで続行する。
- シグナルの解釈には `knowledge/technical/` の文書を参照し、**事実と解釈を分離**して `reports/brief-<日付>.md` に免責付きでまとめる。
- 注目銘柄があれば /analyze（analyze-stock スキル）での深掘りを提案する。

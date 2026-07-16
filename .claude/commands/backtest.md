---
description: 売買戦略をバックテストし統計的に検証する
argument-hint: "[戦略と銘柄（例: 25日75日ゴールデンクロス 7203）]"
---

対象戦略・銘柄: $ARGUMENTS

**backtest-strategy スキル**を必ず起動し（Skill ツールで `backtest-strategy` を呼び出し、引数として「$ARGUMENTS」を渡す）、その手順に厳密に従ってバックテストと統計検証を行うこと。

要点（スキルの手順が正）:
- 戦略の記述を `python3 analysis/run_backtest.py --strategy <名前> --code <コード>` のパラメータ（例: `--strategy ma_cross --fast 25 --slow 75 --cost-bps 10`）に翻訳して実行する。ネットワーク不可なら `--synthetic` を付ける。
- 数字を出すだけで終わらせない。t統計量・多重検定・ルックアヘッド/生存者バイアス・取引コスト・過剰適合のチェックをスキルの手順どおりに必ず通す。
- 実装前に `knowledge/math/`・`knowledge/strategies/` の必読文書（バックテスト検証・統計的検定関連）を読む。
- 成果物は `reports/` 配下のバックテストレポート。免責の一文を必ず含める。

戦略名や銘柄が特定できない場合は、ルール（指標・パラメータ）と対象銘柄をユーザーに確認してから進めること。

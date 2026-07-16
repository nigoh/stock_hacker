---
description: 検証期日が来たジャーナル仮説を検証し、当たり外れから学ぶ振り返りを行う
argument-hint: "[エントリのパス（省略時は期日到来分すべて）]"
---

引数（あれば特定エントリのパス）: $ARGUMENTS

**journal-review スキル**を必ず起動し（Skill ツールで `journal-review` を呼び出し、引数として「$ARGUMENTS」を渡す）、その手順に厳密に従って検証と振り返りを行うこと。

要点（スキルの手順が正）:
- `python3 analysis/research_journal.py due` で期日到来エントリを確認し、`verify <path>` で hit/miss/mixed を機械判定する（ベンチマーク調整後リターン、±2%未満は mixed）。
- 実データのエントリを `--synthetic` で検証しない（接続失敗時は延期）。
- miss/mixed は `knowledge/strategies/behavioral-finance-japan.md` の認知バイアスの枠組み（確証バイアス・アンカリング・ハーディング・自信過剰）で振り返り、プロセスと結果を分離して次の仮説への修正を1行にまとめる。

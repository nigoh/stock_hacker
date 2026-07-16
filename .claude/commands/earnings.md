---
description: 個別銘柄（日本株）の決算・業績推移の深掘り分析レポートを作成する
argument-hint: "[銘柄コード（例: 7203）]"
---

対象: $ARGUMENTS

**earnings-analysis スキル**を必ず起動し（Skill ツールで `earnings-analysis` を呼び出し、引数として「$ARGUMENTS」を渡す）、その手順に厳密に従って決算・業績の時系列分析を行うこと。

要点（スキルの手順が正）:
- 銘柄コードは4桁数字（例: 7203）。社名で指定された場合は `analysis/universe/liquid30.csv` や WebSearch でコードを特定する。
- 定量分析は `python3 analysis/fundamentals_report.py <コード> --years 5` をリポジトリルートから実行する（ネットワーク不可なら `--synthetic` を付け、合成データである旨をレポートに明記）。
- 解釈の枠組みとして `knowledge/fundamental/reading-japanese-financials.md`・`earnings-guidance-and-consensus.md`・`valuation-metrics.md` を読む。
- 環境変数 `EDINET_API_KEY` があれば有報・半期報告書の原文確認（docID → `stocklib.edinet.fetch_document_csv`）まで行える。
- 株価・テクニカルを含む総合分析は `/analyze`（analyze-stock スキル）の領分。業績の時系列深掘りがこのコマンド。
- 成果物は `reports/fundamentals-<コード>-<日付>.md`。免責の一文を必ず含める。

引数が空の場合は、分析したい銘柄コードをユーザーに確認してから進めること。

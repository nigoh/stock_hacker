# BACKLOG — 作りたいことの候補（優先度順）

MAPE-K の Analyze が追記し、Execute が消化する候補置き場（docs/mape-k.md）。人間も自由に足してよい。
優先度は P1（高）> P2 > P3。`tier` は POLICY.md のリスク分類（auto/approve/consult）。

各項目のフォーマット:
`- [ ] (P?, tier) タイトル — 根拠 / インパクト×労力`

`analyze.sh` は監視シグナルから候補を自動追記し、`plan.sh` はここと分析結果からイシュー本文を生成する。
実装が完了した項目は Execute が `[x]` にして「→ PR #N」を付す。

## 候補

<!-- MAPE-K の主題は「株の解析の醸成」。候補も分析ドメインに置く。多くは analyze.sh が
     監視シグナル（未分析銘柄・陳腐化・的中率）から自動追記する。人間も自由に足してよい。 -->

- [ ] (P3, approve) 予想レンジ（pred_low/high）の ATR 係数を採点実績（in_range）で最適化する — 根拠: レンジ的中率の底上げ / 中×中
- [ ] (P3, approve) forecast の方向スコア合成重み（trend/momentum/meanrev）を採点実績で見直せる形にする — 根拠: 的中率の継続改善の土台 / 中×中

- [ ] (P2, auto) 未分析銘柄 30 件をユニバースへ醸成する（例: 7203,6758,9984,8306,6861）— 根拠: 網羅率 0%（0/30）/ `python3 analysis/overnight_forecast.py run` をユニバース全体で回すと台帳に記録され網羅が上がる

- [ ] (P3, approve) 陳腐化ナレッジ 14 件を更新する（例: fundamental/earnings-quality-and-accounting-fraud.md,fundamental/qualitative-analysis-and-moats.md,history/lost-decades.md）— 根拠: 「〜年時点」の最新が2年以上前 / `/learn` で数値・制度を最新化し分析の土台を新鮮に保つ

## アーカイブ

<!-- 未チェックのまま一定期間過ぎた計画項目や、却下された項目をここへ移す（計画イシューを腐らせない）。 -->

<!-- 2026-07-20 MAPE-K を「株の解析の醸成」へ再設計。以下は旧・システム健全性テーマの候補で、
     主題外につきアーカイブ（analyze の重複排除は再設計で実装済み）。 -->
- [x] (P2, auto) analyze の proposals 重複排除 — 再設計で実装（analyze.sh がスコア降順後に同一テキストを排除）
- [~] (P2, approve) monitor の churn 分析に... / テストの無い module にテスト追加 / README churn 等（システム健全性テーマ。主題外につき保留）

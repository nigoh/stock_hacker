# Analyze レポート — 2026-07-21T07:31Z (cycle 11)

## 症状（分析ドメインの解釈）

### 🎯 予測精度
- 予想: 採点済み 0 件 / 方向的中率 na% / Brier na / レンジ的中 na% / 未採点 0 件
- ジャーナル: 検証済み 0/0 / 的中 0 / 検証期日超過 0 件
### 🗺️ 分析カバレッジ
- ユニバース網羅: 0/30（0%）/ 未分析 30 件
- ナレッジ: 90 文書 / 陳腐化 14 件
### 🛡️ ガードレール（主題外）: pytest=pass

## 改善案（スコア = インパクト×(6-労力)、降順）

| # | tier | prio | impact | effort | score | 内容（根拠つき） |
|---|---|---|---|---|---|---|
| 1 | auto | P2 | 4 | 2 | 16 | 未分析銘柄 30 件をユニバースへ醸成する（例: 7203,6758,9984,8306,6861）— 根拠: 網羅率 0%（0/30）/ `python3 analysis/overnight_forecast.py run` をユニバース全体で回すと台帳に記録され網羅が上がる |
| 2 | approve | P3 | 3 | 3 | 9 | 陳腐化ナレッジ 14 件を更新する（例: fundamental/earnings-quality-and-accounting-fraud.md,fundamental/qualitative-analysis-and-moats.md,history/lost-decades.md）— 根拠: 「〜年時点」の最新が2年以上前 / `/learn` で数値・制度を最新化し分析の土台を新鮮に保つ |
| 3 | approve | P3 | 2 | 3 | 6 | forecast の方向スコア合成重み（trend/momentum/meanrev）を採点実績で見直せる形にする — 根拠: 的中率の継続改善の土台 / 中×中 |
| 4 | approve | P3 | 2 | 3 | 6 | 予想レンジ（pred_low/high）の ATR 係数を採点実績（in_range）で最適化する — 根拠: レンジ的中率の底上げ / 中×中 |

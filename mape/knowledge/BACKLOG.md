# BACKLOG — 作りたいことの候補（優先度順）

MAPE-K の Analyze が追記し、Execute が消化する候補置き場（docs/mape-k.md）。人間も自由に足してよい。
優先度は P1（高）> P2 > P3。`tier` は POLICY.md のリスク分類（auto/approve/consult）。

各項目のフォーマット:
`- [ ] (P?, tier) タイトル — 根拠 / インパクト×労力`

`analyze.sh` は監視シグナルから候補を自動追記し、`plan.sh` はここと分析結果からイシュー本文を生成する。
実装が完了した項目は Execute が `[x]` にして「→ PR #N」を付す。

## 候補

- [ ] (P2, auto) analyze の proposals 重複排除（シグナル由来と BACKLOG 由来の同一テキストを board に二重掲示しない） — 根拠: 計画イシューの重複表示を解消 / 中×小
- [ ] (P2, approve) monitor の churn 分析に stocklib モジュール別のテスト密度指標を追加する — 根拠: カバレッジの穴の精度向上 / 中×中
- [ ] (P3, auto) POLICY.md の却下ログを analyze が学習する精度を上げる — 根拠: 好みへの収束を早める / 中×中

- [ ] (P2, auto) テストの無い stocklib モジュール 1 件にテストを追加する（report）— 根拠: 回帰の穴 / カバレッジ強化

- [ ] (P3, approve) 変更集中箇所 README.md のテスト強化/整理を検討 — 根拠: 直近30コミットの churn 首位 / 回帰リスク

## アーカイブ

<!-- 未チェックのまま一定期間過ぎた計画項目や、却下された項目をここへ移す（計画イシューを腐らせない）。 -->

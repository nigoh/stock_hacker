# Analyze レポート — 2026-07-20T03:21Z (cycle 2)

## 症状（Monitor シグナルの解釈）

- 品質ゲート(pytest): fail（70 s）
- knowledge 索引整合: ok
- 未完了マーカー TODO/FIXME: 0 件
- テストの無い stocklib モジュール: 1 件
- 最長 SKILL.md: 126/200 行
- churn 首位: README.md
- 【分析の答え合わせ】予想: 採点済み 0 件 / 方向的中率 na% / Brier na / 未採点 0 件
- 【分析の答え合わせ】ジャーナル: 検証済み 0/0 / 的中 0 / 検証期日超過 0 件

## 改善案（スコア = インパクト×(6-労力)、降順）

| # | tier | prio | impact | effort | score | 内容（根拠つき） |
|---|---|---|---|---|---|---|
| 1 | auto | P1 | 5 | 2 | 20 | pytest（analysis/tests）の赤を直す — 根拠: gate=fail（最優先。緑化するまで他を止める） |
| 2 | auto | P2 | 4 | 3 | 12 | テストの無い stocklib モジュール 1 件にテストを追加する（report）— 根拠: 回帰の穴 / カバレッジ強化 |
| 3 | approve | P2 | 3 | 3 | 9 | monitor の churn 分析に stocklib モジュール別のテスト密度指標を追加する — 根拠: カバレッジの穴の精度向上 / 中×中 |
| 4 | approve | P3 | 3 | 3 | 9 | 変更集中箇所 README.md のテスト強化/整理を検討 — 根拠: 直近30コミットの churn 首位 / 回帰リスク |
| 5 | auto | P2 | 3 | 3 | 9 | テストの無い stocklib モジュール 1 件にテストを追加する（report）— 根拠: 回帰の穴 / カバレッジ強化 |
| 6 | approve | P3 | 2 | 3 | 6 | 変更集中箇所 README.md のテスト強化/整理を検討 — 根拠: 直近30コミットの churn 首位 / 回帰リスク |
| 7 | auto | P3 | 2 | 3 | 6 | POLICY.md の却下ログを analyze が学習する精度を上げる — 根拠: 好みへの収束を早める / 中×中 |

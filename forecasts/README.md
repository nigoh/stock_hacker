# forecasts/ — 翌営業日フォーキャストの実績台帳

`overnight_forecast.py`（`/overnight`）が生成する「翌営業日の機械予想」と、その
答え合わせ結果を蓄積するディレクトリ。**git 管理対象**（journal/ と同じく、track record を
コミットで醸成していく設計）。

## ledger.csv

1行 = 1銘柄・1営業日ぶんの予想と採点結果。列の定義は
`analysis/stocklib/forecast.py` の `LEDGER_COLUMNS` が正。主な列:

| 列 | 意味 |
|---|---|
| `forecast_id` | `<asof_date>:<code>`（同一営業日・同一銘柄は上書き＝upsert） |
| `asof_date` | 予想の基準にした最終終値の日付 |
| `target_date` | 予想対象の翌営業日（暦日ベースの目安。採点は asof 超の最初の実データ日） |
| `code` / `name` | 銘柄コード・表示名 |
| `data` | `real` / `synthetic`（合成予想は実データの実績で採点しない） |
| `direction` | `up` / `down` / `flat`（機械的方向ラベル） |
| `prob_up` | 上昇確率（0..1、Brier スコア用） |
| `pred_return` | 予想リターン（点推定） |
| `pred_low` / `pred_high` | 予想レンジ（ATR ベース、終値） |
| `s_trend` / `s_momentum` / `s_meanrev` / `score` | 方向スコアの内訳（-1..1） |
| `status` | `pending`（採点前）/ `graded`（採点済み） |
| `actual_return` / `dir_hit` / `in_range` / `brier` / `abs_error` | 採点結果 |

## 運用ループ

```bash
# 夜間（翌営業日へ向けて）: 前回予想の答え合わせ → 新規予想の生成
python3 analysis/overnight_forecast.py run

# 蓄積した台帳から的中率・Brier・較正を集計
python3 analysis/overnight_forecast.py calibration
```

- 予想は RSI・移動平均の並び・モメンタムを固定重みで合成した**機械的ベースライン**で、
  過去データにフィットしていない。**将来の騰落の断定でも売買助言でもない**。
- 「どのシグナルが効くか」は台帳を蓄積して `calibration` で継続測定して初めて分かる
  （＝データの醸成）。詳細は `docs/overnight-forecast.md`、評価指標の理論は
  `knowledge/math/forecast-evaluation-and-calibration.md` を参照。
- 個人のウォッチリスト（`data/watchlist.csv`、git 管理外）を対象にすると、その銘柄コードが
  この台帳（git 管理対象）に記録される点に留意。コミットしたくない場合は `--ledger` で
  git 管理外のパスを指定する。

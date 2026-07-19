# 夜間フォーキャストの運用ガイド

翌営業日の機械予想を生成し（`forecast`）、翌日その予想を実績で答え合わせし（`grade`）、
結果を台帳（`forecasts/ledger.csv`）に蓄積して継続測定する（`calibration`）ループを、
Routine / cron で自動運用するためのガイド。`overnight_forecast.py` / `/overnight` が対象。

大原則は daily_brief と同じ: **実データが取れないときに合成データで代替して「予想」や
「答え合わせ」のように見せることは絶対にしない**。取れなければデータ取得不可を明示して静かに終了する。

## 全体像

```
夜間（翌営業日へ向けて）        翌営業日以降
┌─────────────────┐          ┌────────────────────────┐
│ forecast         │          │ grade                   │
│  翌営業日の予想  │  ───▶    │  実績が出た予想を採点   │
│  を台帳に追加    │          │  （方向的中/Brier/レンジ）│
└─────────────────┘          └────────────────────────┘
        ▲                                  │
        └──────── run が両者を1コマンドで実行 ◀────┘
                                           │
                                    calibration
                              （的中率・Brier・較正の集計）
```

`run` は「前回予想の答え合わせ → 翌営業日の新規予想」を1コマンドで行う夜間運用の本体。

## 予想モデル（機械的ベースライン）

方向スコア $s$ を3つの正規化サブスコアの固定重み合成で作る（`stocklib/forecast.py`）:

- トレンド（終値と25日線の上下、25/75日線の並び）
- モメンタム（20営業日リターンをその期間の標準偏差で正規化）
- 平均回帰（RSI(14) の売られすぎ／買われすぎ）

上昇確率 $p=1/(1+e^{-1.5 s})$、方向は $p\ge 0.55$ で up・$p\le 0.45$ で down・他は flat、
予想リターン $\hat r = s\cdot\sigma_d$、予想レンジは $C_t(1+\hat r)\pm \mathrm{ATR}_{14}$。
**重みは過去データにフィットしておらず、予想は将来の断定でも売買助言でもない**。評価指標の
理論は `knowledge/math/forecast-evaluation-and-calibration.md` を参照。

## 機械可読な契約（自動実行の土台）

各サブコマンドは stdout の**最終行**に `RESULT` を出力する:

| サブコマンド | RESULT 行 |
|---|---|
| `forecast` | `RESULT forecasts=<件数> universe=<成功>/<総数> data=<real\|synthetic\|unavailable>` |
| `grade` | `RESULT graded=<採点数> pending=<残保留数> data=<real\|synthetic\|unavailable>` |
| `run` | `RESULT graded=<g> forecasts=<f> universe=<成功>/<総数> data=<...>` |
| `calibration` | `RESULT graded=<採点済み総数> data=real` |

exit code:

| exit code | 意味 |
|---|---|
| 0 | 正常終了（`data=real` または `data=synthetic`） |
| 2 | 実データ全滅（`data=unavailable`）。予想・採点・レポートは生成されない。stderr に対処を出力 |
| 1 | その他のエラー（ユニバース CSV の不正など。RESULT 行なし） |

補足:
- `grade` の `graded=0` は「翌営業日の実データがまだ揃っていない」＝正常（保留のまま）でも起きる。
  `pending` の残数で監視に穴がないかを見る。
- `data=synthetic`（`--synthetic`）は手法デモ。台帳に `data=synthetic` と記録され、実データの
  実績とは採点されない。**自動実行では使わない**。

## 方法1: ローカル cron（推奨）

東証の大引け後（当日終値が確定する 15:30 以降）に `run` を回すのが素直。当日終値を asof に予想を作り、
翌営業日に前日ぶんを採点できる。

```cron
CRON_TZ=Asia/Tokyo
# 平日 16:30 JST に「前回答え合わせ → 翌営業日予想」
30 16 * * 1-5 cd /path/to/stock_hacker && python3 analysis/overnight_forecast.py run >> ~/stock_hacker_forecast.log 2>&1
```

exit code と RESULT 行で通知を振り分けるラッパー例:

```bash
#!/usr/bin/env bash
set -u
cd /path/to/stock_hacker
out=$(python3 analysis/overnight_forecast.py run 2>&1)
rc=$?
result=$(printf '%s\n' "$out" | grep '^RESULT ' | tail -1)
case "$rc" in
  0)
    graded=$(printf '%s\n' "$result" | sed -n 's/.*graded=\([0-9]*\).*/\1/p')
    fc=$(printf '%s\n' "$result" | sed -n 's/.*forecasts=\([0-9]*\).*/\1/p')
    notify "夜間フォーキャスト: 採点 ${graded:-0} 件 / 新規予想 ${fc:-0} 件"  # notify は任意の通知コマンド
    ;;
  2)
    notify "夜間フォーキャスト: データ取得不可（ネットワーク/Yahoo Finance 到達性を確認）"
    ;;
  *)
    notify "夜間フォーキャスト: 実行エラー（exit=$rc）"
    ;;
esac
```

較正の確認は週次などで十分:

```cron
CRON_TZ=Asia/Tokyo
0 8 * * 6 cd /path/to/stock_hacker && python3 analysis/overnight_forecast.py calibration >> ~/stock_hacker_forecast.log 2>&1
```

## 方法2: Claude Code リモート環境の Routine

平日夜に新規セッションを起動して `/overnight` を実行する構成にできる。

- スケジュール: 平日夕方〜夜の cron 式（時刻基準が UTC かローカルかは環境設定を確認）。
- 実行内容: 「Routine による自動実行である」ことをプロンプトに含めると、スキルの自動実行の扱い
  （RESULT 行での分岐・簡潔報告）が確実に適用される。
- **注意: リモート環境では Yahoo Finance がプロキシで遮断されている場合がある**。その場合
  `overnight_forecast.py` は exit 2 / `data=unavailable` で終了する（正しい挙動）。実データで
  動かすには環境のネットワークポリシーで Yahoo Finance への到達を許可するか、方法1を使う。
  `data=unavailable` のとき `--synthetic` で予想を作り直して市況風に見せることは禁止。

## 台帳（forecasts/ledger.csv）の扱い

- git 管理対象。予想と採点結果が営業日ごとに積み上がり、コミットで track record を醸成する。
- 個人のウォッチリスト（`data/watchlist.csv`、git 管理外）を対象にすると、その銘柄コードが
  台帳（git 管理対象）に記録される。コミットしたくない場合は `--ledger` に git 管理外のパスを指定する。
- 同一営業日・同一銘柄の予想は upsert（`forecast_id = <asof_date>:<code>`）され重複しない。

## 通知の考え方

- **原則: 採点で外れが偏ったとき・データ取得不可・監視の穴（pending が減らない）を通知する**。
  「毎日予想を出した」こと自体は台帳とレポートに残るので、無条件通知はノイズ。
- 較正の判断は標本が貯まるまで保留する。二桁前半の的中率で「勝てる／勝てない」を結論しない。

## 免責

自動実行で生成される予想・答え合わせも投資助言ではなく分析支援である（レポートには
`stocklib.report.DISCLAIMER` が自動付与される）。予想は機械的な条件合成であり、売買の推奨ではない。

---
name: overnight-forecast
description: 翌営業日の機械予想を生成し、前回予想の答え合わせ（採点）をして実績台帳（forecasts/ledger.csv）に蓄積する夜間フォーキャストのスキル。ユーザーが「翌営業日の予想を出して」「明日の予想と答え合わせをして」「予想の的中率を見て」「較正（キャリブレーション）を確認して」「寝てる間に予想と検証を回したい」のように、短期（翌営業日）の機械予想・その事後採点・蓄積した track record の集計を依頼したときに使う。overnight_forecast.py で予想生成（forecast）・採点（grade）・両者一括（run）・集計（calibration）を実行し、knowledge/math/forecast-evaluation-and-calibration.md の枠組みで的中率・Brier・較正を解釈したレポートを reports/ に生成する。個別銘柄の総合分析（→ analyze-stock）、売買ルールのバックテスト（→ backtest-strategy）、当日のウォッチリスト定点観測（→ daily-brief）、長期の仮説記録（→ research-journal）には使わない。
argument-hint: "[forecast|grade|run|calibration（省略時 run）] [--universe CSV]"
---

# 夜間フォーキャスト（翌営業日予想 → 答え合わせ → 台帳醸成）

引数（サブコマンド等）: $ARGUMENTS

「翌営業日へ向けて機械的な予想を出し、翌日その予想を実績で答え合わせし、結果を台帳に貯めて
継続測定する」ループを回す。**予想は将来の騰落の断定でも売買助言でもない**。事実（予想値・
実績・的中）と解釈を分離して書く。

## このスキルの位置づけ

- **翌営業日（短期）の方向・確率予想とその事後検証**が対象。RSI・移動平均の並び・モメンタムを
  固定重みで合成した機械的ベースラインで、過去にフィットしていない。当たり外れは蓄積して測る。
- 混同しやすい隣接スキルとの違い: 当日の市況＋シグナル定点観測は daily-brief、長期（数十日）の
  定性仮説の記録・検証は research-journal、売買ルールの過去検証は backtest-strategy。

## 手順

### 1. サブコマンドの決定

引数からサブコマンドを読み取る（省略時は `run`）:

| 依頼の意図 | サブコマンド |
|---|---|
| 予想と答え合わせをまとめて回す（既定・夜間運用の本体） | `run` |
| 翌営業日の予想だけ生成する | `forecast` |
| 前回予想の答え合わせだけする | `grade` |
| 蓄積した台帳の的中率・Brier・較正を集計する | `calibration` |

### 2. CLI の実行

リポジトリルートから実行する:

```bash
python3 analysis/overnight_forecast.py run                       # grade → forecast
python3 analysis/overnight_forecast.py forecast                  # 予想のみ
python3 analysis/overnight_forecast.py grade                     # 採点のみ
python3 analysis/overnight_forecast.py calibration               # 集計のみ
python3 analysis/overnight_forecast.py run --universe <CSVパス>  # 対象ユニバース指定
```

- 対象ユニバースの既定は `data/watchlist.csv`（無ければ `analysis/universe/liquid30.csv`）。
  `--universe` で任意 CSV（`code` 列必須、`name`/`note` 列は任意）を指定できる。
- 予想・採点結果は `forecasts/ledger.csv`（git 管理対象）に蓄積される。レポートは
  `reports/forecast-<日付>.md` / `forecast-grade-<日付>.md` / `forecast-run-<日付>.md` /
  `forecast-calibration-<日付>.md` に保存され、パスが stdout に出る。
- **yfinance への接続に失敗した場合**は `--synthetic` を付けて再実行し、レポートと会話の両方で
  「合成データによる手法デモであり実データではない」ことを必ず明記する。合成予想は台帳に
  `data=synthetic` と記録され、実データの実績とは採点されない。

### 3. 機械可読な契約（自動実行時）

stdout 最終行の `RESULT` を読む（`docs/overnight-forecast.md` に詳細）:

- `forecast`: `RESULT forecasts=<件数> universe=<成功>/<総数> data=<real|synthetic|unavailable>`
- `grade`: `RESULT graded=<採点数> pending=<残保留数> data=<...>`
- `run`: `RESULT graded=<g> forecasts=<f> universe=<成功>/<総数> data=<...>`
- 実データ全滅は exit code 2 / `data=unavailable`（予想・レポート非生成）。CSV 不正等は exit 1。

**自動実行（Routine / cron）で実データが取れないときに `--synthetic` で代替して市況を偽装する
ことは禁止**。データ取得不可を明示して静かに終了する。

### 4. 結果の解釈

評価指標を解釈する前に、必ず読む:

- `knowledge/math/forecast-evaluation-and-calibration.md` — 方向的中率・Brier スコア
  （無情報基準 0.25）・較正曲線・予想リターン MAE の意味と、少数標本での統計的不安定さ。

解釈の書き方:

- 予想（forecast）: 上位の信頼度銘柄の方向・上昇確率・予想レンジを事実として示す。方向・確率は
  「〜と整合的」「〜の可能性」で述べ、「上がる／下がる」と断定しない。
- 答え合わせ（grade）: 今回採点した予想の的中・外れを列挙し、累積成績（方向的中率・Brier）を
  無情報ベースライン（Brier=0.25、方向50%）と対比する。**外れた予想も隠さず記録する**。
- 較正（calibration）: 較正が取れているか（平均予想確率 ≒ 実際の上昇頻度）を見る。ただし
  **サンプルが少ないうちの的中率・較正は偶然の範囲が広い**ことを必ず添える。標本が二桁前半なら
  「まだ統計的に判断できない」と明記する。
- 予想が無情報ベースラインを安定して上回っているかは、台帳が十分貯まるまで結論を出さない。

### 5. 継続運用の提案

- 初回や単発実行のときは、`docs/overnight-forecast.md` を案内し、Routine / cron で夜間に `run` を
  定期実行して台帳を貯める運用を提案する（通知は RESULT 行・exit code で振り分け）。
- 注目すべき予想（高信頼度・レンジ逸脱の実績など）があれば `/analyze` での深掘りを提案してよい。

## 禁止事項

- 予想・的中率を根拠に特定銘柄の売買を推奨しない（分析支援であって投資助言ではない）。
- 予想モデルの重みを、この場の思いつきで過去データにフィットさせない（ルックアヘッド／過剰最適化。
  見直す場合は backtest-strategy / quant-researcher の手続きで IS/OOS 分割を通す）。
- レポートには必ず免責を入れる（`stocklib.report.DISCLAIMER`。`save_report` が自動付与する）。

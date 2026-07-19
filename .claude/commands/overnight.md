---
description: 翌営業日の機械予想を生成し、前回予想の答え合わせをして実績台帳に蓄積する（夜間フォーキャスト）
argument-hint: "[forecast|grade|run|calibration（省略時 run）] [--universe CSV]"
---

引数（サブコマンド等）: $ARGUMENTS

**overnight-forecast スキル**を必ず起動し（Skill ツールで `overnight-forecast` を呼び出し、引数として「$ARGUMENTS」を渡す）、その手順に厳密に従うこと。

要点（スキルの手順が正）:
- `python3 analysis/overnight_forecast.py run` で「前回予想の答え合わせ（grade）→ 翌営業日の予想生成（forecast）」を実行し、実績を `forecasts/ledger.csv` に蓄積する。個別サブコマンド（`forecast` / `grade` / `calibration`）も指定できる。
- 対象ユニバースの既定は `data/watchlist.csv`（無ければ `analysis/universe/liquid30.csv`）。`--universe` で任意 CSV を指定できる。
- 予想は RSI・移動平均の並び・モメンタムを固定重みで合成した**機械的ベースライン**であり、**将来の騰落の断定でも売買助言でもない**。事実（予想値・実績・的中）と解釈を分離して書く。
- 評価指標（方向的中率・Brier・較正）の解釈は `knowledge/math/forecast-evaluation-and-calibration.md` を参照する。
- yfinance に接続できない場合は `--synthetic` を付けて手法デモとして動かし、「合成データであり実際の市況ではない」ことを必ず明記する（**自動実行では合成での市況偽装は禁止**）。

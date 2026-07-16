# デイリーブリーフの自動実行ガイド

デイリーブリーフ（`analysis/daily_brief.py` / `/brief`）を Routine や cron で定期実行するためのセットアップガイド。
自動実行の大原則は1つ: **実データが取れないときに合成データで代替して「今日の市況」のように見せることは絶対にしない**。取れなければ「データ取得不可」を明示して静かに終了する。

## 前提: 実行環境は2種類ある

| 環境 | Yahoo Finance (yfinance) | J-Quants / EDINET | 備考 |
|---|---|---|---|
| (a) ユーザーのローカル | 到達可 | 到達可 | 自動実行の推奨環境 |
| (b) Claude Code リモート環境 | **プロキシで遮断されることがある** | 到達可 | ネットワークポリシーで Yahoo Finance を許可すれば動作。J-Quants Free は12週間遅延（2025年時点）のため朝のシグナル検出の代替にはならない |

## CLI の機械可読な契約（自動実行の土台）

`daily_brief.py` は stdout の**最終行**に次の1行を出力する:

```
RESULT signals=<検出シグナル総数> watch=<取得成功数>/<ウォッチリスト総数> data=<real|synthetic|unavailable>
```

- `signals` — ウォッチリスト銘柄で検出されたシグナルの総数（`--max-alerts` による表示絞り込み前の値）。
- `watch` — ウォッチリスト銘柄の取得成功数/総数。**`signals=0` の意味を判定するためのフィールド**。`signals=0` だけでは「全銘柄を監視して変化なし」「銘柄が取得できず監視に穴が開いた」「watchlist 未設定」を区別できない:
  - `watch=5/5`（全数成功）— 全銘柄を監視した上でシグナルなし。本当の「変化なし」。
  - `watch=2/5`（部分失敗）— 3銘柄の監視に穴。シグナル見逃しがあり得る（失敗銘柄はレポートの「取得失敗」節に列挙）。市況か銘柄のどれかが取れていれば `data=real` のまま継続する点に注意。
  - `watch=0/0` — `data/watchlist.csv` 未設定（銘柄監視は行われていない。市況のみ）。
- `data=real` — 実データで作成。市況・ウォッチリストの一部の取得に失敗しても、取得できた分があれば継続して `real`（失敗分はレポートの「取得失敗」節に列挙。ウォッチリスト側の失敗の有無は `watch` で判定する）。
- `data=synthetic` — `--synthetic` 指定時。手法デモであり実際の市況ではない。**自動実行では使わない**。
- `data=unavailable` — 実データが1件も取得できなかった。レポートは作成されない（このとき `watch=0/<総数>`）。

exit code:

| exit code | 意味 |
|---|---|
| 0 | 正常終了（`data=real` または `data=synthetic`） |
| 2 | 実データ全滅（`data=unavailable`）。stderr に環境別の対処を出力 |
| 1 | その他のエラー（ウォッチリスト CSV の不正など。RESULT 行なし） |

シグナルが多い日は `--max-alerts N` でレポートの詳細表示を種別優先度（急変動 > 出来高急増 > 移動平均クロス > 52週高安 > RSI）の上位 N 件に絞れる。

## 方法1: ローカル cron（推奨）

### 1-a. Claude Code 経由（/brief スキルの解釈つきブリーフ）

crontab 例（平日 8:30 JST。cron はシステムのタイムゾーンで動くため、必要なら `CRON_TZ` を指定する）:

```cron
CRON_TZ=Asia/Tokyo
30 8 * * 1-5 cd /path/to/stock_hacker && claude -p "/brief" >> ~/stock_hacker_brief.log 2>&1
```

- `claude -p`（非対話モード）はスキルの「自動実行モード」（`.claude/skills/daily-brief/SKILL.md`）に従い、RESULT 行を読んで簡潔に報告する。
- ログをローテーションするか、`logger` 等に流すことを推奨。

### 1-b. Claude Code を介さず CLI を直接実行（軽量・確実）

解釈は不要でレポートファイルだけ欲しい場合は、Python を直接叩く方が依存が少なく確実:

```cron
CRON_TZ=Asia/Tokyo
30 8 * * 1-5 cd /path/to/stock_hacker && python3 analysis/daily_brief.py >> ~/stock_hacker_brief.log 2>&1
```

exit code とRESULT 行を使った通知の振り分け例（シグナル検出時とウォッチリストの穴のみ通知）:

```bash
#!/usr/bin/env bash
# scripts 例: 平日朝に cron から呼ぶラッパー
set -u
cd /path/to/stock_hacker
out=$(python3 analysis/daily_brief.py 2>&1)
rc=$?
result=$(printf '%s\n' "$out" | grep '^RESULT ' | tail -1)
case "$rc" in
  0)
    signals=$(printf '%s\n' "$result" | sed -n 's/.*signals=\([0-9]*\).*/\1/p')
    watch_ok=$(printf '%s\n' "$result" | sed -n 's/.*watch=\([0-9]*\)\/[0-9]*.*/\1/p')
    watch_total=$(printf '%s\n' "$result" | sed -n 's/.*watch=[0-9]*\/\([0-9]*\).*/\1/p')
    if [ "${watch_ok:-0}" -lt "${watch_total:-0}" ]; then
      # ウォッチリスト部分失敗: signals=0 でも「変化なし」ではなく監視に穴が開いている
      notify "デイリーブリーフ: ウォッチリスト取得 ${watch_ok}/${watch_total} 銘柄（$((watch_total - watch_ok)) 銘柄失敗、シグナル ${signals:-0} 件）"
    elif [ "${signals:-0}" -gt 0 ]; then
      notify "デイリーブリーフ: シグナル ${signals} 件検出"   # notify は任意の通知コマンドに置換
    fi
    ;;
  2)
    notify "デイリーブリーフ: データ取得不可（ネットワーク/Yahoo Finance 到達性を確認）"
    ;;
  *)
    notify "デイリーブリーフ: 実行エラー（exit=$rc）"
    ;;
esac
```

- 8:30 JST 時点では当日の東証データはまだ無い（前営業日終値ベースのブリーフになる）。大引け後の確定値でよければ 16:00 以降の実行も選択肢。
- 休場日（土日祝・年末年始）は前営業日と同じ内容になるだけで害はないが、祝日はシグナルが「変化なし」になりやすい。

## 方法2: Claude Code リモート環境の Routine

Claude Code の Routine（スケジュールトリガー）で、**平日朝に新規セッションを起動して `/brief` を実行する**構成にできる:

- スケジュール: 平日朝の cron 式（例: JST 8:30 = UTC 23:30 前日なので `30 23 * * 0-4`。Routine の時刻基準が UTC かローカルかは環境の設定を確認する）。
- 実行内容: 新規セッションで `/brief` を実行するプロンプト。セッションは毎回まっさらな状態から始まるため、プロンプトには「Routine による自動実行である」ことを含めると、スキルの自動実行モード（RESULT 行での分岐・簡潔報告）が確実に適用される。
- **注意: リモート環境では Yahoo Finance がプロキシで遮断されている場合がある**。その場合 `daily_brief.py` は exit 2 / `data=unavailable` で終了する（これは正しい挙動）。実データでブリーフを動かすには、環境のネットワークポリシーで Yahoo Finance（`query1.finance.yahoo.com` 等）への到達を許可するか、方法1（ローカル cron）を使う。
- `data=unavailable` のとき、Routine 側のセッションが `--synthetic` で再実行して市況風のレポートを作ることは禁止（冒頭の大原則）。失敗の簡潔な報告のみ行う。

## 通知の考え方

- **原則: シグナル検出時と「監視に穴が開いたとき」のみ通知する**。「変化なし」を毎日無条件に通知するとノイズになり、本当のシグナルが埋もれる。
- 通知すべきイベントは4つだけ:
  1. `signals >= 1`（`data=real`）— 銘柄・シグナル種別の要点とレポートパス。
  2. **ウォッチリスト部分失敗の継続**（`watch` の成功数 < 総数）— `signals=0` でも通知する。市況が取れている限り exit 0 / `data=real` のままなので、これを黙認すると銘柄の定点観測が静かに穴になる（自動実行はユーザー不在が前提のため数日気づけない）。1日だけなら一時障害の可能性もあるが、続くなら銘柄コードの誤り・上場廃止・データソース側の変更を疑う。
  3. `data=unavailable`（exit 2）— データ取得の失敗。数日続くならネットワーク設定を見直すサイン。
  4. exit 1 — 設定ミス（ウォッチリスト CSV の不正等）。修正するまで毎回失敗する。
- `signals=0` **かつ** `watch` が全数成功の日は通知しない（またはログのみ）。定点観測の記録自体は `reports/brief-<日付>.md` に毎回残る。
- `watch=0/0` は watchlist 未設定（銘柄監視なし・市況のみ）。自動実行を組む前に `data/watchlist.csv` を設定しておくこと（毎朝 `watch=0/0` が続くのは「監視しているつもりで何も監視していない」状態）。
- ウォッチリスト銘柄が多くシグナルが日常的に多発する場合は、通知が形骸化する。`--max-alerts` で表示を絞るよりも先に、ウォッチリストの絞り込みを検討する。

## 免責

自動実行で生成されるブリーフも投資助言ではなく分析支援である（レポートには `stocklib.report.DISCLAIMER` が自動付与される）。シグナルは機械的な条件検出であり、売買の推奨ではない。

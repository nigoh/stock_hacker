# journal/ — リサーチジャーナル（分析仮説の記録と事後検証）

分析・仮説を「書きっぱなし」「やりっぱなし」にせず、**後から当たり外れを検証できる形**で
記録するディレクトリ。エントリは git 管理対象としてコミットする。

## 設計思想

- ここに記録するのは**分析仮説**（「7203 は決算後60日で市場平均を上回る」等）であり、
  **個人の売買記録・保有情報ではない**。保有情報・ウォッチリストは gitignore 対象の
  `data/` に置く（`data/portfolio.csv` 等）。この分離により、仮説の検証履歴は
  リポジトリの共有資産として蓄積しつつ、個人データはコミットされない。
- 仮説は**記録時点の終値スナップショット**（`entry_prices` / `benchmark_entry`）とともに
  保存されるため、後知恵での書き換えが効かない検証が可能になる。
- 検証はベンチマーク（既定 ^N225）対比の**超過リターン**で判定する。
  「地合いで全体が上がっただけ」を的中扱いしないため。
- **反証条件を必ず書く**。何が起きたら仮説を捨てるかを事前に決めておくことが、
  確証バイアス・処分効果への最も実効的な対策になる
  （`knowledge/strategies/behavioral-finance-japan.md` 参照）。

## ファイル配置と書式

エントリは `journal/<YYYY>/<YYYY-MM-DD>-<slug>.md`。YAML frontmatter + Markdown 本文。

```markdown
---
id: 2026-07-16-sample-synthetic-golden-cross
date: 2026-07-16
title: 仮説のタイトル
codes: ["7203", "6758"]
direction: up
review_date: 2026-09-14
status: open
outcome: pending
data: synthetic
benchmark: ^N225
benchmark_entry: 4449.7451
entry_prices:
  "7203": 7994.3202
  "6758": 4321.5
---

## 仮説
（何がどうなると考えるか。検証可能な形で書く）

## 根拠
（データ・レポート・ナレッジ文書。reports/ のパスを引用する）

## 反証条件
（何が起きたらこの仮説を捨てるか。必ず書く）

## 検証結果
（verify コマンドが判定テーブルと総合判定を追記する）
```

### frontmatter の各フィールド

| フィールド | 説明 |
|---|---|
| `id` | `<日付>-<slug>`。ファイル名（拡張子を除く）と一致させる |
| `date` | 記録日（`YYYY-MM-DD`） |
| `title` | 仮説のタイトル |
| `codes` | 対象銘柄。4桁コードの文字列リスト（`["7203", "6758"]` 形式） |
| `direction` | 仮説の方向。`up` / `down` / `neutral`。全銘柄共通ならスカラー、銘柄ごとに変える場合はマッピング（下記） |
| `review_date` | 検証予定日（`YYYY-MM-DD`） |
| `status` | `open` / `reviewed` |
| `outcome` | `pending` / `hit` / `miss` / `mixed` |
| `data` | スナップショット価格のデータ出所。`real` / `synthetic`（new コマンドが自動設定） |
| `benchmark` | 判定に使うベンチマーク（既定 `^N225`） |
| `benchmark_entry` | 記録時点のベンチマーク終値（自動スナップショット） |
| `entry_prices` | 記録時点の各銘柄終値のマッピング（自動スナップショット） |
| `verified_date` | verify 実行日（verify が追記する。未検証のエントリには無い） |

銘柄ごとに方向を変える場合、`direction` は1段ネストのマッピングで書く:

```yaml
direction:
  "7203": up
  "6758": down
```

### 手書き編集時の構文上の注意

frontmatter は PyYAML 非依存の自前パーサ（`analysis/stocklib/journal.py` の
`parse_frontmatter`）で読み書きする。対応する構文は次のサブセット**のみ**:

- `key: value` — スカラー（引用符付きは文字列、それ以外は int/float/bool を推定）
- `key: [a, b]` — フロー形式リスト
- `key:` の次行から半角スペース2つでインデントした `subkey: value` 行 — 1段ネストのマッピング
- 行頭 `#` で始まる行 — コメント（行全体のみ）

**インラインコメント（値の後ろの `# ...`）は非対応**。例えば
`date: 2026-07-16  # 記録日` と書くとコメント部分まで値として読まれて
日付として解釈できずエラーになり、`codes: ["7203"]  # 対象銘柄` は
リストの閉じ括弧を誤認して値が壊れる。手書きで編集する場合も上記サブセットの
範囲で書き、注記が必要なら行頭 `#` の独立したコメント行にすること。

## 使い方（CLI: `analysis/research_journal.py`）

```bash
# 1. 仮説を記録（終値を自動スナップショット。生成後に本文3節を必ず記入）
python3 analysis/research_journal.py new --codes 7203 --title "決算後の上方修正期待" \
    --direction up --review-days 60

# 2. 検証期日が来たエントリの確認（status: open かつ review_date <= 今日）
python3 analysis/research_journal.py due

# 3. 検証（## 検証結果 を追記し、status: reviewed / outcome を更新）
python3 analysis/research_journal.py verify journal/2026/2026-07-16-....md

# 全エントリのサマリー
python3 analysis/research_journal.py list
```

全サブコマンドで `--synthetic`（合成データ・ネットワーク不要）が使える。
合成データで作ったエントリにはその旨が自動で明記される。

## 判定ロジック（verify）

銘柄騰落率 − 同期間のベンチマーク騰落率 = 超過リターンとして、

- |超過リターン| < 2% → 有意な動きなし: up/down は **mixed**、neutral は **hit**
- |超過リターン| >= 2% → 符号と direction が一致すれば **hit**、逆なら **miss**
  （neutral は ±2%以上動いた時点で **miss**）
- 複数銘柄の総合判定: 全 hit → hit、全 miss → miss、混在 → mixed

正確な仕様は `analysis/stocklib/journal.py` の `judge_direction` docstring を参照。

## 関連

- スキル: `.claude/skills/research-journal/SKILL.md`（いつ・どう書くか）、
  `.claude/skills/journal-review/SKILL.md`（期日確認と振り返りの型）
- コマンド: `/journal`（記録）、`/journal-review`（検証と振り返り）
- サンプル: `journal/2026/2026-07-16-sample-synthetic-golden-cross.md`（合成データ・書式見本）

本ディレクトリの記録・判定は分析支援であり投資助言ではない。

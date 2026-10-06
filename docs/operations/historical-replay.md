# 過去検証

入力と結果は `research/`、一次資料の原本は `data/market/primary-sources/` に保存する。
原本のファイル名はSHA-256。資料一覧でURL、公表日時の精度、保守的な利用可能日時、
取得日時、ハッシュ、確認範囲を管理する。保存済みとレビュー済みを区別する。
採用済みJ-Quants原本は[市場データ](market-data.md)の世代管理を使う。

最新の結果と不足は[2026年10月5日の検証](../../research/validation-2026-10-05/README.md)にある。
財務の開示日時による入力再構成と次の確認一覧は
[財務入力](../../research/financial-inputs-2026-10-05/README.md)を参照する。
正式採点・MRS・出口検証の実行結果と将来のholdout計画は
[正式検証の記録](../../research/formal-validation-2026-10-05/README.md)にまとめている。
価格・流動性による資金配分診断と、ルール全体の履歴再生を区別する。
診断の好成績や一次資料の保存件数だけではLIVE条件を通過しない。

## ルール全体の入力

正本は [`research/historical-replay-2025-2026/input-manifest.json`](../../research/historical-replay-2025-2026/input-manifest.json) と
[`research/historical_replay.py`](../../src/stock_jp/research/historical_replay.py)。
以下の9役割をハッシュで固定する。

| 入力 | 必要な内容 |
| --- | --- |
| source_register | 公式提供元、利用権、公開・取得時刻、原本 |
| security_master | 当時の全国内銘柄、上場・廃止・変更期間 |
| trading_calendar | 全取引日 |
| daily_prices | 未調整OHLCV、取引停止、売買可能性 |
| corporate_actions | 分割・併合・合併・上場廃止の公式確認 |
| review_events | 月次・四半期・即時の判定時点 |
| market_regime | 当時利用できたMRS入力と計算 |
| benchmark | 同じ期間の公式指数 |
| assessments | 全月末・全母集団のハードゲート、採点、SAM/SOM、出口判定 |

公式J-Quantsは利用可能。非公式株価は採用しない。現在取得した履歴値については、
当時の公表値と訂正履歴の確認が必要。原本や日時が不明な項目は未確認として残す。
判定を捏造して空欄を埋めず、欠損銘柄や不利な結果を除外しない。

```bash
python -m stock_jp research replay --manifest research/historical-replay-2025-2026/input-manifest.json validate
python -m stock_jp research replay --manifest research/historical-replay-2025-2026/input-manifest.json run
```

`validate`が合格した入力だけを`run`へ渡す。年別と連続ポートフォリオの損益、
費用、最大下落率、ベンチマーク、母集団網羅率、未約定・欠損を確認する。
不足時は合格証跡を生成しない。

## 保存済み財務から入力を作る

```bash
python -m stock_jp research financial-inputs \
  --price-register research/validation-2026-10-05/price-input-register.json \
  --from 2024-12-30 --through 2026-08-31 \
  --output research/financial-inputs-2026-10-05
```

原本の採用済み世代を固定し、通信せずに月末の開示cutoff、四半期・TTM、
原本参照と欠損を出力する。更新される観測CSVを入力の正本にしない。
日付別マスターがある評価日は国内普通株全体を使い、価格欠損を除外しない。
入力不備や会計範囲変更を採点0点・正式不合格へ置き換えない。
この準備コマンドの成功は、9役割を検証する`replay validate`の成功とは別である。

## 実売買への扱い

2025年〜2026年8月は回顧的検証。v0.4凍結後の未観測期間を使うholdoutを代替しない。
holdoutは `operations/evidence/v04-holdout-plan.json` に期間・基準・入力源を事前固定し、
利用者が結果と集中損失を確認する。事後的に基準や凍結日を変更しない。
本人のリスク確認と最終LIVE承認は[有効な条件](live-gates.md)に従う。
復旧試験は必須条件に含めない。

PAPER研究の期間・方法は`research holdout freeze`で未観測期間に固定する。
固定後は上書きせず、凍結した実装変更を無効として検出する。
`research holdout observe --output .work/forward-holdout/observation.json`は原本を変更せず、
入力不足を記録する。期間終了後に正式入力の検証と再生を通過するまで、
snapshot件数を正式な月末評価や取引へ加算しない。

forward入力の正本は
[`research/historical-replay/v04-forward/input-manifest.json`](../../research/historical-replay/v04-forward/input-manifest.json)。
`research.forward_collection`は既存計画の期間・ハッシュへ接続し、9役割の入力領域を
準備して、採用済み世代・日付別partition・公表時刻のあるIR原本を記録する。
原本確認がない役割、利用権、全母集団の開示・採点は未承認のまま保存する。
開始前の価格はwarmup資料であり、forwardの価格や月末評価へ数えない。
収集記録は`collection/<取得日>/<SHA-256>.json`へ追加し、過去の記録を上書きしない。

```bash
python -m stock_jp research replay --manifest research/historical-replay/v04-forward/input-manifest.json validate
```

将来のholdout期間・観測回数は[開始条件 v2](start-conditions-v2.md)では開始前ゲートにしない。
このforward研究の不足は研究未完了として保持し、旧研究の未実施・不足を合格へ変更しない。
現在のPIT・データ整合性・必要な一次資料レビュー・リスク確認と本人の最終承認は引き続き必要で、
現行の運用設定はPAPER / HUMAN_ONLYである。契約・権利が確認できない日次指数、
全銘柄の採点、財務・開示による出口を、取得した件数だけで確認済みにしない。

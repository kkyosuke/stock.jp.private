# 過去検証

入力と結果は `research/`、一次資料の原本は `data/market/primary-sources/` に保存する。
原本のファイル名はSHA-256。資料一覧でURL、公表日時の精度、保守的な利用可能日時、
取得日時、ハッシュ、確認範囲を管理する。保存済みとレビュー済みを区別する。
採用済みJ-Quants原本は[市場データ](market-data.md)の世代管理を使う。

最新の結果と不足は[2026年10月5日の検証](../../research/validation-2026-10-05/README.md)にある。
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

## 実売買への扱い

2025年〜2026年8月は回顧的検証。v0.4凍結後の未観測期間を使うholdoutを代替しない。
holdoutは `operations/evidence/v04-holdout-plan.json` に期間・基準・入力源を事前固定し、
利用者が結果と集中損失を確認する。事後的に基準や凍結日を変更しない。
本人のリスク確認と最終LIVE承認は[有効な条件](live-gates.md)に従う。
復旧試験は必須条件に含めない。

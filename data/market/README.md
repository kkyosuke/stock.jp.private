# 市場データ

- `jquants/`: 採用済みJ-Quants世代。`current.json` が採用世代を指す。
- `manual-dates/`: 手動取得を採用した生応答・結果。分析readinessとは区別する。
- `legacy-prices/`: 過去の日足と取得manifest。
- `historical-replay/`: 取得済み履歴のカバレッジ記録。
- `observations/`: 出典付きの企業行動、株数、予想、決算日。

保存・検証・復元の手順は [市場データの保存・取得](../../docs/operations/market-data.md)。
取得途中のデータと復元済み入力はGit対象外の `.work/market-data/` に置く。
これらの市場データはこのprivateリポジトリで管理する。

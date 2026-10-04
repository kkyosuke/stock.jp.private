# 市場データの保存・取得

取得設定は [`config/sources.json`](../../config/sources.json)、経路と保存先は
[`market_data`](../../src/stock_jp/market_data/) が正本。

## 保存

J-Quantsの採用済みデータは `data/market/jquants/` に置く。
`current.json` が世代manifestを指し、manifestが各JSON/CSVのオブジェクトとSHA-256を指す。
保存形式、manifest、価格・時刻・出典の検証はコードが定義する。
保存上限は `market_data/storage.py` が定義する。1ファイル22MiB、store全体2GiB。
履歴補完後の約740MiBに日次追加の余地を確保する。上限超過時は停止し、自動で引き上げない。

```bash
python -m stock_jp data verify
python -m stock_jp data restore
```

復元先は `.work/market-data/`。復元時にハッシュを照合し、対象外のファイルや経路への
書き込みを拒否する。採用済み世代は直接編集しない。

| 場所 | 内容 | Git |
| --- | --- | --- |
| `data/market/jquants/` | 検証して採用した世代・オブジェクト | 追跡 |
| `data/market/manual-dates/` | 手動承認して採用した対象日ごとの生応答・結果 | 追跡 |
| `data/market/legacy-prices/` | 過去の日足・manifest | 追跡 |
| `data/market/observations/` | 出典付きの企業行動・株数・予想・決算日 | 追跡 |
| `.work/market-data/` | 復元済み入力、取得途中のキャッシュ・チェックポイント | 除外 |
| `.work/staging/` | PR用の保存世代、ロック、途中状態 | 除外 |
| `operations/acquisition/` | 取得取引、消費予算、完了記録 | 追跡 |

## 取得

```bash
python -m stock_jp data catalog
python -m stock_jp data daily --help
python -m stock_jp data history --help
python -m stock_jp data collect --help
```

カタログ参照は通信を行わない。取得には設定と `JQUANTS_API_KEY` が必要。
GitHubからの取得は[自動化](automation.md)の承認・予算管理を通す。
毎日21:00 JSTに同じ夜間入口で取得し、採用済み世代と通信前に保存したcheckpointを使う。
日次価格、財務、カレンダーの公開時刻を区別し、対象日と実際の取得時刻をそれぞれ記録する。
ページの再開、予算消費、HTTP失敗の扱いは取得モジュールが管理する。

本人から依頼された固定履歴のローカル補完には次を使う。

```bash
python -m stock_jp data backfill --as-of 2026-10-02
```

対象期間は `operations/acquisition/date-grants.json` の既存280営業日計画に固定する。
日付別master・日足の保存済みpartitionはハッシュを照合して再利用し、TOPIXと
取引カレンダーを取得する。要求は直列・最低1.05秒間隔で送り、失敗も消費に含める。
旧消費を残した共有台帳に通信前に加算し、合計600回・50バッチ・日付別12回を守る。
`history-backfill-<日付>.json` は元の台帳と履歴journalへの対応を記録する。
同じコマンドで再開でき、完了済み履歴では通信しない。認証失敗・不正応答・429では
停止し、データのpack・検証・PR作成は取得後に行う。

取得が完了しても運用準備完了とは限らない。日足の網羅性、鮮度、銘柄数、財務・開示の
根拠を検証してから日次runに使う。必要な入力が不足していれば停止する。

## 採用

```bash
python -m stock_jp data pack --help
python -m stock_jp data stage --help
python -m stock_jp automation publication --help
```

ローカルでpackするだけでは採用しない。保存世代の検証と公開前検証を通し、privateの
データPRで採用する。完了記録と予算台帳を同じ取引として照合する。
一時ファイル、API key、未検証の取得途中データをPRへ含めない。

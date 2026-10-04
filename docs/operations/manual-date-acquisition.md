# 指定日のGitHub Actions取得

`manual-date-acquisition.yml` は所有者の `workflow_dispatch` 専用です。
対象はLight契約の指定日カレンダー、銘柄一覧、日足。元のprovider responseを
`stock.jp/data/date-acquisition/YYYY-MM-DD/<dataset>/001.json` から保存します。
判断・注文・夜間PAPER完了を生成せず、全9データセット取得済みとも扱いません。

## 永続予約と実行

1. 所有者が対象日を入力して起動する。provider呼出しは行わず、予約専用draft PRを作る。
2. CIと予約内容を確認してmainへ採用する。予約は日付、run ID、次のattempt、最大消費枠に固定する。
3. 同じrunの「Re-run all jobs」で予約されたattemptを起動する。
   checkoutは最新main、実行コードは承認baselineから変更されていないことを検証する。
4. 各ページの送信前にPENDING記録を日付の結果branchへ保存・再読込する。
   応答保存後はSHA-256と件数を記録する。完了後、結果専用draft PRを作る。
5. 結果PRをmainへ採用する。同日再実行はmainの全応答hashを検証して0件で終了する。

恒久checkpoint branchは使いません。予約branchは採用後に削除可能です。
未採用の結果branchが削除されても、mainの予約は消えません。別attempt・新runで
同日分の枠を発行し直すことは禁止します。完了結果が残る場合のみ、providerを再送せず
PR作成を再開できます。不明応答・途中失敗・保存失敗は停止し、自動再試行しません。
失敗後の再取得は別途費用照合と明示的なレビューが必要です。

mainのfirst-parent履歴を承認baselineまで検証し、途中での台帳削除・巻戻しも拒否します。
過去の結果と費用を確認できない限り次の日付を開始しません。
実行履歴やartifactの保持期限を費用記録の根拠にはしません。

## 消費記録

`ledger.json` は既存承認manifestの旧12件をそのまま保持します。
採用済み2026-10-02については正確な送信journalがarchive索引にないため、
追加の12件・1batchを保守的な既存費用枠として計上します。12回の送信が確認できた、
という意味ではありません。新scopeはこの初期台帳のhashとmain SHAを固定します。
未採用payloadは取り込みません。

2025-08-07には旧4件があるため、新しい最大予約は8件です。
全ページ・失敗・不明結果をこの枠に含めます。枠は自動返却しません。
実際の観測送信数は結果の `requests`、保守的な課金枠はmainの `allowance` として区別します。
上限は日付12件、全体600件、50batch、280日。最初の3endpointがページ増加で
枠を超える場合は途中停止し、完全な取得結果として公開しません。

## 承認と有効化に必要なもの

実装PRだけでは有効になりません。既存の承認manifest・owner承認・retirement receipt・
設定は変更しません。別レビューの `.github/stock-date-acquisition-approval.json` が必要です。
schemaは `actions_date_authorization.py` が検証します。新scopeには以下を固定します。

- `version: 1`, `writer: date-grant-v1`, private repository、workflowの完全path
- PAPER / HUMAN_ONLY / Light / addonなし、既存のcoverageと取得上限
- `enabled`, `data_publication_authorized`, `reservation_authorized`、有効期限
- 実装と初期台帳を採用した `baseline.main_sha` と `baseline.ledger_sha256`
- `previous_approval_sha256`、既存の `activation_id` と `retirement_sha256`
- `assurance: retired-external-cooperative-github`、承認PR番号

scopeのPRを採用し、所有者がそのPRへ
`APPROVE_STOCK_AUTOMATION sha256=<scopeの全bytesのSHA-256>` を記録します。
既存の `STOCK_ACTIONS_APPROVAL_SHA256` を同じ値へ、`STOCK_ACTIONS_ENABLED` を
`true` に設定する操作は別の有効化段階です。この実装は承認commentや設定を生成しません。
同じpin変数を使うため、新scopeのpinでは旧Actionのmanifest検証が失敗します。
旧checkpointの復元・作成・存在確認は行いません。

secretは既存の `AUTOMATION_PAT` と `JQUANTS_API_KEY` のみ。
workflow権限は `contents: read`、書込みは既存PATの範囲内です。
共通concurrencyは `stock-private-market-writer`、自動schedule・自動mergeは追加しません。
外部APIに実送信したかどうかは実runの証拠で確認します。synthetic成功だけでは完了としません。

## 保存容量

plain object上限を22MiBへ揃えました。base64、改行、JSONメタデータを含めても
既存32MiB HTTP上限内に収めます。750MiBのarchive総量上限は維持します。
新経路のprovider pageは8MiBまで、応答を分割・丸め・間引きしません。

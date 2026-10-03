# GitHub Actions 日次取得の実行経路（未有効化）

対象は private `kkyosuke/stock.jp.private`。PR #51 後の main
`3d4774d4f14ed0c6183b58d2bb2e72771c060c3e` を基点にした PR #52 のコード変更である。
PAPER/HUMAN_ONLY、Light・addonsなし、価格 coverage 98%、active coverage 100%を維持する。
実データ取得、既存拒否データの転送、dispatch、credential設定、権限変更、旧scheduler操作は行わない。

## 実装した経路

`stock.jp/scripts/actions_driver.py` は、信頼済みmainの照合、独立した承認・引継ぎ証拠の確認、
main archiveの復元、create-only checkpoint初期化、日次または履歴runner、候補検証、
データPR、CI待ち、SHA指定通常squash、remote mainの全hash復元を接続する。
取得・公開の中断状態は固定checkpoint refへ保存し、次の明示起動で読み戻す。

両workflowは `workflow_dispatch` のみで、job条件に無条件の `false` を残す。
実行stepには既存secret名 `AUTOMATION_PAT` と `JQUANTS_API_KEY` の参照を宣言したが、
無効jobなので今回注入・実行されない。PR検証workflowには渡さない。
既存guardrailの例外は、この2つの無効workflowとmain/private repo条件に限定した。
secret名の難読化や代替注入はしない。default tokenの権限は Contents read のままである。

有効化前の必要操作は、独立レビュー、公開拒否・未解決campaignの解消、旧writer停止の証跡、
承認manifestの配備とhash固定、既存tokenの有効性・scope確認、別途承認された有効化である。
`false` を外すだけでは起動しない。CLIは `STOCK_ACTIONS_ENABLED=true` と
`STOCK_ACTIONS_APPROVAL_SHA256`、正しいrepo/ref/event/HEADを必須にする。
この文書やテストfixtureは実環境の承認を発行しない。

## 承認・旧scheduler停止の証拠

`actions_authorization.py` はmain上の `.github/stock-automation-approval.json` を読む。
外部でレビューされたSHA256と一致すること、期限、writer、PAPER/HUMAN_ONLY、契約、
固定上限、publication/checkpoint許可、空の拒否・未解決campaign一覧を検証する。
基点からmainまでの変更は承認関連3ファイルとarchiveの追加・current更新だけに制限する。
比較APIの上限に達する場合も停止し、独立した再承認を要する。

`actions_attestation.py` は次の独立した証拠を全て要求する。

1. scopeの `approval_pr` が同じprivate repoのmainへmerge済み。
2. そのPRに所有者 `kkyosuke` の完全一致コメント
   `APPROVE_STOCK_AUTOMATION sha256=<manifestのSHA256>` がある。
3. `.github/stock-automation-handover.json` のhashがscopeの `handover_sha256` と一致。
4. receiptがcontroller=`chatgpt-automations`、指定scheduler ID、enabled=false、
   active_run_ids空、next_writer一致、handover_epoch一致、observed_by=`kkyosuke`を示す。
5. `.github/stock-automation-controller-evidence.json` の実内容を読み、receipt内の
   `source_evidence_sha256` と観測項目が一致。初期化時の観測は15分以内である。

この証拠は、親・所有者が認可済みschedulerの読取りで停止と稼働中run不在を確認した後に作る。
collector自身にはその証拠の生成・scheduler停止・承認コメント投稿機能がない。
初期化後は同じ承認hash/epoch/writerをledgerに固定する。継続的なChatGPT API監視とは主張しない。
承認済みコードのlegacyローカルrunnerも、enabled manifest存在時にはremote hookなしの取得を拒否する。
古いコードを保有する旧環境まで技術的に無効化できるわけではなく、旧writerを再開しない運用が必要である。

## 永続予算、容量、GitHub側の待機

固定refは `heads/automation/daily-checkpoint-v1`。API送信前に予約とsnapshotをappendし、
非force更新と読み戻しが一致してから送信する。各CASのcommit messageには一意なnonceを付ける。
同じ親・bodyを競合送信しても同じcommitにならず、sibling更新の片方だけが採用される。
結果不明の予約はIN_FLIGHTのまま停止し、返金・自動再送・新runへの付替えをしない。

日次は最大12回。履歴は最大280営業日・600回・50batch×12回を維持する。
ページ追加、既知失敗、明示再試行も消費する。429は120秒以上待ち、401/403は停止する。
既存拒否branchや未保存履歴を初期化seedとして取り込まない。

`actions_sharded.py` はcodec出力を最大1 MiBの不変Git blobに分割する。
小さいcheckpoint JSONにはhash/サイズ/chunk参照だけを入れ、同じtreeで全blobを到達可能にする。
既存20 MiB ledger、750 MiB codec store、2 GiB raw/staging、6 GiB空き容量等の上限は変更しない。
毎回raw hashを確認し、変更のないcompressed objectの再圧縮・再アップロードを省略する。
変更のないRUNNING状態の同期もまとめる。未知の書込み失敗は再送せず停止する。

合成280日×4,000銘柄試験では、raw 135,816,459 bytes、圧縮41,268,636 bytes、
snapshot索引64,267 bytesとなり、全ファイルhashが一致して復元できた。
これは現実的な行数の容量試験であり、実providerデータの内容・容量保証ではない。

`actions_rate.py` はprovider予算とは独立に、GitHub全requestを2,400回/時、content writeを360回/時以下とし、
request間1.6秒、write間10.1秒以上を空ける。GraphQL review照会も同じ計数を通す。
429/403の既知rate応答ではRetry-After/resetを尊重する。結果不明のwriteは再試行しない。
予算にはcheckpoint保存の余力を残し、provider予約前・公開intent保存前に余力不足なら停止する。
計数をledgerへ保存し、同じ時間枠での再起動にも継承する。
[GitHub rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)

1回の実行は5時間を目安に新規予約を止め、workflowの6時間上限に余力を残す。
履歴全件が1jobに収まるとは保証しない。`PAUSED_GITHUB_BUDGET` の後は同じcampaignを明示再開する。
自動dispatch連鎖、上限増加、新campaignでの予算リセットはしない。

## 公開と障害復旧

候補は隔離Git領域で作成し、全Git subprocessから環境・global/system config・template・hooksを除く。
候補tree、圧縮generation、差分bytesを既存意味validatorで検証する。
差分は `stock.jp/data/jquants-archive/` のみ。private分析やrawをPRへ渡さない。

公開transactionはBLOBS→TREE→COMMIT→REF→PR→CI→MERGE→RESTOREを永続化する。
各write前にPENDING intent、応答後にACKEDを保存する。CI待ち・公開再開中は新規取得を行わない。

- refのACK喪失は、期待refとSHAを読んで照合する。
- PR作成のACK喪失は、head SHA/tree/generation、両repo、作者、base/ref、open・non-draft状態を照合する。
  閉じた旧PRや同名branchだけでは成功にしない。
- mergeのACK喪失は、PRのmerged/head/merge SHAを読み、main世代と全raw hashを復元確認する。
  mergeを再送しない。
- opaqueなGit object作成の結果不明は自動復旧せずoperator reviewで停止する。
- mainが進んだ場合は自動rebaseせず、別途検証で停止する。

CI証跡はchecks/runs/statuses/reviews/filesの全ページを取得する。
commit statusはcontextごとにcreated_at/idの最新値へ集約し、未知stateは拒否する。
残存するCHANGES_REQUESTEDはreviewDecisionがnullでも停止する。
required reviewや保護条件はサーバーでも維持し、SHA指定の通常squashだけを行う。
[Statuses](https://docs.github.com/en/rest/commits/statuses)、
[Merge API](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)

merge後のmainからgenerationと全raw hashの一致を確認してRESTOREDにする。
同日のRESTOREDは再取得・再PRをしない。休日と翌日の財務補完は既存runnerに従い同じ境界で採用する。
無差分候補はPRを作らず、確認済みmain世代を採用する。

## 完了履歴の継承

承認baselineのarchive自体を取得・復元し、COMPLETE state、marker、全receipt revisionとpage hashを固定する。
後続mainが自己整合した別のCOMPLETEを含んでも、そのhash集合を比較基準に作り直さない。
8sessionの承認基点を4sessionのCOMPLETEへ置換する合成攻撃を拒否する。

基点に履歴がなかった場合、承認campaignを取得・PR採用・main復元した時点で最初の不変履歴hash集合を
checkpointの `adopted_history` に保存する。後続CASで変更・削除を拒否し、次の日次実行もその集合を検証する。
旧履歴markerを削除して日次を通す運用は禁止する。

## 検証と残る外部条件

テストは実runner、codec、実Git object identityを使い、GitHub/providerの全通信を合成transportにする。
日次、複数履歴batch、CI待ち、PR/merge ACK喪失、再起動、休日財務補完、予算pause、容量、
否定review、status集約、Git環境汚染、履歴基点差替え、独立停止証拠欠落を検証する。
実サービス上のtoken有効性やCI起動・merge成功を、このmock結果から保証しない。

既存secret名はユーザー提供画面で確認されたものを使う。値と実scopeは未確認。
以前のmetadata照会は403 `Resource not accessible by integration`で、別経路による回避をしていない。
新規PAT作成や権限変更はこのPRに含めない。候補は所有者 `kkyosuke` の既存AUTOMATION_PATで、
Contents/Pull requests read/write、Actions/Checks/Commit statuses readとMetadata readを必要範囲で確認する。
EDINET_API_KEYとCLOUDFLARE_API_TOKENは使用しない。

将来の日次予定は21:00 JST（12:00 UTC）だがscheduleは未追加。
旧scheduler停止・未解決campaign解消・承認証拠の実配備後、別レビューで無効条件の解除とscheduleを決める。
PR #52のmerge、有効化、実データ取得は別途承認する。

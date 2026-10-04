# GitHub Actions 日次取得の実行経路（未有効化）

対象は private `kkyosuke/stock.jp.private`。PR #51 後の main
`3d4774d4f14ed0c6183b58d2bb2e72771c060c3e` を基点にした PR #52 のコード変更である。
PAPER/HUMAN_ONLY、Light・addonsなし、価格 coverage 98%、active coverage 100%を維持する。
実データ取得、既存拒否データの転送、dispatch、credential設定、権限変更、旧scheduler操作は行わない。

## 日付指定Actionへの更新

現在の作成対象は [日付指定Action](actions-target-date.md) である。
日次workflowの入口は `actions_target_driver.py --target-date` に切り替え、
真正な一度限りの退役証跡と、GitHub上の現在のrun/attempt所有権を分離した。
以下はPR #52時点のlegacy実装の記録であり、そのlive controller probeを
実在する外部APIとして扱わない。旧経路は回帰テスト用に残すが、履歴workflowを含め無効のままである。
実行者の決定・退役確認・新scopeレビュー・有効化は今回のコード変更では実施しない。

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
compare APIは基点のancestor関係だけに使い、差分は両commitの完全なGit treeから列挙する。
300ファイル上限に依存せず、truncated tree、コード変更、既存object削除・置換は拒否する。

`actions_attestation.py` は次の独立した証拠を全て要求する。

1. scopeの `approval_pr` が同じprivate repoのmainへmerge済み。
2. そのPRに所有者 `kkyosuke` の完全一致コメント
   `APPROVE_STOCK_AUTOMATION sha256=<manifestのSHA256>` がある。
3. `.github/stock-automation-handover.json` のhashがscopeの `handover_sha256` と一致。
4. receiptがcontroller=`chatgpt-automations`、指定scheduler ID、enabled=false、
   active_run_ids空、next_writer一致、handover_epoch一致、observed_by=`kkyosuke`を示す。
   old writerのowner・lease IDもscopeと一致し、leaseはreleased・ownerなしである。
5. `.github/stock-automation-controller-evidence.json` の実内容を読み、receipt内の
   `source_evidence_sha256` と観測項目が一致。これは過去の引継ぎを束縛する証拠である。
6. 別途認証されたcontroller/lease読取りの `live_handover_probe` が、同じidentityについて
   現在もscheduler disabled、active runなし、lease released、ownerなしと確認する。
   実観測時刻が60秒以内であることを検証し、観測時刻を現在時刻へ書き換えない。

この証拠は、親・所有者が認可済みschedulerの読取りで停止と稼働中run不在を確認した後に作る。
collector自身にはその証拠の生成・scheduler停止・承認コメント投稿機能がない。
初期化済みでも鮮度を免除しない。取得予約前、provider送信直前、公開write前にも再照合する。
同じ承認hash/epoch/writerをledgerに固定するが、これを現在の旧writer不在の証拠とは扱わない。
**実controller/leaseの認証済み読取りtransportはまだ提供されていない。**
CLIはこのprobeがないため有効化不可である。保存receipt、環境変数のboolean、ローカルguardで代用しない。
テストでは独立した合成controllerを注入するが、これを実環境の接続完了とは主張しない。
承認済みコードのlegacyローカルrunnerも、enabled manifest存在時にはremote hookなしの取得を拒否する。
このguardは古いcheckoutや旧schedulerの実状態を証明しない。古いcheckout、期限切れ観測、
lease復活、異なるowner/lease/epochは独立した検証で拒否する。

## 永続予算、容量、GitHub側の待機

固定refは `heads/automation/daily-checkpoint-v1`。API送信前に予約とsnapshotをappendし、
非force更新と読み戻しが一致してから送信する。各CASのcommit messageには一意なnonceを付ける。
同じ親・bodyを競合送信しても同じcommitにならず、sibling更新の片方だけが採用される。
結果不明の予約はIN_FLIGHTのまま停止し、返金・自動再送・新runへの付替えをしない。

日次・履歴とも、予約CASとhandover照合が終わった実provider送信直前にgateとapproval期限を再確認する。
同日内に失効しても予約はIN_FLIGHTのまま保持し、送信・返金・自動付替えをしない。
日次はJST運用日も再確認し、日跨ぎなら同じく予約を保持して送信しない。
履歴の固定as_ofにはこの日次境界を適用しない。

日次は最大12回。履歴は最大280営業日・600回・50batch×12回を維持する。
ページ追加、既知失敗、明示再試行も消費する。429は120秒以上待ち、401/403は停止する。
既存拒否branchや未保存履歴を初期化seedとして取り込まない。

`actions_sharded.py` はcodec出力を最大1 MiBの不変Git blobに分割する。
小さいcheckpoint JSONにはhash/サイズ/chunk参照だけを入れ、同じtreeで全blobを到達可能にする。
既存20 MiB ledger、750 MiB codec store、2 GiB raw/staging、6 GiB空き容量等の上限は変更しない。
毎回raw hashを確認し、変更のないcompressed objectの再圧縮・再アップロードを省略する。
変更のないRUNNING状態の同期もまとめる。再起動直後もsnapshotに記録された圧縮bytesを
容量使用量に含め、空のlocal cacheを空のremote storeと誤認しない。
未知の書込み失敗は再送せず停止する。

合成280日×4,000銘柄試験では、raw 135,816,459 bytes、圧縮41,268,636 bytes、
snapshot索引64,267 bytesとなり、全ファイルhashが一致して復元できた。
これは現実的な行数の容量試験であり、実providerデータの内容・容量保証ではない。

`actions_rate.py` はprovider予算とは独立に、GitHub全requestを2,400回/時、content writeを360回/時以下とし、
request間1.6秒、write間10.1秒以上を空ける。GraphQL review照会も同じ計数を通す。
429/403の既知rate応答ではRetry-After/resetを尊重する。結果不明のwriteは再試行しない。
予算にはcheckpoint保存の余力を残し、provider予約前・公開intent保存前に余力不足なら停止する。
計数をledgerへ保存し、CI待ち・同日重複での終了と同じ時間枠の再起動にも継承する。
再開では小さいledger metadataを先に読み、保存予算を復元してからmain archiveを取得する。
この先行読取りは取得・公開を認可せず、承認hash・writerの一致と予算型・非負値も検証する。
時間枠の更新がjob期限内なら、既存の永続予約を保持して次の時間枠を待つ。
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

publicationのblob/tree/commit/ref/PR/mergeは、次の順序で実送信を制御する。

1. GitHub headroom待機後に承認・handoverを照合し、PENDING intentをcheckpointへ保存する。
2. transportのpacingとrate-limit待機を終える。mergeではこの位置からhead/base・CI・reviewを再取得する。
3. 証拠取得中または取得後にrate/quota待機が入った場合、取得途中の証拠を捨てて再取得する。
   通常のrequest間隔も再計算し、merge証拠は取得開始から実送信まで60秒以内を必須にする。
4. 最後のpacing後に、GitHub読取りを伴わない独立controller/lease観測を実行する。
   その観測後にmerge証拠の鮮度、gate、approval期限を確認し、後続の待機・checkpoint書込みなしで送信する。
5. 既知のrate拒否後の再試行も2〜4を繰り返す。観測失敗・復活・失効・証拠の変更なら送信を止める。

head/base・CI・reviewの取得には複数のHTTP readを使い、上記60秒の鮮度検査とSHA指定merge、
既存のサーバー側保護を組み合わせる。GitHubの複数readとmergeを原子的に固定するAPIではない。
送信前の拒否でも保存済みPENDINGを自動消去・再送せず、明示的な確認を必要とする。

- refのACK喪失は、期待refとSHAを読んで照合する。
- PR作成のACK喪失は、head SHA/tree/generation、両repo、作者、base/ref、open・non-draft状態を照合する。
  閉じた旧PRや同名branchだけでは成功にしない。
- mergeのACK喪失は、PRのmerged/head/merge SHAを読み、main世代と全raw hashを復元確認する。
  mergeを再送しない。
- opaqueなGit object作成の結果不明は自動復旧せずoperator reviewで停止する。
- mainが進んだ場合は自動rebaseせず、別途検証で停止する。

CI証跡はchecks/runs/statuses/reviews/filesの全ページを取得する。
commit statusはcontextごとにcreated_at/idの最新値へ集約し、未知stateは拒否する。
reviewerごとの最新の実質的なreviewを集約する。同じheadへの後続APPROVEDは、そのreviewerの
CHANGES_REQUESTEDを解消する。COMMENTや別headへの承認は解消とせず、他reviewerの否定を保持する。
未解決のCHANGES_REQUESTEDはreviewDecisionがnullでも停止する。
[Review仕様](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/reviewing-proposed-changes-in-a-pull-request)
required reviewや保護条件はサーバーでも維持し、SHA指定の通常squashだけを行う。
[Statuses](https://docs.github.com/en/rest/commits/statuses)、
[Merge API](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)

merge後のmainからgenerationと全raw hashの一致を確認してRESTOREDにする。
同日のRESTOREDは再取得・再PRをしない。休日と翌日の財務補完は既存runnerに従い同じ境界で採用する。
無差分候補はPRを作らず、確認済みmain世代を採用する。

## 履歴の正規化と不変objectの再利用

正規化のretrieved_atはexport時刻ではなく、参照するmaster/bars入力revisionの取得完了時刻に束縛する。
同じ入力とnormalizerの意味が同じなら、同一processでは入力page・出力hashを再確認して再利用する。
新processでも一度の再計算で同一bytesとなり、新しいmanifest/revisionを増殖させない。
既存の不変ファイルを削除して件数を減らす方式ではない。

Git commit/tree/blobおよびSHA指定contentsだけを、実行中の容量制限付きcacheで再利用する。
branch/ref、PR、reviews、CI、controller/lease観測はcacheしない。
mainの検証を複数段階で行っても同じ不変blobを繰り返しダウンロードしない。
checkpointで既に検証・保存された同一Git blobはpublication treeからそのSHAで参照する。
新規checkpointでも、直前のmain復元で取得しGit blobのSHA・bytesが一致するblobは再アップロードしない。
未取得・cacheから退避されたblobを、hash名だけで存在すると仮定しない。
main採用の検証では最新のmain treeとgenerationを照合し、hashが一致する不変objectから全rawを復元する。

## 完了履歴の継承

承認baselineのarchive自体を取得・復元し、COMPLETE state、marker、全receipt revisionとpage hashを固定する。
後続mainが自己整合した別のCOMPLETEを含んでも、そのhash集合を比較基準に作り直さない。
8sessionの承認基点を4sessionのCOMPLETEへ置換する合成攻撃を拒否する。

基点に履歴がなかった場合、承認campaignを取得・PR採用・main復元した時点で最初の不変履歴hash集合を
checkpointの `adopted_history` に保存する。後続CASで変更・削除を拒否し、次の日次実行もその集合を検証する。
旧履歴markerを削除して日次を通す運用は禁止する。

## 検証と残る外部条件

ローカルのstock.jp全537テスト、root全53テスト、文書lintが合格した。
その後、予算復元順序の追加回帰を含むdriver/rateの15テストも合格した。
テストは実runner、codec、実Git object identityを使い、GitHub/providerの全通信を合成transportにする。
日次、複数履歴batch、CI待ち、PR/merge ACK喪失、再起動、翌日の古い停止証拠拒否、予算pause、容量、
否定review、status集約、Git環境汚染、履歴基点差替え、独立停止証拠欠落を検証する。
実サービス上のtoken有効性やCI起動・merge成功を、このmock結果から保証しない。

280session・47batchを実runnerで作ったmainから、cacheなし・checkpointなしで月曜の日次を開始する
回帰試験は、provider 9回、GitHub REST 2,353回＋GraphQL 2回、pacing込みの模擬経過5,428.9秒でRESTOREDとなった。
正規化manifestは280件、master/barsのraw partition revisionは560件で、batchごとに増殖しない。
API回数と模擬時刻は合成1銘柄fixtureの測定値であり、実通信やCPU処理時間の上限保証ではない。
4,000銘柄の容量試験は上記の独立した試験である。

全期間のremote checkpoint→publication→adoption→fresh dailyを通すopt-in smokeは、
`stock.jp` から `PYTHONPATH=.:tests python tests/actions_fullperiod_smoke.py` で実行できる。
全通信は合成であり、rate待機は注入clockを進める。全raw hashの反復検証を含むため数十分かかる。

全期間smokeは、280session・562 provider requestsを8回の実行で完了し、合成PR/CI/merge後の
全raw hash復元と直後の休日の日次までRESTOREDとなった。履歴採用時のarchive objectは1,685件。
GitHub予算による7回のpause後も同じcampaignを継続し、予算・承認・不変履歴をリセットしていない。

| 経路 | provider回数 | REST回数 | GraphQL回数 | pacing込み模擬秒 |
| --- | ---: | ---: | ---: | ---: |
| 全280日履歴（8実行の合計） | 562 | 29,690 | 2 | 129,161.5 |
| 直後の休日の日次 | 1 | 2,048 | 2 | 3,997.6 |
| 別試験のcold開始・月曜日次 | 9 | 2,353 | 2 | 5,428.9 |

履歴の各実行は最大17,344.9秒の模擬経過で、5時間予算内に停止した。
このsmokeのclockはCPU処理・実ネットワーク遅延を数えない。単一jobで全履歴が終わる主張ではない。
全期間smokeは45fdd44の実装で開始し、db01849の「大量読取り前の予算復元」は別の追加回帰とCIで確認した。
smokeのJSONにある旧 `github_calls` はREST呼出数を指す。GraphQLの2回/PRは別に計数するが、
そのpacingは模擬経過に含まれる。出力項目名は `github_rest_calls` に揃えた。
上表は68be4cb以前の測定である。その後の送信境界修正ではmerge証拠をもう一度取得するため、
現在の正常採用はGraphQL 3回/PRとなり、REST数と経過も増える。過去の数値を最新実装の測定値とは扱わない。

既存secret名はユーザー提供画面で確認されたものを使う。値と実scopeは未確認。
以前のmetadata照会は403 `Resource not accessible by integration`で、別経路による回避をしていない。
新規PAT作成や権限変更はこのPRに含めない。候補は所有者 `kkyosuke` の既存AUTOMATION_PATで、
Contents/Pull requests read/write、Actions/Checks/Commit statuses readとMetadata readを必要範囲で確認する。
EDINET_API_KEYとCLOUDFLARE_API_TOKENは使用しない。

将来の日次予定は21:00 JST（12:00 UTC）だがscheduleは未追加。
認証済みlive controller/lease transportの実接続、旧scheduler停止、未解決campaign解消、
承認証拠の実配備後、別レビューで無効条件の解除とscheduleを決める。
PR #52のmerge、有効化、実データ取得は別途承認する。

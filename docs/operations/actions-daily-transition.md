# GitHub Actions 日次取得への移行設計（未有効化）

2026-10-03 時点の code-only draft 用。対象は private `kkyosuke/stock.jp.private`、今回の基準 main は PR #51 マージ後の `3d4774d4f14ed0c6183b58d2bb2e72771c060c3e`。既存の日次運用手順を置き換える有効な運用設定ではない。

## 現時点の成果物と限界

- `.github/workflows/daily-collector-disabled.yml`: schedule なし、手動イベントのみ、job は常に false。secrets 注入・書込み権限・取得処理なし。
- `scripts/actions_daily_protocol.py`: 外部接続なしの永続予約プロトコル模型。直接実行すると `STOP_DISABLED` を出す。
- `tests/test_actions_daily_protocol.py`: 合成メモリストアによる、停止・再実行・予約・復元境界の検証。
- `stock.jp/scripts/actions_daily_remote.py`: Git Data REST adapter と既存日次 runner の同期 hooks。transport は注入式で、HTTP client・credential 探索・CLI は持たない。
- `stock.jp/tests/test_actions_daily_remote.py`: 実 runner/codec とネットワークモックによる再起動・障害・競合試験。
- `.github/workflows/history-once-disabled.yml`: 一回限り履歴取得の無効な枠。履歴 runner hooks は接続済みだが、workflow の統合実行driverは未接続。

この変更だけでは自動取得できない。Git REST adapter の呼出し手順、既存 `stock.jp/scripts/jquants_daily.py` の同期予約 hook、応答ページと pagination checkpoint の codec 保存 hook は実装した。固定 origin の実通信 transport、create-only 初期 checkpoint、PR/証跡/通常 merge/復元の callable 部品を追加した。ただし永続的な公開 intent と ref/PR/merge の読取り復旧部品も追加したが、段階を統括する実行driverは未接続。模型の真偽値を人為的に true にすることで運用条件を満たしたと扱ってはならない。最終実装では各条件を信頼できる実測から生成する。

PAPER/HUMAN_ONLY、Light、価格 coverage 98%、active coverage 100% と既存の除外基準を維持する。発注や基準・権限・セキュリティ設定の変更は含まない。模型の endpoint は calendar/master/daily bars/TOPIX のみ。接続した remote runner は既存の日次 NAMES をそのまま使用する。指数 scope の未確定事項や EDINET 等を勝手に追加しない。

## 成立条件とユーザー側の作業

1. 未解決の履歴取得・公開拒否を解決する。拒否された既存 batch を Actions へ移して再公開することは禁止。新しい checkpoint 保存先も契約データの保存・公開先として別途確認が必要。
2. 旧定期タスクを停止し、動作中 writer がいないことを確認する。永続 writer 識別も更新し、旧環境による再開を拒否する。旧タスクの停止操作は本変更では行っていない。
3. ユーザー提供画面で登録済みと確認された `JQUANTS_API_KEY` と `AUTOMATION_PAT` を使用する構成。新規 secret 作成は要求しない。有効性と実 PAT scope は別途未確認。キー値をログ・成果物へ出さない。EDINET はこの市場データ collector では必須にしない。
4. PR 作成者の既存検証が `kkyosuke` を要求するため、既存 `AUTOMATION_PAT` の所有者・対象 repository・権限を確認する。fine-grained PAT の場合の最小権限案を以下に示す。必要な権限は Contents/Pull requests の read/write、検証用 Actions/Checks/Commit statuses の read と Metadata read。実装時に使用する全 REST endpoint の権限を再照合する。Administration、workflow 書換え、Secrets 読取り権限は collector に与えない。
5. GitHub App は短寿命 token の候補だが、bot 作成者は既存 author 検証に通らない。採用には別途明示的な設計レビューが必要であり、本変更では author 検証を緩和しない。
6. branch protection と必須 review を確認する。人の review が必須なら自動処理はそこで待つ。bypass や admin merge を使わない。実 adapter と統合障害テストを完成後、別途許可された公開・有効化の段階で schedule を追加する。

Secrets のメタデータ確認 `gh secret list --repo kkyosuke/stock.jp.private --json name,updatedAt` は HTTP 403 `Resource not accessible by integration`。API 経由では登録状況を確認できず、別経路で回避していない。その後ユーザー提供画面で上記2名の登録を確認した。値と実scopeは未確認。EDINET_API_KEY と CLOUDFLARE_API_TOKEN も画面には存在するが今回使用しない。

現行仕様では `GITHUB_TOKEN` による PR の opened/synchronize/reopened は承認待ちの workflow run を作るため、無人で CI を進める前提にはできない。App token または PAT を候補とする。push による一般の再帰的 workflow 起動も抑制される。[GitHub の起動仕様](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)

既存 repository secrets を参照し、値の読出し・転記・再保存はしない。コード checkout は credential を残さず、secret を渡す step を信頼済み collector に限定する。PR 検証は credential 不要とし、`pull_request_target` や任意 PR コードを privileged workflow で動かす方式を使わない。[Secrets の取扱い](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)

## 時刻・排他・休日

有効化後の予定は毎日 `0 12 * * *` UTC、21:00 JST。GitHub schedule は既定 branch 上で動き、高負荷時の遅延や取りこぼしがあり、定刻実行を保証しない。取りこぼし時は対象日と保存状態を確認してから明示的に再開する。[schedule 仕様](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

同一 repository の全取得 workflow に同じ concurrency group を使い `cancel-in-progress: false` とする。ただし pending run の順序や保持を日次処理の保証に使わない。旧クラウド定期実行はこの排他の外側なので、旧 writer の停止確認と永続ストア側の排他が必要。[concurrency 仕様](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/control-workflow-concurrency)

JST の実行日を開始時に固定し、同日の完了 run は取得せず終了する。既存 calendar 判定で休場日は価格を捏造せず skip とする。翌営業日の補完・訂正は既存 runner の対象日選択と revision 処理に従い、新しい日次枠の中で扱う。暫定値を確定値に見せない。12 回で不足すれば停止する。前日の未解決 run がある場合、新日付を使って予算をリセットしない。

## 永続予約と途中復旧

通常の既存 runner はローカル journal に予約してから API を呼ぶ。追加した `remote=` hook を使うと、ローカル予約後にリモート CAS と読み戻しが終わるまで API を呼ばない。Actions の一時ディスクだけでは runner 消失時に予約も失われるため、hook 未指定の運用を Actions で有効化してはならない。終了時 artifact upload や cache を予算台帳にしない。

1. 最新 main の archive を復元し、generation と全ファイル hash を照合する。global checkpoint、未採用 PR、未解決 writer も読み戻す。
2. API 呼出しごとに、JST 日付・config/main identity・request 通番・endpoint・query hash を永続予約する。上限は日次 12、ページ・失敗・再試行も消費する。
3. 永続ストアの更新と読み戻しが一致して初めて外部 API を呼ぶ。Git 実装案では期待 revision を唯一の親にした append commit と非 force 更新を使う。競合した sibling commit は fast-forward 不成立で停止し、勝手に rebase/force して API を再送しない。[Git reference 更新 API](https://docs.github.com/en/rest/git/refs#update-a-reference)
4. 応答を既存 codec・secret scan・意味検証に通し、承認済み checkpoint 保存先へ永続化して読み戻す。成功後のみ次の request に進む。エラー本文に含まれ得る秘密情報も保存しない。
5. ACK 消失、通信結果不明、応答保存失敗は消費済み `IN_FLIGHT` のまま停止。返金・自動再送しない。既知 429 は 120 秒以上待ち、再試行も同じ枠を消費する。401/403 は停止。日付越えの未完了 run はレビューし、別枠へ無断移行しない。

checkpoint branch は未採用の作業保存であり main 採用済みを意味しない。remote adapter は global schema、hash、writer identity、日付順序、設定 identity、予算累積を検証しなければならない。Git adapter は固定 ref `heads/automation/daily-checkpoint-v1` のみを更新し、main や既存履歴 branch を入力に取らない。blob hash・tree member・schema・writer・global ledger の削除/予算変更を検証する。snapshot は既存 codec で秘密情報/参照を検証し、20 MiB の JSON blob 上限を超えればアップロード前に停止する。実サービスの障害・容量特性は未検証。全 snapshot を毎回保存する方式は検証優先で、長期運用向け object 分割とコスト測定は残る。

## 採用・CI・復元

検証済み checkpoint から、最新 main を種にした新しい作業領域へ最終 generation を一度 pack する。途中 checkpoint の全世代を publication branch に詰め込み、未参照 object を増やさない。差分は既存 validator が許す `stock.jp/data/jquants-archive/` のみ。既存 daily-publication の意味・秘密情報・所有者検証を必須にする。

データ専用 PR を作成後、全ページの checks・workflow runs・statuses・reviews を取得し、同じ head SHA に必要な CI が成功していることを確認する。模型の `publication_gate` はこのうち補助的な判定だけを表し、既存 validator や全 REST 証跡収集を置き換えない。PR head/base が動けば再検証する。

merge 直前にも private repository、差分、head、base、保護・review を確認し、期待 head SHA を指定した通常の squash merge を行う。競合・CI 失敗・review 待ちは checkpoint を保持して停止する。サーバーの branch protection を変更しない。[merge API](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)

merge 応答だけで成功とせず、remote main の採用 generation を復元して全 hash を確認して初めて完了とする。復元失敗は `MERGED_PENDING_RESTORE` で停止し、新規取得せず復元を修復する。休日に保存差分がない場合は PR を作らず、対象 main と無変更証跡を検証する別遷移が必要。この最終遷移および collection→publication→merge の実接続は未実装。

collector は schedule と信頼済み手動起動だけに限定し、push/PR に反応させない。データ PR の既存 CI は取得を呼ばない。この分離により自身の checkpoint/PR 更新から取得が連鎖することを避ける。

## 未完了の有効化条件

- 契約データ公開の拒否・既存履歴 campaign の未解決状態の解消。
- credential 登録と必要権限の確認、旧タスク停止と writer 引継ぎ。
- 実装済みtransport・初期化・証跡収集・復元部品を、永続的な公開intentと再開制御へ接続する統合driver。
- 実データを使わない隔離環境で、runner 強制終了、競合 writer、ACK 消失、PR 更新、CI 待ち、merge 後復元失敗を含む統合試験。
- 最終差分のレビュー後、別途許可された GitHub 公開と schedule 有効化。

本段階で許可された公開範囲は、このコード・合成テスト・文書の draft PR のみ。dispatch、schedule 有効化、secret 設定、権限変更、実 API 取得、既存拒否データの転送は行わない。merge は別途承認する。

## GITHUB_TOKEN と追加 credential の役割

| 操作 | 最小権限と判断 |
| --- | --- |
| checkout・checkpoint 読取り | `GITHUB_TOKEN` の Contents read で可能。今回の disabled workflow はこの権限だけ。 |
| 同じ private repo の checkpoint append | 将来 Contents write を明示承認すれば `GITHUB_TOKEN` で可能。PAT は保存そのものの必須条件ではない。 |
| PR 作成 | Pull requests write が必要。repository 側の許可にも依存する。`GITHUB_TOKEN` なら CI の人による承認待ちを受け入れる。 |
| PR CI を公式仕様どおり自動起動 | repository 限定 PAT または GitHub App token。現在の author gate と合う候補は `kkyosuke` の PAT。 |
| checks/runs/status/reviews の照合 | Actions read、Checks read、Commit statuses read、Contents read、Pull requests read。成功 status を書く権限は不要。 |
| 通常 merge | Contents write と保護条件の充足。bypass・Administration は不要かつ禁止。 |

権限は REST endpoint と repository policy の両方で制限される。[reference 更新の権限](https://docs.github.com/en/rest/git/refs#update-a-reference)、[PR 作成の権限](https://docs.github.com/en/rest/pulls/pulls#create-a-pull-request)。combined status と paginated statuses の取得には、いずれも Commit statuses read が必要。[combined status の権限](https://docs.github.com/en/rest/commits/statuses#get-the-combined-status-for-a-specific-reference)、[statuses 一覧の権限](https://docs.github.com/en/rest/commits/statuses#list-commit-statuses-for-a-reference)

必要 secret は `JQUANTS_API_KEY` と、無人 PR CI を選ぶ場合の `AUTOMATION_PAT`。checkpoint 保存だけに PAT を要求しない。App を選ぶ場合は App ID/private key の登録と author 検証の別レビューが必要。token の値や一覧をログへ出さない。`workflow_dispatch` を追加送信して PR CI の承認待ちを迂回する方式は採用しない。

人が行う順序は、公開拒否と未解決 campaign の解消 → 旧 automation 停止・実行中 writer 不在確認 → secret/権限の最小設定 → main 復元検証 → 空の新規 checkpoint branch と writer identity の承認済み初期化 → 模擬試験/コードレビュー → 別途実行承認 → 有効化。現在はこの順序を実行していない。

## 一回限り履歴 workflow の案

履歴 workflow は `workflow_dispatch` のみ、job は常に false、secret 注入なし、Contents read のみ。日次と同じ concurrency group を使う。既存 `jquants_history.py` の 280 営業日・600 request・最大 50 batch × 各 12 回を拡張しない。

将来接続する履歴 adapter は、campaign identity と全 batch の累積予約を外部取得前に同期永続化し、応答ページ・pagination checkpoint を保存してから次へ進む。再開時には同じ identity と累積予算を読み戻す。別 run ID や新 branch で既存予算をリセットしない。外部旧 writer との切替確認、結果不明予約、HTTP 権限エラー、CAS 競合、容量上限、公開拒否、未解決 campaign のいずれでも停止する。タイムアウト前は検証済み checkpoint を残し、次回の明示再開まで停止する。自動 dispatch 連鎖は作らない。

現在の daily adapter は、独立検証された採用済み履歴境界を渡さなければ、履歴 directory/marker をローカルで検出すると Git 読取り前に拒否する。remote `blocked_campaigns` が空でない場合も書込み前に停止する。既存拒否 batch を入力・初期化 seed・復旧素材に使わない。履歴 runner の remote hooks、累積 600 回を維持する予約台帳、snapshot/復元を追加し、合成 fixture で batch 再開を検証した。実サービスの campaign には接続しておらず、この draft の workflow は実行しない。

将来、別途承認された履歴取得が完了し、全件検証・データ PR/CI・main 採用・全 hash 復元確認が成功した後、一時 workflow コードを撤去する PR を提案する。取得 data、journal、checkpoint、CI/検証証跡は削除しない。今回 workflow の撤去や実行はしない。

## 完了履歴から日次への移行：現adapterでは未対応

証拠を渡さないデフォルトの `reject_history` は directory/marker の存在だけで拒否する。これは未解決・拒否済み履歴だけでなく、正当に COMPLETE となり main に採用された履歴にも適用される。したがって、前節の履歴完了後にそのまま日次 workflow を有効化しても動作しない。これは有効化前に解消すべき実装 blocker であり、marker/journal を削除して通す運用は禁止する。

将来の設計は「採用済み terminal history 基点を証拠で束縛する」方式とする。

1. 信頼済み verifier が private repository の採用済み main SHA と archive generation を独立に取得し、別途承認された基点 identity と一致させる。PR 本文、workflow input、取得 branch 自身の主張を承認証拠にしない。
2. その generation を復元し、既存 `validate_history` と archive validator により COMPLETE、固定 campaign identity、対象 session・全 task/range・累積予約予算・marker 整合を確認する。全ファイル hash と journal hash を基点に束縛する。単に `status=COMPLETE` と書かれた journal は受け入れない。
3. 承認台帳で対象 campaign に公開拒否・未解決状態が残っていないことを照合する。実行環境内の自己申告 boolean を根拠に解除しない。現在拒否されている campaign を、この文書変更で承認基点へ登録しない。
4. 日次 snapshot には履歴 journal/marker/immutable revisions を保持する。毎回、その履歴部分が基点と一致することを検証し、日次の新規ページ・revision・journal だけを追加する。履歴証跡の削除・変更、別 campaign への差替え、基点 SHA/generation の不一致は取得・アップロード前に停止する。共通 latest pointer の更新と immutable 履歴証跡の保持は区別する。
5. main 更新時は新しい採用済み generation と履歴基点の継承を再検証する。履歴完了後の一時 workflow 撤去でも、この証拠連鎖は残す。

`history_transition_plan` と合成 fixture は、採用済み COMPLETE、未完了、拒否、identity/hash/検証証跡の欠落・不一致を区別する設計模型である。採用済み分類でも `execution_allowed=false` を返す。新しい verifier はレビュー済み manifest の固定ハッシュ、main のデータ限定差分、remote archive の全hash、独立した handover probe を照合する。採用済み履歴の immutable 証跡を保持する adapter も追加した。ただし旧schedulerの実probe・承認manifestの実配備は未接続で、自己申告 flag を承認には使わない。証拠未指定時の履歴拒否と両 workflow の default-off は維持する。

## 同一予約の二重ACK防止

Git の非 force ref 更新は expected-old-SHA を条件にする真の CAS ではない。同一親・tree・message・作者・時刻から同じ commit SHA が生成されると、同じ target への二つの更新がともに成功し得る。non-fast-forward 拒否だけを予算保証として扱わない。

checkpoint schema v2 は永続 fence と各予約の一意な `reservation_ids` を持つ。予約IDは既存 journal の request と一対一に対応し、削除・差替えを拒否する。各更新では新しい fence を保存し、Git adapter はさらに CAS 試行ごとの UUID nonce を commit message に記録する。同じ予約bodyを二つの caller が提示しても candidate commit は別 SHA となるため、一方が先に更新した ref にもう一方の sibling commit を非 force で採用できない。ACK後の同一fence/body読み戻しが成功した caller だけが取得へ進む。transport は書込みの自動再試行をしない。

合成 GitMock は現実と同じく current target への更新を成功させる。二つの caller が同じ parent と完全に同じ予約bodyを使う交差実行テストで、candidate SHA が二つ、成功ACKが一つとなることを確認する。未知のACK失敗・IN_FLIGHT予約を自動再送しない規則も維持する。UUIDの一意性に加え、将来の実通信adapterで同じ競合条件を隔離統合試験する必要があり、現時点で実サービス実行を有効化するものではない。

## PR #51 後の実装段階と残る有効化条件

| 部品 | 今回実装・検証した範囲 |
| --- | --- |
| `actions_http.py` | GitHub/J-Quants の固定origin、redirect禁止、サイズ上限、JSON重複拒否、固定write/ref範囲、エラー本文を出さない、書込み再試行なし。モックのみで実行。 |
| `actions_authorization.py` | 別途レビュー済みmanifest hashの照合、mainの変更範囲、採用archive全hash、失効、拒否/未解決campaign、writerと独立handover確認。承認manifestを生成しない。 |
| `actions_daily_remote.py` | 日次と履歴が同じcheckpointを共有し、600/50/12台帳を同期保存。未解決予約は停止。採用済みCOMPLETE履歴はimmutable証跡を保持した境界で受入れ可能。 |
| `actions_publication.py` | create-only bootstrap、過去の採用済み予算の引継ぎ、隔離local candidate、全ページのchecks/statuses/runs/reviews、SHA指定通常squash、remote main全hash復元。 |
| `actions_readiness.py` | ネットワーク・credential読出しなしの `STOP_DISABLED` 報告。両workflowはこの診断だけを参照し、jobもfalseのまま。 |

現時点では自動運用は完成していない。特に次の統合が必要であり、`if: false` を外すだけでは取得を開始できない。

- 旧dot daily schedulerと稼働中leaseを独立に読む `handover_probe` の実接続。自己申告や時刻経過で停止確認を代用しない。
- 別途レビュー済み承認manifestと固定hashの配備。現在拒否された履歴branch/dataをseed・承認基点に登録しない。公開拒否解除は独立した承認記録が必要。
- 公開intentを送信前に永続化する `actions_intent.py` と ref/PR/merge の読取り照合を実装した。これを候補作成・CI待ち・採用・復元の全段階へ接続する統合driverは未接続。結果不明のopaque Git object作成は再送せず、operator reviewで停止する。
- `NOOP`、休日、翌日のfinancial補完を、main採用証跡とcheckpointのRESTORED遷移まで結ぶ通し試験。既存日次runnerの処理自体と合成回帰は維持する。
- 認証された実サービスでの非取得・非書込み疎通確認と容量/中断試験。現在はモックで検証した部品であり、実運用での動作保証はしていない。

ユーザー側の次の確認は、既存 `AUTOMATION_PAT` の対象repoと Contents/Pull requests write、Actions/Checks/Commit statuses read が必要範囲内か、有効期限が切れていないかである。値の共有は不要。今回は追加権限を設定しない。既存secretの登録名確認は、有効性・scopeの確認とは別である。

切替順序は、拒否/未解決campaign解消 → 旧dot dailyの停止とwriter不在の独立確認 → writer identity/承認scope確定 → 最新main復元と初期checkpoint検証 → 統合driverの障害試験・レビュー → 別途有効化、とする。今回schedule、dispatch、旧dot設定は変更しない。

既存の `test_guardrail_ownership` と `test_single_repository` は workflow 中の `AUTOMATION_PAT` 参照を禁止している。今回その検証やセキュリティ方針は変更しない。secret 名はコード・文書の接続仕様に記載し、workflow にはまだ注入しない。将来の有効化には、trusted main の collector だけに既存secretを渡し、PR検証には渡さない境界と既存guardrailの整合を別途レビューする必要がある。テストを避けるためのsecret名の難読化や別経路注入は行わない。

取得結果は既存runnerの status・requests_used・batch・retry_not_before と、checkpointのphase/fence、公開部品の WAIT_CI/RESTORED を証跡として返す。secret値・認証header・エラー本文は出力しない。これらを一つの定時実行reportへ集約する処理も統合driver側の残作業である。現在の全snapshot方式は20 MiBのcheckpoint JSON上限で停止するため、280営業日分が収まる保証はない。実データ取得前に、分割object保存または実測に基づく容量設計と中断・再開試験が必要であり、上限を黙って引き上げない。

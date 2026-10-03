# GitHub Actions 日次取得への移行設計（未有効化）

2026-10-03 時点の code-only draft 用。対象は private `kkyosuke/stock.jp.private`、基準 main は `ee5aa843ea6568c9bcac42900d4f5681556f1853`。既存の日次運用手順を置き換える有効な運用設定ではない。

## 現時点の成果物と限界

- `.github/workflows/daily-collector-disabled.yml`: schedule なし、手動イベントのみ、job は常に false。secrets 注入・書込み権限・取得処理なし。
- `scripts/actions_daily_protocol.py`: 外部接続なしの永続予約プロトコル模型。直接実行すると `STOP_DISABLED` を出す。
- `tests/test_actions_daily_protocol.py`: 合成メモリストアによる、停止・再実行・予約・復元境界の検証。
- `stock.jp/scripts/actions_daily_remote.py`: Git Data REST adapter と既存日次 runner の同期 hooks。transport は注入式で、HTTP client・credential 探索・CLI は持たない。
- `stock.jp/tests/test_actions_daily_remote.py`: 実 runner/codec とネットワークモックによる再起動・障害・競合試験。
- `.github/workflows/history-once-disabled.yml`: 一回限り履歴取得の無効な枠。履歴 adapter は未接続。

この変更だけでは自動取得できない。Git REST adapter の呼出し手順、既存 `stock.jp/scripts/jquants_daily.py` の同期予約 hook、応答ページと pagination checkpoint の codec 保存 hook は実装した。実通信 transport、初期 branch 作成、PR/CI/merge/採用後復元の実接続は未実装。模型の真偽値を人為的に true にすることで運用条件を満たしたと扱ってはならない。最終実装では各条件を信頼できる実測から生成する。

PAPER/HUMAN_ONLY、Light、価格 coverage 98%、active coverage 100% と既存の除外基準を維持する。発注や基準・権限・セキュリティ設定の変更は含まない。模型の endpoint は calendar/master/daily bars/TOPIX のみ。接続した remote runner は既存の日次 NAMES をそのまま使用する。指数 scope の未確定事項や EDINET 等を勝手に追加しない。

## 成立条件とユーザー側の作業

1. 未解決の履歴取得・公開拒否を解決する。拒否された既存 batch を Actions へ移して再公開することは禁止。新しい checkpoint 保存先も契約データの保存・公開先として別途確認が必要。
2. 旧定期タスクを停止し、動作中 writer がいないことを確認する。永続 writer 識別も更新し、旧環境による再開を拒否する。旧タスクの停止操作は本変更では行っていない。
3. 信頼する実装をレビューした後、対象 repository の認証情報をユーザーが登録する。想定名は `JQUANTS_API_KEY` と `STOCK_AUTOMATION_PAT`。キー値をログ・成果物へ出さない。EDINET はこの市場データ collector では必須にしない。
4. PR 作成者の既存検証が `kkyosuke` を要求するため、現在の検証に整合する候補は同ユーザーの repository 限定 fine-grained PAT。必要な権限は Contents/Pull requests の read/write、検証用 Actions/Checks/Commit statuses の read と Metadata read。実装時に使用する全 REST endpoint の権限を再照合する。Administration、workflow 書換え、Secrets 読取り権限は collector に与えない。
5. GitHub App は短寿命 token の候補だが、bot 作成者は既存 author 検証に通らない。採用には別途明示的な設計レビューが必要であり、本変更では author 検証を緩和しない。
6. branch protection と必須 review を確認する。人の review が必須なら自動処理はそこで待つ。bypass や admin merge を使わない。実 adapter と統合障害テストを完成後、別途許可された公開・有効化の段階で schedule を追加する。

Secrets のメタデータ確認 `gh secret list --repo kkyosuke/stock.jp.private --json name,updatedAt` は HTTP 403 `Resource not accessible by integration`。存在の有無は確認できておらず、別経路で回避していない。

現行仕様では `GITHUB_TOKEN` による PR の opened/synchronize/reopened は承認待ちの workflow run を作るため、無人で CI を進める前提にはできない。App token または PAT を候補とする。push による一般の再帰的 workflow 起動も抑制される。[GitHub の起動仕様](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)

秘密情報はユーザーが repository/environment の secrets に設定する。コード checkout は credential を残さず、secret を渡す step を信頼済み collector に限定する。PR 検証は credential 不要とし、`pull_request_target` や任意 PR コードを privileged workflow で動かす方式を使わない。[Secrets の取扱い](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)

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
- 実通信 transport と checkpoint branch の承認済み初期化、全証跡収集、休日・採用・復元遷移の実装。
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

必要 secret は `JQUANTS_API_KEY` と、無人 PR CI を選ぶ場合の `STOCK_AUTOMATION_PAT`。checkpoint 保存だけに PAT を要求しない。App を選ぶ場合は App ID/private key の登録と author 検証の別レビューが必要。token の値や一覧をログへ出さない。`workflow_dispatch` を追加送信して PR CI の承認待ちを迂回する方式は採用しない。

人が行う順序は、公開拒否と未解決 campaign の解消 → 旧 automation 停止・実行中 writer 不在確認 → secret/権限の最小設定 → main 復元検証 → 空の新規 checkpoint branch と writer identity の承認済み初期化 → 模擬試験/コードレビュー → 別途実行承認 → 有効化。現在はこの順序を実行していない。

## 一回限り履歴 workflow の案

履歴 workflow は `workflow_dispatch` のみ、job は常に false、secrets なし、Contents read のみ。日次と同じ concurrency group を使う。既存 `jquants_history.py` の 280 営業日・600 request・最大 50 batch × 各 12 回を拡張しない。

将来接続する履歴 adapter は、campaign identity と全 batch の累積予約を外部取得前に同期永続化し、応答ページ・pagination checkpoint を保存してから次へ進む。再開時には同じ identity と累積予算を読み戻す。別 run ID や新 branch で既存予算をリセットしない。外部旧 writer との切替確認、結果不明予約、HTTP 権限エラー、CAS 競合、容量上限、公開拒否、未解決 campaign のいずれでも停止する。タイムアウト前は検証済み checkpoint を残し、次回の明示再開まで停止する。自動 dispatch 連鎖は作らない。

現在の daily adapter は履歴 directory/marker をローカルで検出すると Git 読取り前に拒否する。remote `blocked_campaigns` が空でない場合も書込み前に停止する。既存拒否 batch を入力・初期化 seed・復旧素材に使わない。履歴 runner の remote hooks、累積 600 回の同期予約、履歴専用 snapshot/復元、終了判定の統合試験は未実装であり、この draft では実行できない。

将来、別途承認された履歴取得が完了し、全件検証・データ PR/CI・main 採用・全 hash 復元確認が成功した後、一時 workflow コードを撤去する PR を提案する。取得 data、journal、checkpoint、CI/検証証跡は削除しない。今回 workflow の撤去や実行はしない。

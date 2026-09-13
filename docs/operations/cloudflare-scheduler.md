# 定期起動・同期・監視の自動化

Cloudflare WorkersのCronからGitHub Actionsを起動・監視します。Pythonの株価収集、
private state検証、PR作成は既存のActionsで実行します。Cloudflareへのデプロイは
初回設定後に有効になります。GitHub側のscheduleは予備として残します。

## 自動で行うこと

| 処理 | 起動と停止条件 |
| --- | --- |
| public株価収集 | 平日18:30のGitHub schedule。Cloudflareは30分の猶予後、当該夜のrunがなければmainをdispatchする。失敗は30分以上空けて再試行し、当該夜は合計2 runまで |
| private同期 | Cloudflareが20分ごとにpublic mainとprivate mainのgitlinkを比較。差分があれば更新workflowをdispatch。既存の検証とPR auto-mergeに従う |
| 同期の重複防止 | 実行中run、終了後30分以内、既存pointer PRがある場合は待機。24時間で2回失敗したらCloudflareからの再試行は停止する |
| GitHubの同期予備 | 毎日7:00と平日19:47〜23:47の毎時。Cloudflare未設定・停止中も夜間同期を試みる |
| 運用監視 | GitHubで1:17・7:17・13:17・19:17 JST、同期workflow完了後、運用データのmain更新後。Cloudflareも直近6時間に定期・手動監視runがなければ起動する |
| 異常検知 | workflow失敗・無効化・未実行・3時間超の滞留、3時間超の自動PR停滞・pointer遅延、夜間run漏れ、データ不足、PAPER readinessを確認 |

CronはUTCの毎時7・27・47分です。日次株価の区切りはJST平日18:30で計算し、
日付をまたいだ遅延や週末も前回の平日を引き継ぎます。公開URLは作りません。
GitHubとCloudflareの同時起動競合は、Actionsの既存concurrencyと固定PR branchで処理します。
exactly-once実行を保証する仕組みではなく、価格の同一日再取得とpointer更新の冪等性に依存します。

public mainへマージ済みの変更だけを同期します。data以外のcode・rule変更は既存の
検証で停止するため、人が採用するまで自動同期は進みません。workflowを意図的に無効化した
場合、Cloudflareは勝手に再有効化しません。

## 初回設定

このprivate repositoryのActions Secretsへ次を登録します。値をチャットやGitへ書きません。

| 名前 | 内容 |
| --- | --- |
| `CLOUDFLARE_API_TOKEN` | 対象Cloudflareアカウントだけに限定したWorkers Scriptsの編集用API token |
| `AUTOMATION_PAT` | public/privateの2 repositoryだけを選んだfine-grained PAT。既存の`Contents: Read and write`、`Pull requests: Read and write`を維持し、`Actions: Read and write`を追加する。有効期限を設定する |

Actions Variablesへ`CLOUDFLARE_ACCOUNT_ID`を登録します。private側の既存`AUTOMATION_PAT`を
PR作成とCloudflareからの起動に共用します。対象repositoryにpublic/privateの両方を含めます。
public側の`AUTOMATION_PAT`はpublic専用のまま使い、privateへアクセスできるPATは登録しません。
Cloudflareの処理はこのPATでmainの既存workflowを起動します。

1. この変更をprivate mainへマージする
2. 上のSecretsとVariableを登録する
3. privateの`deploy-operations-scheduler`をmainで手動起動する
4. Cloudflareの`stock-jp-operations-scheduler`でCronと成功ログを確認する
5. privateの`operation-health`をmainで手動起動し、Summaryとartifactを確認する
6. Variablesに`CLOUDFLARE_SCHEDULER_ENABLED=true`を登録する。以後schedulerコードのmain更新時に自動デプロイする

デプロイworkflowはロック済みWranglerでテスト後にコードとruntime Secretを一緒に
デプロイします。Cloudflareへ置くSecretは`AUTOMATION_PAT`だけです。
Secrets未設定の間はpushによるデプロイをスキップし、既存のデータ更新を継続します。

根拠は[Cloudflare Cron](https://developers.cloudflare.com/workers/configuration/cron-triggers/)、
[Secrets設定](https://developers.cloudflare.com/workers/configuration/secrets/)、
[GitHub workflow dispatchの権限](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)です。

## 監視結果と通知

`operation-health`は注意状態をActions Summaryと14日保存のJSON artifactへ残します。
監視ジョブ自体はレポートを保存できれば成功とし、注意内容はSummaryの`ATTENTION`で確認します。
監視はstate・台帳・注文票を
変更せず、夜間runの成功日時や開示カットオフを進めません。

public収集は平日18:30から6時間を猶予とし、その夜の成功runがなければ監視で異常とします。
休日の休場判定を推測せず、価格の採用可否は既存のchecksum・coverage・鮮度検証に従います。
夜間runは既存watchdogの次回予定と2時間の猶予に従います。

GitHubの[Actions失敗通知](https://docs.github.com/en/subscriptions-and-notifications/how-tos/managing-github-actions-notifications)
をメールまたはWebで受け取る設定を一度確認してください。通知先はGitHubの利用者設定に
従い、このコードから別の人やSlackへ通知しません。Cloudflare API/PAT障害はWorkerの
失敗ログにも残ります。Cloudflare自身のCron停止や両サービス同時停止は、この二者だけで
完全には監視できません。

privateのPR用PATは監視jobで疎通と残り14日以内の期限を検知します。起動用PATの期限は
Cloudflareログへ記録します。publicのPR用PATの期限はGitHub側の通知・設定で確認します。
期限の検知には[GitHubの有効期限ヘッダー](https://github.blog/news-insights/product-news/whats-new-from-github-changelog-july-2021-recap/)を使います。

PATの期限前再発行は本人が行います。各repoの`AUTOMATION_PAT`を更新し、private側を更新したら
deploy workflowを再実行してCloudflareにも反映します。

## 夜間のAI調査

この構成はデータ取得・同期・監視を自動化します。夜間の公式資料調査、判断、レポート作成を
実行するAI認証と実行環境は別途必要です。GitHub PATはAIの認証にはなりません。
現状は[夜間運用手順](private-operation-runbook.md)から都度依頼します。

AI実行環境の追加時はprivate mainの同期完了を起点に、既存の
`stock.jp/.agents/skills/japan-stock-operator/SKILL.md`と`nightly_operation.py`を使います。
必要な公式資料と全対象の成果物を検証してからfinalizeします。`start`と`finalize`を
時刻で連続実行するだけでは調査を完了したことになりません。
現在のPAPER・HUMAN_ONLYを維持し、証券会社への注文送信は実行しません。

## ローカル検証

~~~bash
python3 -m unittest discover -s tests -v
node --test cloudflare/operations-scheduler/worker.test.mjs
./scripts/setup-workspace.sh
python3 scripts/operation_health.py --offline --output .usagi/operation-health.json
~~~

`--offline`はGitHub APIだけを省略します。CLIのデフォルトは注意があれば終了code 1ですが、
Actionsは`--report-only`でSummaryとartifactを保存して成功扱いにします。日時境界の検証は
`--at 2026-09-11T08:00:00+09:00`で固定できます。

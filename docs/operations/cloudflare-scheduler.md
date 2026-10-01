# Cloudflare schedulerの退役

日次処理はアシスタントが担当し、Cloudflareからprice収集、private同期、再試行、health dispatchを
起動しません。`worker.mjs`はネットワーク・credentialに触れない停止用互換実装です。
`wrangler.toml`のCronを削除し、`deploy-operations-scheduler`は検証専用にしました。
GitHub Actionsからの自動・手動deployは行いません。

## 稼働中Workerの移行は別作業

**repositoryの変更だけでは、すでにdeployされたWorkerは停止しません。**

2026-09-11の[deploy成功履歴](https://github.com/kkyosuke/stock.jp.private/actions/runs/34652572941)があるため、
未deployとは仮定しません。現在のCloudflare稼働状態は別途確認が必要です。

1. 稼働中のWorker/Cron、現在のcode、既存の起動中workflow・auto-merge待ちPRを確認する
2. 所有者の承認を得てCloudflare Cronを無効化するか、この停止用Workerをdeployする
3. その後のtickとGitHub履歴を確認し、新しいprice/sync dispatchが発生しないことを検証する
4. 既存の起動中処理やauto-merge設定は別途確認し、必要な取消は承認範囲内で行う
5. 旧PATやCloudflare secretの撤去・失効は利用先を確認し、別途承認を得る。値をログやGitへ出さない

旧Workerが一時的に残る間も、両repositoryの互換workflowがmainへ反映済みならprice/syncの
dispatch先はread-onlyです。ただしpublic側の変更が未mergeなら旧収集はまだ動き得ます。
両repoのmergeと稼働中Cron停止を確認するまでは「移行完了」と報告しません。

定期healthはprivate GitHub Actionsのread-only scheduleを使用します。
アシスタントの起動設定と必要な能力は[日次実行契約](assistant-daily-operation.md)に定義します。

# Cloudflare schedulerの退役

廃止済みの `cloudflare/operations-scheduler` と専用の
`deploy-operations-scheduler` workflowをrepositoryから削除しました。
専用NodeテストとCI参照も削除し、通常のGit履歴は保持しています。
指定日の取得はレビュー済みのGitHub Actions経路に一本化します。
定期healthはprivate GitHub Actionsのread-only scheduleを使用します。

## 外部サービスの状態

この削除はrepository内だけを対象にしています。Cloudflareアカウント、
稼働中Worker/Cron、外部secretの変更・削除は行っていません。
2026-09-11の[deploy履歴](https://github.com/kkyosuke/stock.jp.private/actions/runs/34652572941)
は残るため、コード削除をもって外部サービスの停止確認とはしません。
外部側の確認・撤去が必要な場合は別の作業として扱います。

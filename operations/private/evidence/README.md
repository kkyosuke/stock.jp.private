# LIVE gate evidence

このディレクトリはLIVE昇格の個人・実運用証跡を保存するprivate領域です。credential、password、token、cookie、MFA回復code、口座番号は保存しません。金額が不要な項目は割合と確認結果だけを記録します。

## 入力form

次のファイルは未承認の入力formです。`false`、`NOT_DECIDED`、placeholderを残したままではgateを通過しません。

| file | 用途 | 誰が確定するか |
| --- | --- | --- |
| `limited-live-plan.json` | 少額限定実運用の資金上限・損失停止・本人承認 | 利用者 |
| `historical-replay-review.json` | 2025〜2026回顧的stress testの成績・制約受入 | 利用者 |
| `recovery-drill.json` | 現行layoutのclean-clone確認結果 | 実施者 |
| `personal-risk-and-broker.json` | 損失許容、税、broker、security | 利用者 |
| `v04-holdout-review.json` | v0.4集中riskの昇格判断 | 利用者 |
| `v04-holdout-plan.json` | v0.4凍結後の未観測期間と合格基準の事前固定 | 利用者 |
| `live-approval.json` | 全条件後の最終LIVE判断 | 利用者 |

## 生成される合格証跡

`point-in-time.json`、`historical-replay.json`、`official-coverage.json`、`repository-recovery.json`、`personal-risk.json`、`v04-promotion.json`、`live-readiness.json`は検証commandが合格した場合だけ生成します。未達結果を合格証跡として保存できません。`paper-12-months.json`と`shadow-20-days.json`は任意diagnosticであり、LIVE必須証跡には含めません。

証跡を更新した場合は関連するreviewのSHA-256と最終approvalを作り直します。手で `eligible` やpolicy gateだけを変更しません。

合格証跡とreviewは最終approvalより先にcommit・pushします。`live-approval.json`の`private_commit_reviewed`は、それら6証跡を含むprivate commitを指します。approval自体を含めるために循環参照させません。policyの`LIVE`変更はapprovalを別commitでpushした後に行います。

`limited-live-plan.json`は通常LIVE証跡ではなく、`LIMITED_LIVE`の専用制限です。
現在の専用資金上限は3,000,000円で、本人の累積損失停止率、個人checklist、
`capital-ledger.csv`の単一`LIMITED_LIVE_FUNDING`記録、
公式source coverage、layout revision 3のclean-clone、本人承認が未完了のため`DRAFT`です。

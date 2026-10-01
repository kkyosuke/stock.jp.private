# public submoduleの採用

旧`update-public-submodule`は互換用のread-only案内workflowです。cron、PAT、pointer更新、
PR作成、auto-mergeはありません。古い外部dispatchを受けてもデータやrepositoryを変更しません。

## アシスタントの責務

- raw市場データはGit外のprivate localに置き、publicの汎用コード・合成fixtureと分離する
- publicで検証・reviewされ、mainへmerge済みのcommitだけを候補にする
- `scripts/validate-public-auto-update.sh`のdata-only境界を維持する
- code、rule、workflow、schema変更は通常のreviewへ戻す。日次data更新として扱わない
- public pointer、private state、ledger、readiness、PAPER/HUMAN_ONLYを照合する
- 採用したpublic SHAを同じprivate結果commitに記録し、remote保存を検証する

詳細は[日次実行契約](assistant-daily-operation.md)を参照します。
既存`scripts/update-public-submodule.sh`はアシスタントが確認しながら使うローカル補助です。
それ自体にcommit/push/mergeの承認はなく、GitHub Actionsから起動しません。

## 構成変更の初回採用

public側のworkflow・schedule変更はdata-only validatorに拒否されるのが正しい挙動です。
public変更のreviewとmergeを確認し、privateのpointer更新も別途reviewします。
未merge branchへの追従やvalidatorの許可範囲拡大で回避しません。

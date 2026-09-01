# public submodule自動更新

## 結論

public `stock.jp` の日次データPRが検証後にauto-mergeされた翌朝、private repositoryは
マージ済みpublic `main`へsubmodule pointerを自動更新します。人が毎朝pointer PRを作成・
mergeする必要はありません。自動採用する差分は`data/daily-prices/`のCSVと`latest.json`
だけです。code、rule、workflow等の変更は停止して手動reviewへ戻します。

## schedule

`.github/workflows/update-public-submodule.yml`を毎日7:00（Asia/Tokyo）に実行します。
publicの平日18:30データ収集から十分に間隔を空けます。土日等でpublic commitに変更が
なければPRを作成しません。

## merge条件

workflowは次をすべて満たした場合だけpointer PRを作成し、squash auto-mergeします。

1. 現在のpointerからpublic `main`までがfast-forwardである
2. 差分が日次株価CSVと`latest.json`だけである
3. submoduleがpublic remoteの`main`と同じ40桁commitである
4. privateのstateとledgerがvalidである
5. `operation_bootstrap.py check`が`paper_go: true`を返す
6. broker submissionが`HUMAN_ONLY`のままである
7. 検証処理が`operations/private/`を変更していない
8. 生成したPR branch上の必須`format` checkが成功する

code・rule・workflow変更、失敗、競合、権限不足、GitHub障害がある場合はprivate `main`を
変更しません。失敗したjobや未merge PRを人が確認します。workflowは夜間PAPER run、
LIVE昇格、証券会社への送信を実行しません。

## repository設定

`Settings > Actions > General > Workflow permissions`で`Read and write permissions`と
`Allow GitHub Actions to create and approve pull requests`を有効にし、repositoryの
auto-mergeも有効にします。標準`GITHUB_TOKEN`を使用し、永続credentialをGitへ保存しません。
main rulesetは`format` checkを必須にします。標準`GITHUB_TOKEN`で作成したPRは通常の
`pull_request` workflowを再起動しないため、更新workflowが生成branchを指定して
`format-check.yml`を明示dispatchし、GitHubのauto-mergeが成功を待ちます。

## 手動fallback

workflowを待たずに更新する場合はprivate repository rootで次を実行します。

~~~bash
./scripts/update-public-submodule.sh
git diff --submodule=log -- stock.jp
~~~

検証成功後も、手動時は通常のprivate PRとしてreviewしてmergeします。public branch tipや
未merge commitへ直接追従しません。

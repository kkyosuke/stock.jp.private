# public submodule自動更新

## 結論

public `stock.jp` の日次データPRが検証後にauto-mergeされた翌朝、private repositoryは
マージ済みpublic `main`へsubmodule pointerを自動更新します。夜間にも定期確認します。人がpointer PRを作成・
mergeする必要はありません。自動採用する差分は`data/daily-prices/`のCSVと`latest.json`
だけです。code、rule、workflow等の変更は停止して手動reviewへ戻します。

## schedule

`.github/workflows/update-public-submodule.yml`を毎日7:00と平日19:47〜23:47の毎時
（Asia/Tokyo）に実行します。土日等でpublic commitに変更がなければPRを作成しません。
[Cloudflare scheduler](cloudflare-scheduler.md)を初回設定すると、20分ごとの更新検知と
同期起動も有効になります。どちらもマージ済みpublic mainだけを採用します。

## merge条件

workflowは次をすべて満たした場合だけpointer PRを作成し、squash auto-mergeします。

1. checkout時にpublic submoduleを再帰的に初期化する
2. 現在のpointerからpublic `main`までがfast-forwardである
3. 差分が日次株価CSVと`latest.json`だけである
4. submoduleがpublic remoteの`main`と同じ40桁commitである
5. privateのstateとledgerがvalidである
6. `operation_bootstrap.py check`が`paper_go: true`を返す
7. broker submissionが`HUMAN_ONLY`のままである
8. 検証処理が`operations/private/`を変更していない
9. 生成したPR branch上の必須`format` checkが成功する

code・rule・workflow変更、失敗、競合、権限不足、GitHub障害がある場合はprivate `main`を
変更しません。失敗したjobや未merge PRを人が確認します。workflowは夜間PAPER run、
LIVE昇格、証券会社への送信を実行しません。

## repository設定

初回だけ、次の設定を行います。

1. GitHubの`Settings > Developer settings > Personal access tokens > Fine-grained tokens`で
   private更新専用のPATを作成します。Resource ownerはrepository所有者、Repository accessは
   `Only select repositories`でこのprivate repositoryだけを選択し、Repository permissionsの
   `Contents: Read and write`と`Pull requests: Read and write`を付け、有効期限を設定します。
   [Cloudflare scheduler](cloudflare-scheduler.md)でも共用する場合は対象にpublic repositoryも
   追加し、`Actions: Read and write`を付与します。
2. repositoryの`Settings > Secrets and variables > Actions > New repository secret`で
   名前を`AUTOMATION_PAT`、値を発行したPATとして登録します。public用とは別のPATを使い、
   このprivate用PATをpublic repositoryには登録しません。PATはGit、チャット、ログへ保存しません。
   未設定なら検証開始前にSecret名を表示して失敗します。
3. repositoryのauto-mergeを有効にし、mainの必須`format` checkを維持します。
   必須checkをbypassする設定は追加しません。

checkoutと検証はread-onlyの`GITHUB_TOKEN`、PR作成とauto-merge設定は専用PATを使用します。
自動PRが変更するのはsubmodule pointerだけなので、PATにWorkflowsの書込権限は不要です。
期限前にPATを再発行し、同じSecretの値を更新します。失効や権限不足でPR作成が失敗した場合も、
Secret更新後に同じworkflowを再実行できます。
詳細は[PATの作成手順](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)と
[create-pull-requestの必要権限](https://github.com/peter-evans/create-pull-request#token)を参照します。

専用PATで作ったpointer PRの通常の`pull_request`イベントから`format-check`を起動し、
必須`format`成功後にauto-mergeします。PR branchの別途dispatchは行いません。
標準`GITHUB_TOKEN`で作ったPRはチェック実行に承認を要求されるため、代替に使用しません。
根拠は[GitHub公式のworkflow連鎖の仕様](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)です。

readinessが失敗した場合も、そのJSON出力と終了codeをrunログへ表示します。
初回はworkflowを手動実行し、pointer PRの`format`成功、merge、採用した価格日まで確認します。
`update-public-submodule.sh`と`setup-workspace.sh`も`stock.jp/.git`の有無で
初期化状態を判定するため、空の`stock.jp/`を親repositoryのworktreeと誤認しません。

## 手動fallback

workflowを待たずに更新する場合はprivate repository rootで次を実行します。

~~~bash
./scripts/update-public-submodule.sh
git diff --submodule=log -- stock.jp
~~~

検証成功後も、手動時は通常のprivate PRとしてreviewしてmergeします。public branch tipや
未merge commitへ直接追従しません。

publicのworkflow・検証コードを変更した直後は、データ以外の差分があるため自動更新を停止します。
修正済みpublic mainの採用を一度手動reviewした後、日次データだけの更新は自動化に戻ります。

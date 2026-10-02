# 市場データの取得・共有保存・個人利用

現行の正本は単一private repo stock.jp.privateです。stock.jp/は通常フォルダです。
CLIのpack/restore例のdata/はstock.jp/基準、stageの--rootはrepo rootです。
新規licensed uploadの既存拒否はこの構成変更で解除されず、再試行・迂回しません。

現在の正本は非公開 `kkyosuke/stock.jp.private` 内の `stock.jp/data/jquants-archive/` です。stock.jp.privateは
個人の分析・状態・台帳・設定の正本です。旧local-only設計は、この明示された市場データ保存先に
限り置き換えます。repositoryは本人のみ閲覧で、第三者配信を許可する変更ではありません。

[境界と全体構成](../repository-layout.md)および
[codec・復元・公開前検査の手順](../../stock.jp/docs/private-git-market-data.md)を参照してください。

## 設定

`operations/private/source-config.json` の `market_data_git` がprivate Git保存先と
本人限定アクセス確認を記録します。runtime readerの `price_source.storage: private_local` は
復元されたローカル入力形式を指し、永続保存先がローカルだけという意味ではありません。
既存のprivate path confinementとcoverage gateを維持します。

- 取得・復元: `operations/private/market-data/{jquants,daily-prices}/`（Git除外）
- 検証用: `operations/private/market-data-staging/`（Git除外）
- 圧縮正本: `stock.jp/data/jquants-archive/`（同じprivate repoで追跡）
- 個人の分析、選定リスト、保有、判断、run、台帳: stock.jp.privateだけで追跡
- 認証情報: runtimeだけ。設定やarchiveに値を書かない

## 日次の限定取得

定期実行は[日次Light runner](bounded-daily-acquisition.md)を使用します。
下記の全履歴CLIを日次1回分の代わりに呼び出しません。

## 初回と再開

採用済みprivate commitとsource-configを確認し、既存writerがいない状態で実行します。

```sh
python -B stock.jp/scripts/jquants_ingest.py --root "$PWD"
```

初回上限は12リクエストです。exit 2/pending_resumeはcheckpoint付き部分取得です。
全9dataset完了とは報告しません。small batchと各datasetのサイズsampleを確認してから再開します。
容量がordinary Gitに不適切なら停止し、対象datasetを黙って落としたりLFSを購入しません。

圧縮・復元を検証後、stock.jpのstage手順を実施します。private可視性・push権限は直前の
GitHub読取metadataで検証し、本人限定ACLはユーザー確認として記録します。
本人確認と独立したcollaborator列挙を混同しません。rawをPR本文・ログ・artifactへ出しません。
専用branch/non-draft日次PRに保存し、[日次PR手順](daily-pr-publication.md)の意味検証と
exact-head CI成功後にnative auto-mergeを有効化します。remote SHA確認後、未mergeなら「保存済み・未採用」と報告します。
native側のrequired reviewを維持します。stock.jp/のコード変更は通常reviewで扱います。

## 復元・保持・運用

新しいwork areaへ世代indexと全file hashを検証して復元します。既存checkpointへoverlayせず、
旧archiveも自動削除しません。source rootからの脱出、symlink、破損を拒否します。
復元後にruntimeのcoverage、鮮度、checksum、active target readinessを改めて検証します。
CI/static検証、データ保存、個人分析、nightly完了、スケジュール設定は別の完了条件です。

解約・ダウングレードでは取得データ・複製物・復元可能派生物の削除条件が適用されます。
Git履歴、fork、clone、backup、PR参照も対象として棚卸しし、履歴purgeは別途承認で行います。
今回、削除、履歴書換え、broker操作、LIVE昇格、nightlyスケジュール作成は行いません。

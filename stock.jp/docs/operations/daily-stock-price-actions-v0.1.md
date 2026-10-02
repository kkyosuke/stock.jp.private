# 非公開株価入力と読み取り専用Actions v0.1

現行の正本は単一private repo stock.jp.privateです。stock.jp/は通常フォルダです。
CLIのpack/restore例のdata/はstock.jp/基準、stageの--rootはrepo rootです。
新規licensed uploadの既存拒否はこの構成変更で解除されず、再試行・迂回しません。

現在の保存先と責務は[private Git保存手順](../private-git-market-data.md)を優先する。publicは旧称であり、現在の公開範囲を示さない。

## 現在の境界

毎晩21:00頃（Asia/Tokyo）の確認で、利用条件を確認できた一次資料の調査と期限到来レビューを行う。新規の市場データはGit除外の作業領域で取得し、非公開stock.jpのdata/jquants-archive/へ圧縮保存する。保存先変更は収集方法の許諾ではない。Yahoo一括自動収集CLIは停止中で、契約済みJ-Quantsのプラン範囲と安全な資格情報設定を確認してから、専用provider実装で取得する。認証情報をチャットやGitへ保存しない。

全市場株価が足りなければPAPERは`WAIT`。会社IR・開示の調査結果は別のprivate調査記録へ保存できるが、夜間runの成功、架空の約定、価格確認完了として扱わない。98%全市場coverage、active対象100%、checksum、7日以内の鮮度、未来日禁止、一次資料照合、point-in-time、リスク上限、`PAPER` / `HUMAN_ONLY`を維持する。

## 設定と移行

public作業ルートから`operations/private/source-config.json`の`price_source`を明示設定する。

~~~json
{
  "storage": "private_local",
  "archive_root": "operations/private/market-data/daily-prices",
  "manifest_path": "latest.json"
}
~~~

これは既存price_sourceへの追加・パス変更部分だけで、provider identityやcoverage設定を置き換える完全な設定ではない。設定schemaは1.5。validatorは採用providerのsource identityを厳密に検証し、別providerをYahooなどと偽装して通さない。J-Quants V2の全データ取得実装は[専用手順](../jquants-private-ingestion.md)を参照する。実行前に契約範囲と安全なruntime資格情報を確認する。

`archive_root`はprivate workspace内`market-data/`配下の相対パスに限定する。public/private接続用`operations/private` symlinkは対応するが、archiveのsymlinkによる範囲外参照は拒否する。raw CSVとlatest.jsonの展開形はGit追跡しない。検証済み圧縮世代は非公開stock.jpのdata/jquants-archive/で追跡する。バックアップや第三者転送も提供元の許諾と承認を確認する。削除可能な原本と必要最小限の監査metadataを区別する。

1. 提供元の取得・保存・利用条件と許可を確認する。未確認なら取得しない
2. private設定を更新し、利用を許可されたデータだけ指定先へ用意する。既存publicデータは自動コピー・削除・履歴改変しない
3. `python scripts/operation_bootstrap.py check --at <aware-JST-timestamp>`で検証する。欠損・stale・checksum不一致時は停止する
4. privateコード更新とpublic submodule pointer採用をレビューする。両repoはprivateのまま。データは専用private PRの圧縮ファイルのみとし、PR本文、ログ、artifact、cacheへ出さない

`price_snapshot.py`、MRS、shadow evidenceは同じresolverを使用する。指定先が無いときpublicの古い`data/daily-prices/`へfallbackしない。既存publicデータ・履歴は今回削除しない。旧source設定のmigrationもデータを移動しない。

## Actions

public `daily-stock-prices.yml`は手動/PRでコード・合成fixture検証と20日smokeのみを実行する。市場データ鮮度をpublic CIで成功扱いしないため旧23:00 watchdogは撤去する。必要な運用監視はprivate実行環境の明示的なarchiveを検証し、未配置なら失敗する。CI成功はデータ利用許諾・実データのreadiness・夜間処理成功の証明ではない。

全トリガーで`contents: read`、`persist-credentials: false`。収集、privateデータ取得、private状態更新、PR作成、merge、deployは行わない。既存`operation-tests`と必須checkは維持する。publicに既存保存されたデータの削除・履歴改変は別途承認が必要。

定時タスクやprivateストレージ配備は、このコード変更だけで設定済みとは扱わない。実行環境と成功した監視結果の確認が必要。旧`AUTOMATION_PAT`はworkflowから参照しない。資格情報の削除・失効は別途確認する。

# 採用済み市場データのJSON/CSV化

対象はprivate repoのmain `6053ec00cb46d30aa0a7dcc9a2b8d1b6055ff18b` に
採用済みの2世代だけ。新規取得、未採用履歴payloadの取り込み、Git履歴書換え、
branch削除、予算台帳の削除、Actions有効化は行っていない。

## 保存結果

| 項目 | 検証結果 |
| --- | --- |
| 現行世代の論理ファイル | 34（JSON 31、CSV 3） |
| 現行世代の展開量 | 4,281,599 bytes |
| 前世代 | 32ファイル、3,935,913 bytes。これも保持 |
| 保存領域 | 659,048 → 3,931,068 bytes |
| 保存領域のファイル数 | 26（重複データは同一objectを共有） |
| 最大ファイル | 1,330,550 bytes |
| 新しい現行世代 | `184c3410bbd499a6d2f19282bb7f76805aaeab4af374859ead09a644919961cd` |

両世代を旧codecで復元し、新codecで保存・再復元した全バイト列とSHA-256を照合した。
JSONの論理値、CSVの行、provider responseの件数・日付も比較した。
詳細は[検証結果](plain-archive-migration-report.json)に記録する。
JSONをCSVへ変換せず、元の形式と数値・null・文字列を保持している。

`stock.jp/data/jquants-archive/current.json` が現行索引を指す。
`generations/<prefix>/<hash>.json` の `files` に元の論理ファイル名があり、
`object` の参照先が読みやすいJSONまたはCSVの実体となる。圧縮・分割はしない。
コンテンツのhashによる命名と重複排除は維持する。

25MiB/file・750MiB/storeの既存保守的上限を維持する。上限超過時は停止し、
不完全なJSONへの分割、情報を落とす変換、LFS購入を自動実行しない。
[GitHubの通常Gitの上限](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)
である100MiB/fileを下回る。Git履歴に残る旧objectは今回書き換えない。

## readerと旧checkpoint

新writerはversion 2を出力し、readerはversion 1のgzipとversion 2のplainを受け付ける。
旧checkpoint snapshotの復元、plainへの再保存、ledgerを保持したCASをsyntheticで検証する。
実checkpoint branchは読取時に404だったため、復活・再作成しない。
旧Actionの保存形式互換性は残すが、その旧branchを新Actionの起動要件にはしない。

既存承認manifest・hash・所有者確認・baseline制約は維持する。
本PRにはコードと設定の変更があるため、旧scopeで実取得が可能になるとは主張しない。
既存scopeを書き換えたり、失効した制約を黙って迂回する変更は含めない。

## 新しい日付指定Actionの境界

新Actionは旧checkpoint branchを使わず、必要な予約と再開状態を日付ごとのデータPR
branchに置き、採用後にmainの費用記録へ引き継ぐ案を検討する。
恒久的なcheckpoint branchを別名で作り直す案ではない。

所有者の手動起動と承認/hash制御、private保存、共通排他、旧12回を含む
全ページ・再試行・不明結果の計上は必要。未マージbranch削除やrun履歴の欠落時に
新たな予算を発行してはならない。未解決の取得がある間は次の日付を開始しない。
旧payloadの復元を新規取得の前提にせず、費用記録だけを保持する。

このPRは保存形式の実装と移行を扱う。新Actionの実行経路はまだ追加していない。
上記の永続台帳・削除検知・同日再実行の設計を検証せず、既存driverのゲートだけを
省いたコピーを新Actionとして公開することはしない。

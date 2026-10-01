# コード・共有データと個人分析の境界

両リポジトリとも非公開・本人のみ閲覧です。「共有」は本人のプロジェクト間で再利用する意味で、
公開・第三者配信・閲覧権限追加の許可ではありません。過去の文書やscript名のpublicは
stock.jpの旧称です。現在の公開範囲を示しません。

| 保存先 | 内容 |
| --- | --- |
| `stock.jp` | 汎用コード、ルール、研究ツール、合成fixture、共通ドキュメント |
| `stock.jp/data/jquants-archive/` | 契約市場データ、共通の正規化データ、取得由来・checksum・再開checkpointの圧縮世代 |
| `stock.jp.private` | 個人の分析結果、選定・監視リスト、保有・資金、判断、run報告、状態・台帳、個人別設定 |
| Git外のruntime | API key等の認証情報。どちらのリポジトリにも保存しない |

~~~text
stock.jp.private/                     # 個人運用の正本
├── operations/private/
│   ├── source-config.json            # 個人別provider・保存設定
│   ├── state.json, *.csv, runs/       # 状態・台帳・個人判断
│   ├── market-data/                  # Git除外の取得・復元作業領域
│   └── market-data-staging/           # Git除外の圧縮・復元検証領域
└── stock.jp/                          # 非公開コード・共有データの固定submodule
    ├── data/jquants-archive/          # 圧縮世代とatomic indexの保存先
    ├── scripts/, tests/, docs/        # 汎用処理・合成テスト・共通手順
    └── operations/private -> ../../operations/private
~~~

symlinkはstock.jpでGit除外します。市場データを利用する処理は既存resolverを通じて
`operations/private/market-data/`の検証済み復元データを読みます。保存元は明示された
`stock.jp/data/jquants-archive/`で、旧`data/daily-prices/`へのfallbackはありません。
新しいsharedデータへ個人の銘柄選定、保有数量、口座情報、判断、注文を混ぜません。
個人別に絞った出力はstock.jp.privateへ置き、汎用集計と区別します。

## 取得と分析の順序

1. 両repositoryの採用済みSHAと未保存変更を確認し、一つのwriterだけで開始する
2. 安全なruntime認証で契約範囲のデータを有限バッチ取得し、checkpointを保存する
3. 圧縮・checksum・restoreを検証し、private可視性と本人限定アクセスを確認する
4. 市場データをstock.jpの専用branch/non-draft日次PRへ保存し、検証後native auto-mergeを有効化する
5. 通常review後に採用したstock.jp SHAを個人側のpointer更新PRで固定する
6. 個人の分析・PAPER判定は復元済み入力のcoverage・鮮度・readiness確認後に実施する
7. 分析結果、判断、run/state/ledgerはstock.jp.privateへ保存する

データ取得成功だけでnightly完了や投資可能とは扱いません。PAPER/HUMAN_ONLY、risk、
coverage・validity gateは変更しません。検証済み日次PRだけnative auto-mergeを使います。LIVE昇格、broker操作は自動化しません。
Actionsは合成fixture/保存済み状態の検証だけとし、licensed rawを復元・出力しません。

## 復旧と削除

clone・submodule初期化だけでは作業領域は復元されません。圧縮世代を新しい空の領域へ
checksum検証付きで復元し、状態・台帳検証と市場データreadinessを別々に実行します。
GitHubへpushしてremote SHAを確認する前の一時環境は永続保存と呼びません。

解約・ダウングレード時の削除条件はGit履歴、branch/tag/PR参照、fork、clone、backupにも
及びます。通常の削除commitだけでは履歴のデータは消えません。対象を確認し、破壊的な履歴整理は
別途明示承認を得て実施します。今回、既存データ、個人監査記録や公開済み履歴は改変・削除しません。
秘密情報のGit保存は禁止です。privateであっても例外はありません。

[保存の実行手順](operations/private-market-data-storage.md)を参照してください。

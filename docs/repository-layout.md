# リポジトリ全体構成

## 責務

| repository | 正本 | 自動化 |
| --- | --- | --- |
| public `stock.jp` | 汎用コード、ルール、研究、合成fixture、空template | Actionsは合成入力のPR品質検査のみ |
| private `stock.jp.private` | 実保有、資金、注文・約定、判断、run状態、個人の承認 | アシスタントがsubmodule採用・日次処理。Actionsは状態検証とread-only監視 |
| Git外のprivate local領域 | 利用条件を確認したraw市場データ・manifest | 認可済みrunnerだけが取得・検証。remote転送は別途確認 |
| Git外の認証情報管理 | API key、password、token、cookie、MFA回復コード | 実行時だけ環境変数等で注入 |

public Actions は合成fixtureによる検証へ利用し、外部情報収集・更新PR・auto-mergeは行いません。private の保有銘柄だけを抽出した結果、資金量を反映した注文数量、private state は public job の input、log、cache、artifact にしません。

## directory tree

~~~text
stock.jp.private/
├── .github/workflows/             # PR検証・read-only状態監視
├── .gitmodules
├── README.md
├── docs/
│   ├── README.md
│   ├── live-readiness.md
│   ├── repository-layout.md
│   ├── operations/
│   └── reviews/
├── operations/private/
│   ├── operation-policy.json
│   ├── state.json
│   ├── source-config.json
│   ├── market-data/              # Git追跡外。raw archiveとmanifest
│   ├── *.csv
│   ├── decisions/
│   ├── migrations/
│   └── runs/
├── scripts/setup-workspace.sh
└── stock.jp/
    ├── .agents/
    ├── .github/workflows/
    ├── data/
    ├── docs/{architecture,operations,research,rules}/
    ├── operations/templates/
    ├── scripts/
    └── tests/
~~~

`stock.jp` は submodule です。private の1 commit が、実運用状態とそれを解釈した public code commit を同時に固定します。public code をコピーして private 側で派生させません。

## data path

public script は自身の repository root にある `operations/private/` を参照します。setup 後は次の symlink で private 正本へ接続します。

~~~text
stock.jp/operations/private -> ../../operations/private
~~~

symlink は public `.gitignore` の対象なので public commit には入りません。状態・台帳等の実体は親 private repository が追跡します。

市場データは`source-config.json`で`price_source.storage: private_local`と
`archive_root: operations/private/market-data/daily-prices`を明示します。manifestはarchive直下の
`latest.json`です。この領域全体をprivate Gitでも除外します。public archiveへのfallbackはありません。
詳細は[非公開保存・移行手順](operations/private-market-data-storage.md)を参照してください。

## 配置判定

次のいずれかを含むものは private に置きます。

- 個人または口座を特定できる値
- 実保有、実資金、実注文・約定、税
- 現在の監視対象や個別の売買意思
- 個人の損失許容、注文可能時間、GO/NO-GO 承認
- private data source の利用記録や障害証跡

再配布が明示的に許可され、出力が世界中に公開されても問題ないものだけを public に置きます。公開サイトで閲覧できることは再配布許可ではありません。迷う場合は private で扱い、一般化したコードと合成 test だけを public へ戻します。

## 保存禁止

private repository でも次は保存しません。

- credential、secret、秘密鍵、session cookie
- MFA recovery code、one-time password
- 不要な本人確認書類
- Git追跡するraw市場データ・manifest、再生成可能な巨大cache、不要な重複copy

raw市場データのprivate local保存は、このGit保存禁止とは別に取得元の利用条件を満たす必要があります。

secret を commit した場合は削除 commit だけでは不十分です。直ちに失効・再発行し、履歴からの除去と access log の確認を行います。

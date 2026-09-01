# リポジトリ全体構成

## 責務

| repository | 正本 | 自動化 |
| --- | --- | --- |
| public `stock.jp` | 汎用コード、公開データ、ルール、研究、空template | 公開情報の収集、品質検査、backtest、CI、更新PR |
| private `stock.jp.private` | 実保有、資金、注文・約定、判断、run状態、個人の承認 | private checkout 上の日次処理、状態検証、復旧訓練 |
| Git外の認証情報管理 | API key、password、token、cookie、MFA回復コード | 実行時だけ環境変数等で注入 |

public Actions は公開情報だけで完結する重い計算へ利用します。private の保有銘柄だけを抽出した結果、資金量を反映した注文数量、private state は public job の input、log、cache、artifact にしません。

## directory tree

~~~text
stock.jp.private/
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

symlink は public `.gitignore` の対象なので public commit には入りません。実体は親 private repository が追跡します。

## 配置判定

次のいずれかを含むものは private に置きます。

- 個人または口座を特定できる値
- 実保有、実資金、実注文・約定、税
- 現在の監視対象や個別の売買意思
- 個人の損失許容、注文可能時間、GO/NO-GO 承認
- private data source の利用記録や障害証跡

公開情報だけから同じ commit と入力で再生成でき、出力が世界中に公開されても問題ないものは public に置きます。迷う場合は private で扱い、一般化したコードと合成 test だけを public へ戻します。

## 保存禁止

private repository でも次は保存しません。

- credential、secret、秘密鍵、session cookie
- MFA recovery code、one-time password
- 不要な本人確認書類
- 再生成可能な巨大cache、public dataの重複copy

secret を commit した場合は削除 commit だけでは不十分です。直ちに失効・再発行し、履歴からの除去と access log の確認を行います。

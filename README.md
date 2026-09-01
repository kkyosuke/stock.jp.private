# stock.jp.private

`stock.jp` を実資金運用へ段階的に移行するための private workspace です。現在の判定は **LIVE NO-GO** です。最低12か月の PAPER、実データ20営業日の連続運用、履歴再生、復旧訓練、利用者固有の確認が完了するまで実注文を行いません。

## 構成

~~~text
stock.jp.private/
├── docs/                         # private固有の設計、GO/NO-GO、レビュー
├── operations/private/           # 状態、台帳、run、判断証跡の正本
├── scripts/setup-workspace.sh     # submoduleとsymlinkの安全な初期化
└── stock.jp/                      # publicコード・公開データのsubmodule
~~~

public/private の境界は[全体構成](docs/repository-layout.md)、実行順は[private 運用手順](docs/operations/private-operation-runbook.md)、LIVE 条件は[GO/NO-GO 基準](docs/live-readiness.md)を参照してください。

## 初回セットアップ

~~~bash
git clone --recurse-submodules git@github.com:kkyosuke/stock.jp.private.git
cd stock.jp.private
./scripts/setup-workspace.sh
python3 -m venv stock.jp/.venv
stock.jp/.venv/bin/pip install 'xlrd==2.0.2' 'certifi==2026.7.22' PyYAML
stock.jp/.venv/bin/python stock.jp/scripts/operation_state.py migrate
stock.jp/.venv/bin/python stock.jp/scripts/operation_state.py validate
stock.jp/.venv/bin/python stock.jp/scripts/operation_bootstrap.py check
~~~

`scripts/setup-workspace.sh` は `stock.jp/operations/private -> ../../operations/private` を作り、private データの権限を補正します。既存の別ファイルを上書きしません。

## 運用原則

- public は汎用コード、公開データ、ルール、研究、CIを持つ
- private は実保有、資金、注文、判断、状態、GO/NO-GO 証跡を持つ
- private データは暗号化 archive にせず、private Git の通常ファイルとして追跡する
- API key、password、token、cookie、MFA 回復コードは private Git にも保存しない
- GitHub Actions の public job へ private データを入力・出力しない
- PAPER は `PAPER_PROPOSED`、LIVE でも broker submission は `HUMAN_ONLY` のままにする
- readiness が1項目でも未達なら、新規・追加注文を作らない

## 日次の最小確認

~~~bash
git pull --ff-only
git submodule update --init
./scripts/setup-workspace.sh
stock.jp/.venv/bin/python stock.jp/scripts/operation_state.py validate
stock.jp/.venv/bin/python stock.jp/scripts/operation_bootstrap.py check
~~~

成功 run の状態、台帳、採用した public submodule commit を同じ private commit に含め、remote へ push してから別端末や次の run へ進みます。競合した台帳を機械的に片側採用しません。

## 暗号化を使わない方針

`age`、`.zip.age`、backup 用 GitHub Release、復号鍵は新しい運用に使いません。復旧可能性は private repository の clean clone、submodule 初期化、状態検証、台帳照合で四半期ごとに確認します。旧 archive を廃棄する場合は、必要な内容が private Git に移行済みで clean clone から復元できることを先に確認します。

## ドキュメント

- [private ドキュメント索引](docs/README.md)
- [現在の readiness review](docs/reviews/operation-readiness-review-2026-09-01.md)
- [public の汎用ドキュメント](stock.jp/docs/README.md)

Markdown は `npm run lint:markdown` で検査します。

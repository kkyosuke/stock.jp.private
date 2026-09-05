# private 運用手順

## 1. setup

private repository の root で実行します。

~~~bash
git submodule update --init --recursive
./scripts/setup-workspace.sh
python3 -m venv stock.jp/.venv
stock.jp/.venv/bin/pip install 'xlrd==2.0.2' 'openpyxl==3.1.5' 'pypdf==6.16.1' 'certifi==2026.7.22' PyYAML
stock.jp/.venv/bin/python stock.jp/scripts/operation_state.py migrate
stock.jp/.venv/bin/python stock.jp/scripts/operation_state.py validate
~~~

credential は shell history に値を残す引数で渡しません。必要な場合だけ実行環境の変数へ設定します。

## 2. public update の採用

publicの日次data PRはpublic workflow自身が検証してauto-mergeします。privateは毎朝7:00 JSTの
`update-public-submodule` workflowで、merge済みpublic `main`の日次株価差分だけを検証し、
pointer PRをauto-mergeします。code、rule、workflow変更は自動採用しません。詳細は
[public submodule自動更新](automatic-submodule-update.md)を参照します。

自動化が失敗した場合だけ、次を手動実行します。

~~~bash
git -C stock.jp fetch origin main
git -C stock.jp switch --detach origin/main
git add stock.jp
git diff --cached --submodule=log -- stock.jp
~~~

手動時はsubmodule更新と、それに伴うschema migration・運用状態の変更を同じprivate PRで
reviewします。自動・手動のどちらも、検証前のpublic branch tipや未merge commitへ直接追従
させません。

## 3. 夜間 run 前

1. 別端末に未pushの runがないことを確認する
2. `git pull --ff-only` と `git submodule update --init` を実行する
3. `./scripts/setup-workspace.sh` を実行する
4. state/ledger validate を実行する
5. public data manifest の日付、coverage、checksum を確認する
6. `operation_bootstrap.py check` の `paper_go` または `live_go` を確認する

~~~bash
stock.jp/.venv/bin/python stock.jp/scripts/operation_state.py validate
stock.jp/.venv/bin/python stock.jp/scripts/operation_bootstrap.py check
stock.jp/.venv/bin/python stock.jp/scripts/nightly_operation.py status
~~~

PAPER は `paper_go: true`、LIVE は `live_go: true` が必要です。LIVE への昇格前は `operation_mode: PAPER` と `broker_submission: HUMAN_ONLY` を維持します。

## 4. run と commit

`$japan-stock-operator` の汎用手順に従い、1つの run token を完了または失敗まで使います。注文候補は PAPER では `PAPER_PROPOSED`、LIVE でも `PROPOSED` に留め、broker へ自動送信しません。

run 後に次を照合します。

- `state.json` と `run-history.csv`
- `handoff.json` と未完了review
- `orders.csv` と `unreconciled_ticket_ids`
- trade/capital/portfolio ledger の数量と残高
- source coverage と data gap
- `assessment-status.json`と会社別の希薄化・SAM/SOM・10倍経路。`INCOMPLETE`/`STALE`なら不足項目を一次資料から`research-inputs/investment-cases/<code>.json`へ補う
- 変更した private file と public submodule commit

実行の成功・失敗を private Git の1 commitへ記録し、push成功後に次の端末・runへ進みます。認証情報、一時 download、巨大な raw data が混入していないことを diff で確認します。

## 5. 競合

state や ledger の競合で片側を機械的に採用しません。

1. 両 branch の run ID、run token、source cutoff を一覧化する
2. order proposal、fill、cancel、expire を event ID 単位で照合する
3. portfolio と capital を event ledger から再計算する
4. 未照合 ticket と次回 handoff を統合する
5. validate と readiness を再実行する
6. 解消根拠を private review logへ残す

同じ口座を複数端末から同時運用しないことを基本とします。

## 6. clean-clone 確認

LIVE昇格前に一度、稼働中とは別の空ディレクトリで実施します。setup、submodule、状態schema、台帳構成を変えた場合はpublic validatorの `REPOSITORY_LAYOUT_REVISION` を上げ、変更後に再実施します。日次run、データ更新、経過日数だけでは再実施しません。

1. 通常のprivate repositoryをcloneする
2. submodule と symlink を setup する
3. Python environment を再構築する
4. state/ledger validate と readiness check を実行する
5. 最新成功 run、handoff、未照合ticket、全ledgerを稼働中と照合する
6. layout revision、private/public commit、実施日時、結果、差異をevidenceに残す

別媒体のmirrorは要求しません。稼働中のdirectoryを上書きしません。clean cloneできない間、またはGitHubを利用できない間は、PAPER/LIVEとも新しい注文候補を作りません。

## 7. access control

- repository visibility を Private に固定する
- collaborator と GitHub App を定期reviewする
- GitHub account と broker account で強い固有passwordとMFA/passkeyを使う
- broker site はbookmarkから開き、URLを確認する
- 不審なlogin、注文、出金を確認したら取引を止めてbrokerへ連絡する

口座securityは strategy code の gate とは別に、[personal risk and broker check](../live-readiness.md#personal-risk-and-broker-check)の証跡へ記録します。

## 8. 公式参照

- [金融庁: インターネット取引サービスへの不正アクセス・不正取引への対策](https://www.fsa.go.jp/ordinary/chuui/chuui_phishing.html)
- [JPX: 内国株の売買制度・売買時間](https://www.jpx.co.jp/equities/trading/domestic/01.html)
- [JPX: 休業日一覧](https://www.jpx.co.jp/corporate/about-jpx/calendar/)

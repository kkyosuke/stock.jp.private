# 日次PR・auto-mergeの運用

本人の承認により、日次データ・生成済み個人分析/run結果はnon-draft PRを作り、
意味検証とexact-head CI成功後にnative auto-mergeを有効化します。mainへ直接書き込みません。
実装と詳細手順は[共通validator・手順](../../stock.jp/docs/daily-pr-publication.md)を正本とします。
共通validatorは[stock.jp PR67](https://github.com/kkyosuke/stock.jp/pull/67)でreview後にmerge済みです。
この変更はmerged commit `91682b33004c06cb9a3ab5c63c3da595503d0f90`を通常reviewで採用します。

| 対象 | 保存先 | 必須check | auto-merge条件 |
| --- | --- | --- | --- |
| 市場データの圧縮世代 | stock.jp/data/jquants-archive | test、guardrail | codec復元・hash・容量・provider/status・全CI成功 |
| 当日completed PAPER分析/run結果 | stock.jp.private/operations/private | format | 個人path allowlist・state/history整合・run-integrity・全CI成功 |
| コード、依存、設定、risk設定、policy、security、金融台帳、submodule pin | 通常の適切なrepo | 既存checks | 対象外、通常review |

branchは`daily/market/YYYY-MM-DD`または`daily/personal/YYYY-MM-DD`で一つだけです。
`trade-event-ledger.csv`は当日ticketと一致するPAPER_PROPOSEDの追記だけ例外として許可し、
約定・broker提出・手数料・資金移動・既存行の書換えは拒否します。
生成済みMRSの新規入力証拠とlog追記は、既存履歴保持・設定一致・隔離再計算に合格した一組だけ
許可します。risk設定や過去risk記録の変更を許可するものではありません。
stateはcompleted handoffから既存transitionを隔離再実行し、全fieldの一致を検証します。

対象外fileが混じったら同じPRを自動mergeせず、日次結果と通常reviewへ分離します。
完了していないrunの監査保存PRは作成可能ですが、自動mergeの対象にはしません。
部分取得データのPRはpartialと明記し、nightly成功や全履歴完了とは扱いません。

## 実施者と権限

アシスタントが認可済みGitHub connectorでPR作成とauto-merge有効化を行います。
既存AUTOMATION_PATは承認済みのstock.jp checkoutだけに使い、取得・再利用しません。
Actionsはread-only guardrailです。workflowにwriter権限、merge処理、収集処理を追加しません。
repo設定・branch protection・ruleset・required reviewを変更せず、admin bypassもしません。

必須checkがserver側で強制されていると確認できなくても、assistant側で全CIのterminal successを
待ってからenableします。最新head/base、全remote path、GitHub Actions由来のcheck、追加status、
全workflowを直前に再確認し、変化があれば再検証します。即時merge endpointへfallbackしません。
`enable_auto_merge`にexpected head/base SHA引数はありません。直前再読取とsingle writerは
best-effortであり、readとenableの間をatomicに保証しません。保護設定は維持します。
required reviewが残る場合、native auto-mergeは待機のままにして正直に報告します。

## 現在の状態

この変更はコードpin採用と運用手順の通常review用PRです。導入PR・依存更新PRにはauto-mergeを付けません。
日次PRの作成・有効化・mergeは各runで実測して報告します。日次スケジュール登録、
初回全履歴取得、初回nightly完了は別の作業で、設定済みとは主張しません。

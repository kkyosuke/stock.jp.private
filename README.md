# stock.jp.private

`stock.jp` を実資金運用へ段階的に移行するための private workspace です。現在の判定は **LIMITED_LIVE/LIVE NO-GO** です。LIMITED_LIVEは3,000,000円の専用資金上限を事前記録済みですが、本人の損失停止と承認、公式coverage、clean-clone、個人checklist完了までPAPERを維持します。通常LIVEには履歴再生とforward holdoutも引き続き必要です。

## 構成

~~~text
stock.jp.private/
├── docs/                         # private固有の設計、GO/NO-GO、レビュー
├── .github/workflows/             # PR検証・read-only運用監視
├── cloudflare/                    # 旧schedulerの停止用互換Worker
├── operations/private/           # 状態、台帳、run、判断証跡の正本
├── scripts/live-gate-status.sh    # 全LIVE条件のread-only確認
├── scripts/setup-workspace.sh     # 単一repo内のsymlink初期化
└── stock.jp/                      # 共通コード・追跡済みデータの通常フォルダ
~~~

public/private の境界は[全体構成](docs/repository-layout.md)、実行順は[private 運用手順](docs/operations/private-operation-runbook.md)、LIVE 条件は[GO/NO-GO 基準](docs/live-readiness.md)を参照してください。

日次処理の主体は、この会話のアシスタントです。許可された株価収集、一次資料調査、入力の検証・採用、
PAPER判定、private結果保存を一つのrunとして担当します。GitHub ActionsはPR検証と
read-only監視だけを担当し、収集、更新PR作成、auto-merge、夜間run開始を行いません。

目標時刻は毎晩21:00（Asia/Tokyo）です。**この変更だけでは定期実行は設定されません。**
実行環境・権限・永続保存・スケジュールの確認後に有効化します。
[アシスタント日次実行契約](docs/operations/assistant-daily-operation.md)を正本とします。
既存Cloudflareの停止は[移行手順](docs/operations/cloudflare-scheduler.md)に従い、
稼働中環境を確認して別途実施します。

## 初回セットアップ

~~~bash
git clone git@github.com:kkyosuke/stock.jp.private.git
cd stock.jp.private
./scripts/setup-workspace.sh
python3 -m venv stock.jp/.venv
stock.jp/.venv/bin/pip install 'xlrd==2.0.2' 'openpyxl==3.1.5' 'pypdf==6.16.1' 'certifi==2026.7.22' PyYAML
stock.jp/.venv/bin/python stock.jp/scripts/operation_state.py migrate
stock.jp/.venv/bin/python stock.jp/scripts/operation_state.py validate
stock.jp/.venv/bin/python stock.jp/scripts/operation_bootstrap.py check
./scripts/live-gate-status.sh
~~~

`scripts/setup-workspace.sh` は `stock.jp/operations/private -> ../../operations/private` を作り、private データの権限を補正します。既存の別ファイルを上書きしません。

## 運用原則

- stock.jp は非公開の共通コード・市場データ・汎用研究ツールを持つ。「共有」は本人のプロジェクト間での再利用で、第三者への配信許可ではない
- private は実保有、資金、注文、判断、状態、GO/NO-GO 証跡を持つ
- private の状態・台帳・判断は暗号化 archive にせず、private Git の通常ファイルとして追跡する
- raw市場データはGit除外の作業領域で取得・検証し、圧縮世代を同じrepoの`stock.jp/data/jquants-archive/`へ保存する。個人の分析結果・選定・判断・台帳はstock.jp.privateに置く
- storage設定だけでは取得を許可しない。確認済みJ-Quants Lightを設定し、全契約対象データの取得実装を用意する。実取得には安全なruntime資格情報登録と明示したprivate Git保存先が必要。Yahoo bulk取得は停止する
- 会社別の希薄化・SAM/SOM・競合・KPI入力は`operations/private/research-inputs/investment-cases/`に置き、publicには値を出さない
- API key、password、token、cookie、MFA 回復コードは private Git にも保存しない
- GitHub Actions の public job へ private データを入力・出力しない
- PAPER は `PAPER_PROPOSED`、LIMITED_LIVE/LIVE でも broker submission は `HUMAN_ONLY` のままにする
- readiness が1項目でも未達なら、新規・追加注文を作らない

## 運用開始後の全体フロー

1. アシスタントがprivate main、採用済みprivate commit、前回runと未push結果を確認する
2. アシスタントが取得元の利用条件で許可された市場データを非公開領域で収集・検証し、一次資料を調査する
3. 市場データはstock.jp/data/、個人分析・保有・監視対象・判断・台帳・runはstock.jp.privateへ分離する
4. 検証・review済みprivate mainだけを採用し、PAPER readinessと全対象の調査を確認する
5. 同一run tokenでPAPER処理を完了し、状態・台帳・証跡・採用コードのtree SHAをprivateへ保存する
6. remoteへの保存を確認して、結果と不足・次回actionをこの会話で報告する

資料欠損、競合、認証・実行環境の不足、readiness未達では`WAIT` / `NO-ACTION`とします。
成功した開始処理だけを「調査完了」と扱いません。PAPER注文を証券会社へ入力しません。
本人の承認、実資金・損失許容の設定、LIVE移行と証券会社での操作は日次委任に含みません。
定期reviewと将来のLIVE手順は[詳細スケジュール](docs/operations/post-launch-operation-schedule.md)を参照してください。

## 日次の最小確認

~~~bash
git pull --ff-only
./scripts/setup-workspace.sh
stock.jp/.venv/bin/python stock.jp/scripts/operation_state.py validate
stock.jp/.venv/bin/python stock.jp/scripts/operation_bootstrap.py check
~~~

成功 run の状態、台帳、採用したコードのtree SHA を同じ private commit に含め、remote へ push してから別端末や次の run へ進みます。競合した台帳を機械的に片側採用しません。

`live-gate-status.sh` は判定専用で、policyやevidenceを変更しません。未達項目が1つでもあれば終了code 1を返します。現在はLIVE条件が未達なので、この終了codeが正常です。

## 暗号化を使わない方針

`age`、`.zip.age`、backup 用 GitHub Release、復号鍵は新しい運用に使いません。LIVE 前に一度、または repository layout の変更後に、private repository の clean clone、workspace setup、状態検証、台帳照合を確認します。別mirrorと日数による再確認は要求しません。旧 archive を廃棄する場合は、必要な内容が private Git に移行済みで clean clone から復元できることを先に確認します。

## ドキュメント

- [市場データの非公開保存・移行手順](docs/operations/private-market-data-storage.md)
- [private ドキュメント索引](docs/README.md)
- [現在の readiness review](docs/reviews/operation-readiness-review-2026-09-01.md)
- [stock.jp の共通ドキュメント](stock.jp/docs/README.md)
- [MRS・希薄化・SAM/SOM・10倍経路の計算](stock.jp/docs/operations/decision-assessment-automation-v0.1.md)

Markdown は `npm run lint:markdown` で検査します。

## 日次PRの保存

日次市場データはstock.jp、完了検証済み個人PAPER結果はstock.jp.privateへnon-draft PRを作成し、
[検証手順](docs/operations/daily-pr-publication.md)に従ってnative auto-mergeを有効化します。
コード・依存・設定・risk・policy変更は通常reviewです。mainへ直接pushせず、required checksと
reviewを維持します。この方針だけでは日次スケジュールは作成されません。

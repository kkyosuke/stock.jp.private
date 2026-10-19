# 自動化

GitHub Actionsの入口は `.github/workflows/`、取得設定は
[`config/sources.json`](../../config/sources.json)、取得・保存処理は
[`automation/nightly.py`](../../src/stock_jp/automation/nightly.py) が正本。

| Workflow | 責務 |
| --- | --- |
| `ci.yml` | 必須チェック`format`の1ジョブで書式・テスト・PAPER検証。差分から対象を選択。手動実行は全件 |
| `ci-full.yml` | 日曜04:17 JSTに全テスト。280日・4,000銘柄・600予約などの上限検証も実行 |
| `market-data-manual.yml` | 毎日21:00 JSTに1回、市場データを取得・検証・保存。取得後に一次資料収集とPAPER調査準備 |
| `operation-health.yml` | 毎日23:17 JSTに保存済みPAPER runメタデータの監視結果をartifactへ保存 |
| `forward-holdout.yml` | 22:30 JSTに凍結したPAPER研究と採用済みデータを読み取り、不足・実装変更をartifactへ保存 |

依存関係は `pyproject.toml` を参照する。夜間取得は実行commitと採用済み市場データを
checkoutする。CIはGitの全差分を比較し、アーカイブ・観測CSV・夜間取得記録だけの変更では
市場データをcheckoutして保存世代・ハッシュ・取得記録・観測ビューを検証する。
コードのPRでは変更前後のPython依存関係から回帰テストを選び、compile・smokeを行う。
設定・workflow・共有入口などの変更は通常回帰を全件行う。READMEと`docs/`のMarkdownだけなら
文書の契約テストを行うが、凍結ルールの`docs/rules/`は通常回帰の対象にする。
差分の基点が不明な場合と手動CIでは全テストを行う。PR検証済みの変更をmainへmergeしても
CIを重ねて起動しないため、コード変更は必須チェック付きのPR経由で反映する。
運用監視は市場データをcheckoutしない。CIの成功は実データの準備完了を意味しない。
forward holdoutの観測は採用済みデータもcheckoutする。正式入力が不足した開始日以降は
失敗として記録するが、市場データの取得・保存・本人のLIVE承認は行わない。
固定した条件と現時点の不足は[正式検証の記録](../../research/formal-validation-2026-10-05/README.md)を参照する。

## Actionsの使用分数

月3,000分の枠では、壁時計の待ち時間ではなく、各jobを分単位で切り上げた合計を管理する。
集計仕様は[GitHubの料金説明](https://docs.github.com/en/billing/reference/actions-runner-pricing)を参照する。
2026-10-01〜20日朝の履歴では、データ更新を除くPRの異なるheadは140件だった。
同じ開発ペースを31日に延ばし、通常コードCIを月220回として見積もる。

| 用途 | 月の回数 | 1回の使用分数の目安 | 月の使用分数 |
| --- | ---: | ---: | ---: |
| コードPRの通常CI | 220 | 5 | 1,100 |
| 日次データのCI | 31 | 2 | 62 |
| 日次取得・保存・CI待機 | 31 | 15 | 465 |
| 一次資料・PAPER調査準備 | 31 | 12 | 372 |
| 保存済み状態の監視 | 31 | 1 | 31 |
| forward holdout観測 | 31 | 1 | 31 |
| 週次全テスト | 5 | 9 | 45 |
| 合計 | | | 2,106 |

この目安では894分を手動の再開・履歴取得・実行時間の変動に残せる。
上表では全220回を通常回帰の全件実行として数え、依存関係による選択と資料だけのPRによる
追加削減を含めない。書式・テスト・PAPER検証を1ジョブにまとめることで、
書式とPAPER検証それぞれの分単位切り上げ、checkoutと依存導入の重複を減らす。
既存のrulesetが必須にする`format`の名前を保ち、どの検証の失敗も同じチェックを失敗させる。
統合前の実測は3ジョブで切り上げ合計6分、統合後の広い通常回帰は1ジョブ4分7秒で5分だった。
runner性能による変動を見込み、上表は通常CIを5分としている。
日次経路の新しい実績は適用後に確認する。GitHubの請求明細、他repositoryの使用量、
追加の手動実行はこの推計に含まない。通常CIが1分延びると月220分増えるため、
追加テストの実行時間もPRで確認する。

通常回帰の候補から毎回除くのは[`tests/ci_suite.py`](../../tests/ci_suite.py)に列挙した上限規模の4件だけ。
直近の全件実行510秒のうち、この4件で297秒を占めた。小規模の履歴・保存・認可・
予算境界の回帰は通常回帰の候補として残し、変更に関係する場合に実行する。
テストのrenameや削除で上限テストが消えた場合、一覧の不一致としてCIを失敗させる。
週次を待たずに全件を確認する場合は「Weekly full regression」または「CI」を手動実行する。

[`tests/ci_selection.py`](../../tests/ci_selection.py)はGit差分を省略せず読み、baseとheadの両方の
import・モジュール名文字列・ソースパスから、変更の影響を受けるテストとその利用側を選ぶ。
動的importの利用側は全ソース変更で対象にし、選択処理・workflow・PAPER方針などの基本検証も毎回残す。
新規テストは自動検出する。設定・fixture・共有入口の変更、対応するテストがない変更、
解析失敗、未commitのローカル変更は通常回帰の全件へ戻す。選択理由と対象一覧はCIログに出す。
静的な依存解析では実行時の結合を全て証明できないため、週次と手動の全件検証を併用する。

```bash
# 全通常回帰（上限規模4件を除く）
python -m tests.ci_suite --profile code
# commit済みの差分から選択
python -m tests.ci_suite --profile code --base <base-sha>
# 上限規模も含めて全件
python -m tests.ci_suite --profile full
```

毎日の21:20の無条件再実行は廃止する。成功後に同じ取得・研究を再実行しない。
失敗時は保存されたcheckpointとjobのログを確認し、必要なjobだけを手動で再開する。
定時取得jobは20分、PAPER準備は15分、監視は3分、holdoutは5分、週次全件は15分で停止する。
取得job内のCI待機も5分までとし、未完了・失敗したデータをmergeしない。
途中で停止した場合は成功扱いにせず、保存済み取得記録と消費予算を引き継ぐ。
大量の手動履歴取得だけは従来の90分枠を残すので、必要な対象日を指定して使う。

## 夜間取得

privateリポジトリのownerが管理するmainで動かす。GitHubへの保存・PR作成・mergeと
CI起動にはActionsが発行する一時的な `GITHUB_TOKEN` を使い、`AUTOMATION_PAT` は不要。
夜間workflowだけに `contents: write`、`pull-requests: write`、`actions: write` と
CI結果確認用の `checks: read` を付与する。CIと監視の権限は読み取り専用のまま。
repositoryの `Settings > Actions > General > Workflow permissions` で
`Allow GitHub Actions to create and approve pull requests` を有効にする。
GitHub Secretにはデータ取得用の `JQUANTS_API_KEY` だけを登録する。
取得の有効化は `config/sources.json` の `market_data_git.automatic_upload` で管理する。

1. 採用済み世代を復元・検証し、通信前に消費記録をcheckpoint branchへ保存する。
2. Light契約の公開時刻に従って日足・master・財務・カレンダーなどを取得する。
3. 財務・株数・予想・決算予定・価格調整の観測CSVを更新し、検証した保存世代と取得完了記録のPRを作る。
4. 全ファイルの差分が生成済みのデータと一致することを確認し、そのPRの承認待ちCIだけを起動する。
5. 同じPR・branch・head・workflowのrun IDとcheck suiteを追跡し、必須チェック`format`と
   workflow全体が成功した後、同じheadをsquash mergeする。書式・データ整合性・PAPER検証の全てを含む。

`GITHUB_TOKEN` で作成したPRのCIは承認待ちになる。検証済みデータ差分・同じhead・同じPR・
bot起動のCIだけを承認し、mainの必須チェックに紐づくPR側CIを1回実行する。
データ更新のために同じCIを別途dispatchしない。再試行時も同じheadの結果を確認する。
承認APIの拒否、PR側CIの失敗・未完了でも採用せず、保護ルールを変更しない。
詳細は[GitHubのトークン仕様](https://docs.github.com/en/actions/concepts/security/github_token#when-github_token-triggers-workflow-runs)を参照する。
承認失敗、run ID不明、対象headの不一致、CI失敗・未完了ではmergeしない。
標準トークンのAPI上限に合わせた通信予算は
[`automation/rate.py`](../../src/stock_jp/automation/rate.py) で管理し、再実行にも引き継ぐ。

J-Quantsは直列・最低1.05秒間隔、日付別に最大12回。失敗・不明応答も消費に含め、
再実行で予算をリセットしない。過去の `date-grants.json` の日付別消費も引き継ぐ。
取引がない日にはカレンダー等を確認し、架空の日足を作らない。

消費記録と取得途中の応答は `automation/nightly-market-data` branchに保存する。
削除すると予算の根拠が失われるため、削除しない。途中でmainが更新された場合、
認証失敗、429、不正な応答、予算不足、CI失敗の場合は処理を停止し、保存済み状態を残す。
実行結果は `nightly-market-data-<run ID>` artifactのJSONで確認する。

一時トークンからのイベントの扱いは
[GitHubのWorkflow起動仕様](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)
を参照する。

## 手動実行・再開

Actionsの「Nightly market data」で対象日を指定する。未指定なら実行時のJST日付。
未完了の対象日がある場合は先にその日を再開する。J-Quantsの認証失敗は
`JQUANTS_API_KEY` を修正し、GitHubの権限不足はworkflowのpermissionsとrepositoryの
PR作成許可を確認してから、対象日を指定して手動実行する。消費記録を消して再試行しない。

ownerの手動再実行では、公開前に作った`WAIT_PUBLICATION`について、復元したjournalと
checkpointがともに要求0・task0・過去消費0である場合だけ現行設定へ接続できる。
予算は変えず、旧状態と新設定のハッシュを`jquants/rebindings/`へ保存する。
要求・取得結果があるrunや定時実行は設定変更を越えて再開しない。

`preview=true` は取得・checkpoint保存・世代の検証後に、検証用draftデータPRを作って
CIを確認する。mainへ採用しない。修正branchでもownerが手動実行できる。
検証用branchは `daily/market/<日付>-preview`。確認後、draft PRを閉じる。
previewでも実際の通信は消費として残り、本番の再実行で完了済み取得を再利用する。

## 一次資料とPAPER調査準備

市場データjobが成功した後、`paper-research` jobが最新mainを復元する。
企業IR・公開資料の選択は`operations/inputs/primary-sources.json`で管理し、VI・景気統計の
取得URLは`config/sources.json`の`market_regime`を正本とする。原本保存は
`market_data/primary_sources.py`、取得記録は`research/forward_collection.py`が管理する。
公開資料も直列・最低1.05秒間隔、1回最大60要求。J-Quantsの取得とは別の消費記録である。
EDINETも直列・最低1.05秒間隔。J-Quantsを使うsource scanは既存の最低1.05秒間隔・
`config/sources.json`のplanと1回の取得予算（最大12要求）を使う。
予算停止時は未完了として保存し、scannerを作り直して取得枠を増やさない。
同夜の同じplan・cutoffの検証済みページは再照会せず、要求数とcache hitを結果に残す。
同じworkflowのconcurrencyで市場データ取得と調査準備の同時実行を防ぐ。

```bash
python -m stock_jp.research.forward_collection --output .work/paper-research/collection.json
python -m stock_jp.operation.paper_preparation --output .work/paper-research/preparation.json
```

`paper_research=true`の手動実行は市場データ取得を省略して、この準備を実行する。
PR branchでの確認は`preview=true`も指定する。Secretsは既存の`JQUANTS_API_KEY`と
`EDINET_API_KEY`を使う。PAPER/HUMAN_ONLYを必須とし、本人の承認は生成しない。
通常のrun開始は21:00 JST以後。朝は`WAIT_PUBLICATION`としてcutoffを作らない。
ownerの明示previewだけが公開前の試行を許可する。朝の試行をmainの当日runへ採用せず、
検証資料として保存する。夜間の正式なrunは公開後に開始する。

準備処理は`operation run start`と同じ入口で実データを調べ、未完了のレビューを
保存する。`REVIEW_REQUIRED`はexit 1であり、成功した開示cutoffやholdout回数を進めない。
調査・判断が終わるまで`run finalize`を呼ばない。取得job成功と研究jobの未完了は
Actionsのjobごとに確認する。

当日のcanonical runが保存済みなら、自動準備は通信・再採点・lease取得を行わず、
レビュー成果物をそのまま保持する。未完了は`REVIEW_REQUIRED`と`resume-review`の
実行引数を返す。採用済み作業領域でのレビューと日次完了は[runbook](runbook.md)の
オフライン再開手順に従う。完了済みrunは`ALREADY_COMPLETED`として扱う。

`automation/paper-source-inbox` branchへ、原本・未完了run・forward記録・J-Quantsページを
内容ハッシュ付きの追記snapshotとして保存する。期限のあるartifactに長期保存を依存しない。
このjobの`contents: write`はこの未レビューの保管先に使い、mainを更新しない。
次回は設定ハッシュとページのcutoff・query・原本ハッシュを検証し、query cacheを復元する。
一次資料は同じlogical day・固定cutoffの追記snapshotを検証して復元する。
最新inboxだけでは前attemptの成功資料が欠けるため、同日の過去snapshotも照合する。
collection receiptのSHA256、Git blob、原本の長さ・SHA256、設定済みURL、issuer feedの
公表日時・文書ID・対象企業を照合し、改変・不明資料・同URLの矛盾する原本は停止する。
復元結果は`.work/paper-research/primary-reuse.json`へ束縛し、collectorは再検証後に再利用する。
元の取得時刻と未レビュー状態を保持し、欠落資料だけが既存の共有予算境界へ進む。
cache hitは課金を返却せず、共有12要求上限と既存charge/receiptを変更しない。
復元対象は同夜に再開できるcutoffのscopeだけ。過去分はGitのsnapshotへ残し、
翌夜のcacheや取得・復元量へ累積させない。
運用state、判断、注文、承認は復元しない。同日の21:00以後の検証済みcutoffだけを
再開メタデータとして返し、手動の再試行でquery pageを再利用する。朝のcutoffは
ownerの明示previewでしか再利用しない。checkpoint branchを削除しない。

`paper-research-<run ID>` artifactは90日保持する受け取り口。repositoryの実上限も90日であり、
400日指定でも90日に切り詰められる。取り込み後に再検証し、privateのPRで採用した
Gitの入力と結果を正本にする。保管したsnapshotの`review_completed`は常にfalse。

## 財務履歴の初回補完

同じ「Nightly market data」を手動実行し、`target_date=2026-10-02`、
`financial_history=true` を指定する。修正branchで確認する場合は `preview=true` も指定する。
Light契約で取得できる2021-10-02〜2026-10-02の財務サマリーと決算予定をbulkで補完する。
株数と業績予想は財務サマリーから同じ変換処理で抽出する。

要求は直列・最低1.05秒間隔、最大256回。失敗も消費に含め、再開でリセットしない。
既存の日足履歴の600回予算は変更しない。nightlyと同じconcurrency groupで同時取得を防ぐ。
生CSVのgzip・サイズ・ハッシュを検証し、解析前に保存する。
同じ対象日で再実行すると保存済みファイルを再利用する。

中間保存先は `automation/financial-history` branch、結果と入力は
`financial-history-<run ID>` artifact。branchは消費記録を含むため削除しない。
初回補完はmainへ自動採用せず、artifactの `market-data/jquants/financial-history/` を
`.work/market-data/jquants/financial-history/` に取り込み、
`data observations`、`data stage`、`data verify` を通してprivateのPRで採用する。
採用後の日次追加は通常の夜間処理が行う。

この入口は市場データの取得を担当する。調査・売買判断・PAPER runの完了は別工程で、
取得結果の `analysis_readiness` は `NOT_EVALUATED`。
運用は `PAPER` / `HUMAN_ONLY` を維持する。

旧 `config/acquisition.json`、`config/automation.json` と引退記録は過去の承認・消費の
証跡として保持する。夜間入口は旧deployment pinや `STOCK_ACTIONS_*` 変数を使わない。

## 監視

```bash
python -m stock_jp automation validate
python -m stock_jp automation health --persisted-only --output .work/operation-health.json
```

監視は保存済み運用記録を読み、日次runの未完了を報告する。
市場データの取得成功だけで未完了の運用記録を成功扱いに変更しない。

## PAPER通信と日次取得の共有予算

日次市場取得とPAPER準備・公開原本収集は、論理日あたり既存の最大12回を共有する。
`automation/nightly-market-data`のjournalに`paper_requests`を追記し、
市場データの既存消費（過去の課金記録を含む）を差し引く。市場取得が10回なら残り2回、
11回なら残り1回であり、PAPER専用に12回を追加しない。
J-Quants・EDINET・JPX・公開原本を含むPAPER側の各HTTP試行を、通信前にGitへ予約する。
失敗・redirect・retry・通信直前のcrashでも予約を返却しない。
GitHubのcheckpoint保存通信には、既存のGitHub専用予算が別途適用される。

定期21:00、手動再開、同じcutoff、異なるcutoff、再起動は残枠を共有する。
Actions runの論理日を保存し、日付をまたぐretryで枠を再発行しない。
writer concurrencyに加え、checkpoint/mainの移動・保存失敗・実行終了を検出して通信前に停止する。
同日の旧PAPER inboxに共有課金記録がない場合は、既往の消費が不明なため停止する。
台帳削除やstateリセットで解除せず、既存記録の照合が必要である。

workflowの原本収集とPAPER準備は`stock_jp.automation.provider_budget`を入口にする。
既存urllib通信の境界を監視するため、凍結済み研究コードは変更しない。
保存済みquery cacheは追加通信を伴わず再利用できる。各collectorのローカル試行数には通信前停止を含む場合があるため、消費の正本は共有journalである。
新規取得の残枠不足はレビュー未完了・
データ不足として残し、日次完了や投資可能に読み替えない。
異なる通信ライブラリや並列collectorを追加する場合は、この境界を通る回帰検証が必要である。
今回、schedule時刻・権限・保護・承認・予算額は変更しない。
追加枠が必要なら、対象provider・論理日・必要試行数を示して別途利用者の承認を得る。

### 過去費用と日次journalの累積

`provider_debit_bases`へ、当該日の`prior_charges`、却下済みpayloadにも対応する
`legacy_debit`の費用、および開始時のdaily request列のhashを取得前に保存する。
同じ基準を日次取得とPAPER取得で使い、過去費用＋daily request数＋PAPER予約数を数える。
例えば過去2回＋新規daily9回では合計11回で、PAPERの残枠は1回となる。
再起動して既存daily9回を復元しても、過去費用を`max(2, 9)`へ読み替えない。
日次報告の`requests_for_date`はPAPERを含む共有消費、`requests_used`はdaily journalの件数、
`paper_requests_used`はPAPER予約数である。

旧journalに開始時の基準がなく、過去費用と既存daily requestの両方がある場合は、
重複の有無が不明なため取得を止める。推測で相殺したり二重加算したりせず、
原記録の照合を必要とする。既存基準の過去費用が変わった場合も取得を停止する。
台帳・state・予算のリセットで解消しない。

## 遅延したPAPER工程の対象日

PAPERの自動入口は日次checkpointの`scheduled_targets[Actions run ID]`を使う。
例えば10月7日対象の定時runが10月8日04:17 JSTにPAPERへ到達しても、
`operations/runs/2026-10-07`を対象にする。main採用済みの日次取得記録と
checkpointの対象日・状態・要求・task・設定hashが一致しない場合は停止する。
休日は新しい調査runを作成しない。

最初のPAPER工程で実時刻の`paper_cutoff_at_jst`を同じbindingへ保存し、
一次資料収集・query cache・準備・inbox保存が共有する。
既存runまたは同日の検証済みinboxにcutoffがあればそれを保つ。
cutoffは対象日の21時から翌日21時未満の実観測時刻であり、
翌朝に取得した資料を前日21時に知っていたことにはしない。
取得時刻・公表時刻・既存のPIT検証条件は維持する。
最初のcutoffがないまま次の公開夜まで遅延した古いrunは停止し、
過去の観測時刻を作らない。保存済みcutoffがある同一runの再試行は元対象を保持する。

既存のcanonical runは再生成せず、`resume-review`でオフラインレビューへ進む。
共有予算が枯渇し新しいrunもない場合は`BUDGET_EXHAUSTED`を返してscanを開始しない。
保存資料のレビュー、日次finalize、投資可能判定は別の工程であり、
対象日の修正だけで完了・投資可能にしない。旧checkpointの消費を削除・移動せず、
予算12回、98%基準、PAPER/HUMAN_ONLYと承認条件を維持する。

## PAPERの流動性窓adapter

採用済み論理対象を持つPAPER経路だけが、公式収集後に
`operation/liquidity_adapter.py`を呼ぶ。日足照会の末日は対象runの日付なのに、
翌朝のcutoff日付で未完了sessionを要求する不整合を補正する。
mainで検証した取得記録のhash、対象営業日、設定、cutoff以前の取得予約を照合し、
収集済みcalendar・日足のhashと原本の対応を検証する。追加通信はしない。

窓末日だけを採用済み対象営業日に揃え、既存`trading_value_window`を使う。
連続する公式20営業日、明示的な無約定のゼロ扱い、最新日の有効価格、
平均・中央値、価格鮮度、参加率・注文上限は変えない。
欠損・未来足・休日・未採用・原本不一致は停止し、別providerの失敗を消さない。
cutoff時点ですでに次の公式sessionが引けている場合も停止し、
旧対象日の窓を「直近20営業日」として使わない（引け時刻と同値も停止）。

cutoff、収集開始時刻、元query receipts、補正前のprovider結果をmanifestに残す。
cutoff後の観測は明記し、取得時刻を前夜へ戻さず、過去時点利用可能性を認定しない。
このadapterは現在のPAPER運用用で、凍結`jpx.py`・旧研究経路は変更しない。
同じ翌朝入力でも旧研究の窓末日は従来のcutoff日付となるため、
旧研究との結果差は運用adapterの適用有無として区別する。
新しい研究結果や投資可能判定を自動承認するものではない。

## 収集・保存の高速化

日次市場データとPAPER原本のGit保存は、UTF-8テキストを
[Git Trees APIのcontent指定](https://docs.github.com/en/rest/git/trees#create-a-tree)でまとめて送る。
JSONのエスケープ後のリクエストを4 MiB以下に分割し、バイナリと単独で上限を超える
ファイルは従来のblob送信を使う。全batchの保存後にpath・mode・blob hashを照合し、
1つのcommitに確定する。途中失敗ではcheckpointのrefを進めない。
検証済みblobとcommitに対応するtreeの識別子は同じプロセスで再利用する。
branch、実行状態、権限の確認は通信で取得する。

取得前の消費予約、取得後の応答保存、要求間隔と通信予算は維持する。
同時取得数は増やさず、変更のない情報の通信とファイル別の書き込みを減らす。
アーカイブのpackは既存保存量を一度集計し、新規objectの容量を加算する。
既存objectの内容検査と、公開前の世代全体の検証は毎回実行する。

全Python jobは`pyproject.toml`をキーに
[pipのダウンロードキャッシュ](https://github.com/actions/setup-python/blob/main/docs/advanced-usage.md#caching-packages)を共有する。
毎回の依存インストールは続ける。財務履歴artifactはgzip済みの原本を再圧縮せず、
PAPER原本artifactは圧縮レベル1を使う。CPU時間を減らす代わりにartifactが大きくなる
場合がある。仕様は[upload-artifactの圧縮設定](https://github.com/actions/upload-artifact#altering-compressions-level-speed-v-size)を参照。

外部通信を使わない比較は次で再現できる。

```bash
mkdir -p .work/tmp
TMPDIR="$PWD/.work/tmp" python -m tests.benchmark_collection --output .work/collection-benchmark.json
```

合成の1営業日・9回取得・preview公開までを比較した結果、GitHub書き込みは185回から
98回、API呼び出しは449回から360回、固定の通信間隔による待機は2,006.1秒から
1,080.1秒になった。ネットワーク転送、runnerの起動、CIの実行時間を含まない計算値である。
1,000ファイルのpackも変更前後で同じgeneration hashになり、保存形式は変わらない。
実行時間は環境に依存するため、出力JSONの`pack.seconds`で比較する。

100件の合成テキスト原本と2件の運用ファイルをPAPER inboxに保存する比較では、
GitHub書き込みは106回から3回、固定間隔の待機は1,076.5秒から34.6秒になった。
実際の原本がPDFなどのバイナリの場合はblob送信を使うため、削減量は原本の構成に依存する。

## Actions停止中の外部市場データ実行

Actionsの定時実行とowner手動実行は引き続き`market-data-manual.yml`を使い、
実際のActions環境内で`python -m stock_jp.automation.nightly`を起動する。
スケジュール、既存のCI検証・採用経路は変更しない。
外部実行のためにActionsの環境変数やrun IDを捏造しない。

`python -m stock_jp.automation.external_market --root <checkout> --session <name>`
は、ユーザーが外部取得と保存PRを明示承認した場合の市場データ専用入口。
PAPER調査、取引、Actions dispatch、CI承認、mergeは実行しない。
資格情報は既存の`GH_TOKEN`と`JQUANTS_API_KEY`をメモリ内で使用する。
旧`config/automation.json`の期限切れ外部campaignを再有効化しない。

修正を採用した最新mainからのみ実行できる。未マージの修正branchを
実取得に使ってはいけない。既存のfinancial-history経路は独立checkpointを使い、
旧コードは外部writerを認識しないため、ローカル試験成功と一時点のActions停止だけでは
相互排他が成立しない。mainの採用には通常のexact-head CIと独立レビューを必要とし、
Actionsの支払・利用上限による未実行をローカル成功で置き換えない。
旧branchからの取得workflowや別の外部collectorを同時に開始しない。

外部writerは`external-market-v1`とランダムsession UUID、main SHA、論理日、
解決時刻で識別する。Actions actor/run IDは生成しない。
ローカルreceiptは`.work/external-market/<name>.json`に保存し、全session共通の
`flock`で同一checkoutの並行プロセスを拒否する。receiptを別環境へコピーしない。
共有日次checkpointの`external_writer`を非force Git更新とreadbackで確保し、
異なるsession、通常の日次/PAPER writer、financial-historyを停止する。
競合したclaimは非fast-forwardで失敗する。既に走っている旧Actionsも含め、
queued/in-progress/waiting/pending/requested runがあれば開始・通信を拒否する。

通信前にmain/code/config不変、checkpoint所有権、既存の履歴課金basis、PAPER込み
日付別12回上限を検証する。各要求は既存collectorが通信前に共有checkpointへ保存し、
保存済みstateと予約を再読してから通信する。ページ・失敗・不明応答・再起動も消費し、
既存の予約やreceiptを返却・修正しない。GitHub通信は別の既存rate budgetに従う。

初回は最新の経過済み21時JSTを論理日とする。日曜朝なら土曜を対象にcalendarで確認し、
架空の取引日を作らない。同じsessionの再開では元の論理日を維持し、過去日指定で
新しい取得枠を作るオプションは設けない。欠損日のbackfillは既存campaignと別途判断する。

取得後は現在の非圧縮保存形式を検証し、mainを親とするデータ専用PRを作成して停止する。
コードはデータPRに入らない。`DATA_PR_CREATED`と`required_ci=NOT_VERIFIED`は
保存候補の作成だけを意味し、採用や日次運用完了を意味しない。
PR作成後に外部fenceを解除し、writer来歴はcheckpointのGit履歴に残す。
途中停止・不明応答ではfenceを保持し、同じreceiptでのみ再開する。
中断した外部実行をActionsへ切り替える場合は、元のプロセスが停止した同じcheckoutで
次の明示handoffを行う。ローカルlockとUUID所有権を確認してfenceだけを解除し、
不明応答も含む要求・journal・課金basis・PAPER課金はそのまま残す。

```bash
python -m stock_jp.automation.external_market --root <checkout> --session <name> --handoff-to-actions
```

handoff自体はprovider通信もActions dispatchも行わない。
その後のowner Actions手動実行ではreceiptに記録された論理日を`target_date`へ指定する。
Actions中断から外部実行へ切り替える場合も、Actionsが終了していることを確認し、
同じ論理日の共有journalから再開する。日をまたいで未完了runが残る場合は、新しい日を
開始せず、元の日のActions再開または既存外部sessionの再開で解消する。
外部完了後のActions再実行も既存receiptを再利用し、要求枠をリセットしない。
receipt紛失、main移動、未解決PR、認証停止は明示的な運用照合が必要。
公開途中に追跡対象の生成ファイルが残った場合も、保存済みcheckpointと照合し、
取得原本を保持したままmainの追跡ツリーを復元してから再開する。
タイムアウトによる自動takeover、台帳reset、main追随の自動再bindは行わない。
98%基準、PAPER/HUMAN_ONLY、凍結研究と実LIVE承認条件は変更しない。

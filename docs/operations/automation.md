# 自動化

GitHub Actionsの入口は `.github/workflows/`、取得設定は
[`config/sources.json`](../../config/sources.json)、取得・保存処理は
[`automation/nightly.py`](../../src/stock_jp/automation/nightly.py) が正本。

| Workflow | 責務 |
| --- | --- |
| `ci.yml` | `format`、`test`、`guardrail`。静的検証と合成データのテスト |
| `market-data-manual.yml` | 毎日21:00 JSTに市場データを取得・検証・保存。21:20に再試行。取得後に一次資料収集とPAPER調査準備 |
| `operation-health.yml` | 保存済みPAPER runメタデータの監視結果をartifactへ保存 |
| `forward-holdout.yml` | 22:30 JSTに凍結したPAPER研究と採用済みデータを読み取り、不足・実装変更をartifactへ保存 |

依存関係は `pyproject.toml` を参照する。夜間取得は実行commitと採用済み市場データを
checkoutする。CIと運用監視は市場データをcheckoutせず、実データの準備完了を判定しない。
forward holdoutの観測は採用済みデータもcheckoutする。正式入力が不足した開始日以降は
失敗として記録するが、市場データの取得・保存・本人のLIVE承認は行わない。
固定した条件と現時点の不足は[正式検証の記録](../../research/formal-validation-2026-10-05/README.md)を参照する。

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
4. データbranchを指定して `ci.yml` を明示dispatchし、返されたrun IDを追跡する。
5. そのrunが同じPR headで実行され、対応するcheck suiteの `format`、`test`、`guardrail`
   が全て成功した後、そのデータPRの承認待ちCIを起動する。このCIも3項目の成功を確認し、同じheadをsquash mergeする。

`GITHUB_TOKEN` で作成したPRのCIは承認待ちになるため、自動の `pull_request` 起動に
依存しない。`workflow_dispatch` は標準トークンでも起動できる。
ただしmainの必須チェックはPR側のCIに紐づく。独立したdispatchの成功後に、
検証済みデータ差分・同じhead・同じPR・bot起動のCIだけを承認する。
承認APIの拒否、PR側CIの失敗・未完了でも採用せず、保護ルールを変更しない。
詳細は[GitHubのトークン仕様](https://docs.github.com/en/actions/concepts/security/github_token#when-github_token-triggers-workflow-runs)を参照する。
dispatch失敗、run ID不明、対象headの不一致、CI失敗・未完了ではmergeしない。
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
次回は設定ハッシュとページのcutoff・query・原本ハッシュを検証し、query cacheだけを復元する。
復元対象は同夜に再開できるcutoffのscopeだけ。過去分はGitのsnapshotへ残し、
翌夜のcacheや取得・復元量へ累積させない。
運用state、判断、注文、承認は復元しない。同日の21:00以後の検証済みcutoffだけを
再開メタデータとして返し、21:20の再試行でquery pageを再利用する。朝のcutoffは
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

定期21:00/21:20、同じcutoff、異なるcutoff、再起動は残枠を共有する。
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

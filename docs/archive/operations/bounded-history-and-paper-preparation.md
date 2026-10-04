# Light履歴取得とPAPER準備

PR47とsnapshotデータPR48を採用したmainを基点にする。
本コードは別draft PRでreview後に採用するまで実取得に使わない。
実行主体は単一writer。毎日21時JSTの日次取得と並行実行しない。

## 固定した履歴計画

`stock.jp/scripts/jquants_history.py` は1つの固定計画を実行する。
対象は最大280営業日のmaster／日足、営業日確定用calendar、同期間TOPIXだけ。
Light・追加契約なしを確認し、財務その他の全期間取得へ範囲を拡張しない。
開始対象日は取得当日より前、直近3年以内に限定する。
calendarの550暦日lookbackを含めLightの5年履歴内に収める。

- 総API送信最大600、batch最大50、1 batchは設定値以下かつ最大12送信。
- 各ページ・失敗・応答前クラッシュも送信前に永続予約。自動retryなし。
- 429は120秒以上待機、401/403はBLOCKED。journal削除や別IDによる再開始は禁止。
- `jquants/history/state.json`とmarker、範囲応答のpage object、日別partition checkpointを保存。
- 復元後も使用数・固定期間・設定hash・選択営業日・全参照raw hashを再検証。
- 既存の正しい日別partitionは再利用し、過去revisionを残す。calendar/TOPIX rangeは完了後再取得しない。
  対象日に未完了の訂正checkpointがあれば古いlatestを再利用せず、checkpoint再開を優先する。
  完了済み計画に未完了訂正が残った場合はCOMPLETEとして受理せず、reviewへ戻す。
  ただしquery・started_at・全pageが検証済みの公開revisionと完全一致する完了checkpointは、
  削除直前のクラッシュ残骸として先に安全回復し、新訂正とは区別する。
- dailyとworkspace／archive lockを共有。未完了daily journalがあれば開始しない。
- batchを自動繰上げしない。消尽後だけ次の番号を明示。総上限・50枠到達は停止。
- COMPLETEでも全契約datasetの履歴完了、PAPER完了、投資可とはしない。

```sh
python stock.jp/scripts/jquants_history.py --root "$PWD" \
  --as-of 2026-09-30 --sessions 280 --batch 1
```

次のbatchに進む前に成果物をpack／verifyし、データPR経由で保存・採用・復元を確認する。
既存の未保存成果物へ古いmain世代をoverlayしない。環境喪失により未保存予約が不明なら停止。
CLIは清潔な最新mainを要求するため、保存用branchのまま次batchを開始しない。
最終送信枠の予約後にクラッシュしてRUNNINGが残った場合、再開時に予約を戻さずPAUSEDへ
永続正規化する。同じbatchでは通信せず、総上限内で明示した次batchだけ再開できる。
50 batch／600送信到達時はEXHAUSTEDとし、自動採用は許可しない。
正常なbatch予算消尽はBATCH_EXHAUSTEDとして保存可能。PAUSED/BLOCKED/EXHAUSTED/RUNNINGは
保存PRを作れても自動採用gateで拒否し、通常reviewに戻す。
1ページ／partitionなら280営業日で562送信・47 batchが目安。
ページ追加・失敗により600で未完了になる場合は残範囲を報告し、上限を増やさない。

6000銘柄の合成1営業日を実測するとraw＋派生CSV 3,203,211 bytes、
圧縮58,346 bytes、process peak RSS 52,596,736 bytes、所要1.201秒だった。
280倍のraw単純外挿は896,899,080 bytes（約0.9GB）。同一形状の合成値であり、
実レスポンスの銘柄名長・pagination・訂正履歴を含む実市場容量や全期間RSSの保証ではない。
毎送信前にraw 2GiB、staging 2GiB、空きdisk最低6GiBを検査し、
検査値をhistory journalへ永続化する。容量不足は送信せずPAUSED。
HTTP応答は16MiBで読取りを打ち切り、超過応答は保存せずBLOCKED。
CLIのaddress-space上限は1GiBで、停止しても予約済み送信枠は戻さない。
訂正revision・複数復元領域・Git object分は別途必要。codecの750MiB store、8GiB restore上限は維持する。
各保存時に実測容量を確認し、上限を黙って変更しない。

## 個別銘柄・PAPER

流動性は公式cash-equityカレンダーの連続20営業日を固定分母とする。
O/H/L/C/Vo/Vaが明示的に全nullの無約定日は、立会内売買代金0として平均・中央値に含める。
rawのnullと価格は補完しない。行欠落・部分null・不正値・calendar欠落・duplicateは失敗する。
最新営業日の有効約定価格も必須。売買停止、監理整理、開示等の別gateは維持する。
既存の最低流動性、参加率、資金・集中制約は変更しない。

`preparation_status.py --root <checkout>/stock.jp`は保存データだけから
source/PAPER未完了とLIVE残件を診断する。新規取得、PAPER開始・finalize、注文は行わない。
実PAPERは引き続き`nightly_operation.py start`を唯一の入口とし、必須証跡がない場合はWAIT。
EDINET、TDnet、会社IR、JPX売買停止・企業行動、カレンダー等を取得成功だけでCHECKEDにしない。

## 正式復元証跡

独立した最新mainのcloneを準備し、private archiveを空領域へ復元、workspace setupを完了する。
`recovery_drill.py`はGitHubのprivate／mainを読み取り、実SHA・layout revision・raw hash・
台帳・state・隔離bootstrap・PAPER証跡を確認して、新しい証跡ファイルを生成する。
既存ファイルは上書きしない。正式evidenceへの採用は別のreviewで扱う。

```sh
python stock.jp/scripts/recovery_drill.py \
  --source /path/to/source --recovered /path/to/independent-clone \
  --operator ACTUAL_OPERATOR --output /private/staging/new-recovery-drill.json
```

schema 1.2は対象commit、archive世代、private台帳・state、validator code、
選択したPAPER run／attemptと全成果物hash、検査結果をmanifestに固定する。
coverageの最新成功v0.4 PAPERと同じrun／attemptだけをhandoff・run integrity検証に使う。
gateはbooleanやSHA文字列だけを信用せず、保存されたGit commitの入力を隔離復元して再検証する。
coverageの判定基準は保存されたrunのprice_date／source_cutoff。監査実行時刻
`evaluated_at_jst`だけは再評価結果との比較から除き、元の監査時刻はmanifest hashで保護する。
採否・blocker・metrics・全入力hashは引き続き比較し、再検証の時刻差で失効させない。
後日の日次更新は過去の証跡を失効させないが、当時のGit objectは必要。
bootstrapはmanifestに保存したinspection_at_jst（生成時の開始時刻）で再実行する。
再検証は現在のコードで過去入力を評価する。当時のvalidator hashと、
再検証で実行したvalidator hashを区別して記録し、過去コードを実行したとは扱わない。
標準のstock.jp rootからもGit最上位のcommitted private filesを照合する。
旧schemaの証跡は新しい実復元検証を要し、手動の版番号更新では通過しない。
最新の成功PAPERやhandoffがなければ対応checkはfalseで、正式recovery gateは未達のまま。
旧証跡のlayout/SHAだけを書換えて合格扱いにしない。個人リスク確認・本人承認は生成しない。

## 指数と契約の境界

Lightを維持し、Standard／Growth250指数API導入は行わない。
グロース個別銘柄を分析対象から除く方針ではない。
TOPIXはLight履歴で取得できる。Growth250は月報だけでは判定日終値を満たせず、
現行MRSはUNAVAILABLEのまま。日経VIのアクセス制限は迂回せず、ユーザー提供待ち。
CIは公表時刻と取得原本hashを確認できた版だけを使用し、予定時刻を実配信ログと混同しない。
このコードPRに新しい指数実データは含まない。

Growth250を使わないMRSは別設計案に留める。検討項目はM2の削除／構成変更の意味、
score範囲・閾値・entry multiplierの再定義、同一期間での現行比較、感度・ストレス分析、
未来情報を排した未観測期間の事前固定、合格基準と本人承認。
既存閾値の流用、欠損無視、他指数の黙示代用は行わず、mainへ適用する前に影響を報告する。

本人判断が必要な資金配分・損失許容・税務・証券口座・手動注文可否と
holdout期間／受入承認は未入力のまま保持する。日次収集の許可をLIVE承認へ拡張しない。

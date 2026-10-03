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
正常なbatch予算消尽はBATCH_EXHAUSTEDとして保存可能。PAUSED/BLOCKED/EXHAUSTED/RUNNINGは
保存PRを作れても自動採用gateで拒否し、通常reviewに戻す。
1ページ／partitionなら280営業日で562送信・47 batchが目安。
ページ追加・失敗により600で未完了になる場合は残範囲を報告し、上限を増やさない。

280営業日の容量目安はraw＋派生CSV約0.9GB、圧縮0.1〜0.2GB。
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

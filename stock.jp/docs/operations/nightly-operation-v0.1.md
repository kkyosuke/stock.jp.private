# 日本株・夜間ワンショット運用 v0.1

- 状態: `PAPER`運用の正規手順
- アシスタントの確認: 毎晩21:00頃（Asia/Tokyo）
- 通常の運用run: 平日21:00を基準に1回。土日は期限到来レビューと未完了項目のみ
- 注文送信: 常に `HUMAN_ONLY`

## 結論

設定・動作確認済みの定時タスク、または利用者の「今日の夜間運用を実行して」という1回の依頼でアシスタントを起動する。エージェントは前回の状態を引き継ぎ、公式情報源の差分取得、一次資料調査、期限到来レビュー、全保有・全監視銘柄の翌営業日アクション判定、必要な注文票の設定、監査ログ保存まで行う。正常完了後は次回の平日21:00まで追加実行しない。

`PAPER`の注文票は仮想注文であり、証券会社へ送信しない。昇格条件を満たした`LIVE`でも夜間に作るのは`PROPOSED`注文票までである。翌朝8:45〜8:55に利用者が注文前チェックを行い、承認した注文だけを証券会社へ手入力する。

## 1. 1回の処理

1. アシスタントが[収集手順](daily-stock-price-actions-v0.1.md)で全市場株価を取得・検証する。その後`operation_bootstrap.py check`でPAPER readinessを確認する。設定されたprivate-local J-Quants全市場archiveのchecksum、98%以上の全体coverage、保有＋active watchlistの100% coverage、7日以内の鮮度に不足があれば開始しない。
2. `nightly_operation.py start`でreadinessを再確認し、状態検証、同時実行ロック、当日フォルダ作成、EDINET、JPX銘柄一覧、一次サイト確認タスク、期限到来タスクを一括作成する。J-Quantsの取得は1回最大12リクエスト（リトライを含む）で区切り、ページcheckpointから再開する。
3. `research-queue.json`と`work-plan.json`を一次資料で処理し、会社IR、TDnet、公式価格・コーポレートアクション、EDINET、JPX現物株カレンダーを確認する。価格archiveだけで開示や会社IRの一次資料確認を代替しない。完了できない項目は理由と期限を付けて`DEFERRED`にし、同じIDを`handoff.pending_reviews`へ残す。
4. `global-risk.md`へ為替、金利、資源、主要国政策、地政学を「事実→保有KPIへの伝播→判断」に分けて記録する。
5. 保有銘柄と監視銘柄を漏れなく`next-day-actions.csv`へ記録する。対象が0件なら`GLOBAL / NO-ACTION`を記録し、`initial_universe_review`で蓄積済み全市場日足から長期候補を抽出する。候補は買い判断にせず、一次資料を読む対象として絞る。
6. `BUY / ADD / REDUCE / SELL`なら、ルールID、一次資料ID、個別判断ログを揃えた後で`order_ticket.py propose`を使う。注文票とアクション、未照合注文、取引イベント台帳は同時に更新される。
7. `research-results.md`と`report.md`を完成させ、カバレッジを閉じる。
8. `nightly_operation.py finalize`を実行する。必須成果物、`global-risk.md`、全対象、全タスク、アクションと注文票の対応に不足があれば完了は拒否される。
9. 成功なら結果と翌日アクションを利用者へ返し、次回夜まで待機する。重大な情報欠損なら`fail`として成功カットオフを進めない。

## 2. 成果物

すべて`operations/private/runs/YYYY-MM-DD/`に保存する。

| ファイル | 正本となる内容 |
| --- | --- |
| `work-plan.json` | 今夜行う日次・週次・月次・四半期・ルールレビューと完了状態 |
| `research-queue.json` | 公式APIから発生した一次資料確認タスク |
| `research-results.md` | 当夜の調査結果。事実・計算・判断を分けた要約 |
| `global-risk.md` | 世界情勢の事実、保有KPIへの伝播、判断 |
| `next-day-actions.csv` | 全保有・全監視銘柄の翌営業日アクション |
| `orders.csv` | アクションに対応する仮想または人間確認待ちの注文票 |
| `sources.csv` | 公開日時、取得日時、URLを持つ根拠 |
| `coverage.json` | 開始時点の対象と確認済み範囲 |
| `report.md` | 利用者向け結果と例外、人間が行うこと |
| `handoff.json` | 未完了レビュー、未照合注文、次回日時 |

チャット履歴は状態の正本にしない。次回は必ず`state.json`と直前の`handoff.json`から再開する。

## 3. 開始コマンド

~~~bash
.venv/bin/python scripts/nightly_operation.py start \
  --at 2026-08-31T21:05:00+09:00 \
  --cutoff 2026-08-31T21:00:00+09:00
~~~

返された`run_token`は終了まで保持する。`locked`なら別の実行を開始しない。`completed`ならその日は既に完了しているため重複作業をしない。

月次レビューは毎日蓄積済みの全市場日足とJPX一覧を使って候補集合を見直す。全市場を日次取得済みなので、月次に同じ市場データを重ねて取得しない。

## 4. 注文票の設定

注文票は次のすべてを満たす場合だけ作成できる。

- 運用ポリシーが有効で、`PAPER`または全昇格条件を満たす`LIVE`
- JPXの現物株取引日カレンダーを一次資料で確認し、翌取引日が確定
- 重大なデータ欠損がない
- 調査キューが全件`COMPLETED`。`DEFERRED`を1件でも含む場合、その夜の売買アクションは`WAIT`へ変更する
- 対応する翌日アクションにルールIDと一次資料IDがある
- 同じ銘柄に未照合注文がない

~~~bash
.venv/bin/python scripts/order_ticket.py propose \
  --run-id 2026-08-31 --run-token '<startのrun_token>' \
  --action-id 2026-08-31-1234-next \
  --code 1234 --company '会社名' \
  --side BUY --action BUY --rule-ids 'E-1;E-2' \
  --trade-date 2026-09-01 --limit-price 1000 --quantity 100 \
  --position-pct 1.0 --participation-cap-pct 5.0 \
  --valid-until 2026-09-01T15:30:00+09:00 \
  --decision-id operations/private/decisions/2026-08-31-1234.md \
  --at 2026-08-31T21:30:00+09:00
~~~

このコマンドは`orders.csv`、`next-day-actions.csv`、`handoff.json`、`trade-event-ledger.csv`を整合的に更新するが、証券会社へ通信しない。同じ引数で再実行しても同じ注文票を重複作成しない。

## 5. 完了と待機

~~~bash
.venv/bin/python scripts/nightly_operation.py finalize \
  --run-id 2026-08-31 --run-token '<startのrun_token>' \
  --completed-at 2026-08-31T21:35:00+09:00 \
  --source-cutoff 2026-08-31T21:00:00+09:00 \
  --price-date 2026-08-31 \
  --summary '調査完了。翌営業日のアクションを設定済み'
~~~

成功すると`next_run_at_jst`が、完了時刻より後で、かつ完了したrun日より後の最も近い平日21:00へ設定される。前日runを翌日01:00に完了した場合は、その日の21:00を選ぶ。同日runを21:00前に完了しても、その日のrunを重複予約しない。それまでは自動再調査や注文条件の追い上げをしない。例外は、昇格済み`LIVE`注文について翌朝に利用者が行う注文前確認だけである。

## 6. 定時タスク

同じチャットのアシスタントが毎晩21:00頃、`Asia/Tokyo`で確認する計画とする。平日は収集からPAPER完了まで行い、土日は期限到来レビューと未完了項目だけを確認する。取引日や仮想約定を作らず、通常runの次回日時は次の平日21:00を維持する。定時タスクの登録・実行環境の接続確認は別作業であり、この文書の変更だけでは有効にならない。GitHub ActionsとCloudflareは実行主体にしない。依頼文の例:

~~~text
$japan-stock-operator を使って、今日の夜間運用を最後まで実行してください。
~~~

ローカルファイルへアクセスする実行では、対象コンピューターとデスクトップアプリが利用可能である必要がある。実行結果が`FAILED`または`locked`の場合は、完了したとみなさずレポートの人間作業を確認する。

## 7. 公式情報源

- [EDINET APIキー案内](https://disclosure2.edinet-fsa.go.jp/week0020.aspx)
- [TDnet API](https://www.jpx.co.jp/markets/paid-info-listing/tdnet/02.html)
- [JPX 休業日一覧](https://www.jpx.co.jp/corporate/about-jpx/calendar/)
- [JPX その他統計資料](https://www.jpx.co.jp/markets/statistics-equities/misc/)

APIの成功は企業IRとJPX個別確認の代替ではない。売買判断に使う事実は一次資料へ結び付ける。

private repository の同期と復旧、実行漏れ検知、初回診断、20営業日試験は[継続稼働準備 v0.1](operation-readiness-v0.1.md)に従う。

## 非公開入力への移行

[非公開株価入力とActions](daily-stock-price-actions-v0.1.md)が現在の保存先・取得制限の正本。Yahoo一括取得CLIは停止中。J-Quants接続の安全な設定完了までは、指定private archiveが欠損ならpublic archiveへfallbackせずWAITとする。IR・開示の調査だけを別途継続しても、nightly成功とは記録しない。

## 取得上限と中断再開

`jquants.ingestion.max_requests_per_run`をnightlyの全J-Quants経路でも共有する。
省略時は12、許容値は整数1〜12。ページングと通信リトライも消費に含める。
これは1回の起動の上限であり、連続自動ループで上限を実質解除しない。
上限到達時は`nightly_jquants_scan`のblocking gapを残し、成功cutoffを進めない。
全有効datasetが完了するまで部分ページを完全な確認根拠にしない。
有効datasetの認証不足もblockingとし、任意の無効datasetは成功判定から除外する。
解決後の再失敗は前回の解決内容を`resolution_history`に保存してgapを再開する。
J-Quantsの個別providerが失敗中なら、gapだけを解決済みにしてもfinalizeできない。

checkpointは`operations/private/market-data/nightly-source-cache/`にローカル保存する。
固定origin・plan・cutoff・endpoint・全query（ページキーを含む）が一致したページだけを
checksum検証後に再利用する。各ページの実取得日時はmanifestのreceiptへ残す。
dataset側の日時は再構成日時であり、再取得の証拠にはしない。
破損したcheckpoint（JSON null、不正・timezoneなしの取得日時を含む）は自動削除・再取得せず停止する。保存直前の中断では、その1ページの
readだけが再実行され得る。成功済みページを削除しない。

同日も日付をまたいだ場合も、元runのcutoffを維持し、現在時刻と元run IDで再開する:

~~~bash
python scripts/nightly_operation.py start \
  --at 2026-09-01T01:00:00+09:00 \
  --cutoff 2026-08-31T21:00:00+09:00 \
  --resume-run-id 2026-08-31 \
  --run-token '<previous-run-token>'
~~~

この例の日時は実行時刻へ置き換える。期限内leaseは同じtokenを必要とし、期限切れは
新しいtokenを返す。以後は返されたtokenを使う。存在しないrunの過去日作成や、後続runが
正常完了した後の巻き戻しは拒否する。元runのcutoff変更も拒否する。
次の新規runは新しいcutoffで評価し、前cutoffのcacheを新しい確認結果として流用しない。

21:00 Asia/Tokyoが現行の意図であり、新規stateテンプレートも平日21:00である。
古いstateの18:30や9月14日の最終成功履歴を、設定変更だけで成功扱いへ書き換えない。
通常runの次回21:00は本当にfinalizeできた時点で更新する。定時タスクの登録は別途行い、
単一writerで実行する。ローカルcacheの存在だけでは別instanceへの引継ぎを保証しない。
取得データのremote保存は別途許可された経路が確立するまで行わない。

## 公式資料の別経路での調査

shell通信が失敗しても、利用可能な標準Web閲覧で公式本文を読めた資料は手動調査に使える。
ネットワーク設定、認証、TLS、アクセス制限を変更・迂回しない。
JPXの現物休業日ページ、内閣府の景気動向指数ページとその公式資料は、本文を確認した上で
`sources.csv`のURL・公開日時・取得日時・primary_sourceと根拠IDへ記録する。
TDnet/EDINETの入口ページを開けただけでは開示確認済みにしない。対象期間・銘柄に該当する
公式資料の本文、PDF等を確認する。会社IRも同様に実際の資料を根拠とする。
検索snippetだけの確認、未取得資料の推測、APIキーのWeb検索への送信はしない。
手動根拠はresearch queue、coverage、必要なresearch-inputsへ対応付け、実際に確認した
項目だけを完了する。ブラウザでの閲覧成功は全市場archiveやMRSの必要系列の代替ではない。

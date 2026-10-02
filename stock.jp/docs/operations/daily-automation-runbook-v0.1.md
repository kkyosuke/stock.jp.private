# 日本株運用・日次自動実行手順書 v0.1

- 状態: `PAPER`運用用。`LIVE`昇格は[運用ガバナンス](operation-governance-v0.1.md)に従う
- 作成日: 2026年8月31日
- 基準ルール: [運用ガバナンス v0.1](operation-governance-v0.1.md)、[実運用手順書 v0.1](live-operation-playbook-v0.1.md)。現行PAPERはv0.4をベースライン、v0.2とv0.3をシャドー比較とする
- 実行単位: 毎晩21:00頃（Asia/Tokyo）の確認。通常の運用runは平日1回

## 1. 結論

同じチャットのアシスタントが毎晩21:00頃（Asia/Tokyo）に、許諾確認済みの非公開株価入力検証、公式情報源・世界情勢の調査、分析、期限到来レビュー、PAPER処理、翌営業日アクション、privateログ保存を一貫して担当する。GitHub Actionsは保存済み成果物の検証と回帰テストだけを行い、情報収集・分析・PAPER実行・PR作成・mergeを行わない。収集の実コマンドと公開境界は[株価収集とガードレール](daily-stock-price-actions-v0.1.md)、判断フローは[夜間ワンショット運用](nightly-operation-v0.1.md)に従う。

日次タスクが行うのは情報収集、ルール判定、注文案の作成までである。証券会社への注文送信は行わない。`PAPER`では注文票を `PAPER_PROPOSED` とし、実際には入力しない。現行v0.4が引き継ぐ執行条件は翌営業日8:45〜8:55の注文前チェックを要求するため、その時間を確保できない利用者は`LIVE`へ昇格しない。注文時刻を柔軟にする案は凍結済みルールを直接変えず、別版で前向きPAPER検証する。

## 2. 最も楽にするための設計

### 2.1 1つの日次タスクへ集約する

- 毎日: 前回カットオフ後の公式開示と即時撤退条件を差分確認
- 週次: 期限到来時だけ、見落とし、流動性、集中、決算予定を確認
- 月次: 月末値が確定した時だけ、候補抽出、価格・時間条件、正式な`MRS-v0.1`を更新
- 夜間開始時: `assessment-status.json`へ希薄化、SAM/SOM、10倍経路を再計算し、不足入力をwork planへ追加

計算の入力、出力、人の作業分担は[判断計算の自動化](decision-assessment-automation-v0.1.md)を参照する。
- 四半期: 新しい四半期開示が出た銘柄だけ、5営業日以内の再採点をキューへ追加
- 本決算: 新しい本決算が出た銘柄だけ、10営業日以内の全面更新をキューへ追加

毎日すべての銘柄を100点で再採点しない。売買判断が発生した銘柄、またはレビュー期限が来た銘柄だけ個別判断ログを作る。それ以外は日次レポートに `NO-ACTION` の件数と例外だけを残す。

### 2.2 引き継ぎをファイルで固定する

チャット履歴だけを状態管理に使わない。public Git では無視し、親の private Git で追跡する `operations/private/` を正本とする。次回実行は必ず `state.json` と前回の `handoff.json` から再開する。

| ファイル | 役割 | 更新者 |
|---|---|---|
| `state.json` | 最終成功日時、開示カットオフ、未完了レビュー、未処理注文 | 日次タスク |
| `run-history.csv` | 1実行1行の成功・失敗履歴 | 日次タスク |
| `watchlist.csv` | 監視対象と次回の全面レビュー日 | 日次タスク・利用者 |
| `portfolio-register.csv` | 保有状態と次回レビュー日 | 利用者が約定反映、日次タスクがレビュー情報更新 |
| `runs/YYYY-MM-DD/report.md` | その日の例外、判断、必要な人間作業 | 日次タスク |
| `runs/YYYY-MM-DD/orders.csv` | 翌営業日の注文候補と実際の約定 | 日次タスク・利用者 |
| `runs/YYYY-MM-DD/sources.csv` | 公開・取得日時付きの根拠URL | 日次タスク |
| `runs/YYYY-MM-DD/coverage.json` | 開始時の対象と、銘柄・情報源ごとの確認完了 | 日次タスク |
| `runs/YYYY-MM-DD/lease.json` | 同時実行を防ぐ6時間のrun token | 日次タスク |
| `runs/YYYY-MM-DD/provider-health.json` | 公式APIごとの成功・不足・取得件数 | 日次タスク |
| `runs/YYYY-MM-DD/research-queue.json` | 当夜に読む一次資料と手動確認のキュー | 日次タスク |
| `runs/YYYY-MM-DD/work-plan.json` | 当夜の期限到来タスクと完了状態 | 日次タスク |
| `runs/YYYY-MM-DD/research-results.md` | 当夜の調査結果 | 日次タスク |
| `runs/YYYY-MM-DD/global-risk.md` | 世界情勢の事実、KPIへの伝播、判断 | 日次タスク |
| `runs/YYYY-MM-DD/next-day-actions.csv` | 全対象の翌営業日アクション | 日次タスク |
| `runs/YYYY-MM-DD/raw-sources/` | APIの生レスポンス。private限定 | 日次タスク |
| `runs/YYYY-MM-DD/handoff.json` | 実行状態と次回キュー | 日次タスク |
| `runs/YYYY-MM-DD/pretrade-check.md` | 寄り前の取消・承認チェック | 利用者 |
| `decisions/*.md` | 売買判断・期限到来レビューの詳細 | 日次タスク |

銘柄・取引・資金の詳しい正本と移行方法は[状態・台帳仕様 v0.2](operation-state-v0.2.md)に従う。とくに、部分利確済みフラグ、`S-B`連続数、再購入禁止、回収原資、業種上限、コーポレートアクションはチャットから推測しない。

失敗時の厳密な完了条件と同時実行防止は[完了条件・重複防止 v0.1](run-integrity-v0.1.md)に従う。

公式APIと一次資料キューは[公式情報源スキャン v0.1](official-source-scan-v0.1.md)に従う。

正確な数量、価格、資産、税、証券会社注文IDはこの非公開領域だけに置く。認証情報、パスワード、APIキー、口座番号はここにも保存しない。

### 2.3 失敗時に確認済み範囲を偽らない

重要な対象を取得できなかった実行は `failed` とし、`last_disclosure_cutoff_jst` を進めない。作成途中のレポートとデータ不足は残す。再実行は既存の日付フォルダを読み、二重の注文候補を作らず続きから処理する。

## 3. 初回セットアップ

マージ後、実際に定時タスクを動かすローカルプロジェクトで一度だけ実行する。

~~~bash
.venv/bin/python scripts/daily_operation.py init
.venv/bin/python scripts/operation_state.py validate
.venv/bin/python scripts/operation_bootstrap.py check
.venv/bin/python scripts/operation_smoke.py --days 20 --start-date 2026-09-01
~~~

最初に利用条件を確認した非公開データがあり、`operations/private/market-data/daily-prices/latest.json`がその最新CSVを指していることを確認する。利用者が全銘柄を手入力する必要はない。月次レビューでは蓄積済みの全市場日足から候補を絞り、AIが一次資料と長期条件を確認した銘柄だけ`watchlist.csv`へ登録する。現在の保有がある場合だけ`portfolio-register.csv`へ正確に登録する。

`operation_bootstrap.py check`は、最新CSVのchecksum、全市場98%以上、存在するactive対象の100%、7日以内の鮮度を確認する。active対象0件はblockerにせず、その日は`GLOBAL / NO-ACTION`と初回全市場候補レビューだけを行う。`paper_blockers`が1件でもあれば夜間runを開始しない。

その後、下記の定時タスク用プロンプトをチャットで1回手動実行し、対象件数、根拠URL、注文候補、引き継ぎが期待どおりか確認する。最初の数回は必ず結果をレビューする。

## 4. 株価の定時収集とAIの一回指示

アシスタントは[非公開入力手順](daily-stock-price-actions-v0.1.md)に従い、最新価格coverage 98%以上、archive、全回帰test、compile、20日smokeを確認する。失敗時は公開・採用やPAPER開始へ進まず、取得エラー、母集団件数、checksum、過去日変更を調査する。コードの公開とprivate submodule採用は別の明示的な操作として権限範囲を確認し、Actionsへ委任しない。未反映、active対象欠損、古いcheckoutはreadinessで停止する。

Yahoo一括取得は停止中。採用するJ-Quants入力もprovider・プラン範囲・鮮度を検証し、売買判断を確定する前に会社IR、TDnet、JPXの公式価格・コーポレートアクション、現物株カレンダー、EDINETを一次資料で確認する。確認不能な日は`WAIT`または`fail`とする。

同じチャットのアシスタントを実行主体にする。計画は毎晩21:00頃、タイムゾーンは`Asia/Tokyo`。通常の運用runは月〜金の21:00を基準とし、土日は期限到来レビューと未完了項目だけを確認する。新しい取引日や仮想約定を作らず、通常runの次回平日時刻を維持する。この文書やGitHub Actionsの変更だけでは定時タスクは登録されない。実行環境・privateデータへのアクセス・通知先を確認し、別途スケジューラを設定して成功実行を確認するまでは「設定済み」と扱わない。手動の依頼でも同じ手順を使う。

private checkoutを保持できる環境を使い、毎回の新規checkoutでは最新private commitとpublic採用commitを検証する。ローカルプロジェクトを使う場合は端末へのアクセスが必要。状態を失う分離worktreeは使わない。

### 定時タスクへ登録するプロンプト

~~~text
$japan-stock-operator を使って、今日の夜間運用を最後まで実行してください。
~~~

アシスタントはまず株価を収集・検証し、スキルは採用済みYahoo全市場archiveの`COMPLETED`相当の証跡とchecksum、active対象100%、状態・台帳の整合性を確認してから`nightly_operation.py start`へ進み、全調査・世界情勢・全対象アクション・必要な注文票を保存して`finalize`する。成功後は次の夜まで待機する。証券会社への注文送信は行わない。

## 5. 1回の実行フロー

1. アシスタントが収集・検証した採用済みYahoo全市場日足の母集団coverage・active対象100%・鮮度・checksumを`operation_bootstrap.py check`で検証し、blockerがあれば開始しない
2. `operation-policy.json`、`state.json`、保有、取引・資金台帳、再購入禁止、監視、未処理注文、前回引き継ぎを読み、`operation_state.py validate`を通す
3. 今回のJST情報カットオフを宣言する
4. `nightly_operation.py start`を実行し、返された`run_token`を保持する。開始処理もreadinessを再検証し、同日の実行が`locked`なら別実行を開始しない。認証不要のJPX銘柄一覧を毎回保存する。J-Quantsは設定せず、会社IR、TDnet/JPX、EDINETを確認して一次資料の根拠を保存する
5. `global-risk.md`へ為替、金利、資源、主要国政策、地政学を「事実→保有KPIへの伝播→判断」に分けて保存する
6. `S-A`を最優先し、注文候補に新情報があれば `WAIT` または取消候補にする
7. 期限到来キューだけ週次・月次・四半期・本決算レビューする。月次は毎日蓄積済みの全市場日足と最新JPX一覧から候補集合を見直し、同じYahooデータを再取得しない
8. 判断根拠を `sources.csv` と日次レポートへ記録し、`coverage.json`の確認済み対象を更新する
9. 売買判断または期限到来レビューだけ個別判断ログを作る
10. 全対象の翌営業日アクションを`next-day-actions.csv`へ記録し、売買なら`order_ticket.py propose`で、`PAPER`は`PAPER_PROPOSED`、昇格済み`LIVE`は`PROPOSED`の注文票を設定する
11. `handoff.json` の未完了レビュー、未処理注文、データ不足、次回日時を更新する
12. 全必須対象を確認できた場合だけ `nightly_operation.py finalize` で状態を進め、次の夜まで待機する
13. 重要な取得失敗があれば `fail` とし、次回は前回成功時のカットオフから再確認する

土日・祝日明けも前回成功時からの差分を見るため、休日中の開示を取りこぼさない。平日の非取引日や価格未更新日は無理に売買判断を作らず、開示確認だけを行って価格基準日を明記する。

## 6. 実行コマンド

日次タスクは、現在日時と情報カットオフをUTCオフセット付きで一度だけ渡す。公式情報源スキャンもこの開始コマンドに含まれる。

~~~bash
.venv/bin/python scripts/nightly_operation.py start \
  --at 2026-08-31T21:05:00+09:00 \
  --cutoff 2026-08-31T21:00:00+09:00
~~~

成功時:

~~~bash
.venv/bin/python scripts/nightly_operation.py finalize \
  --run-id 2026-08-31 \
  --run-token '<prepareが返したrun_token>' \
  --completed-at 2026-08-31T21:35:00+09:00 \
  --source-cutoff 2026-08-31T21:00:00+09:00 \
  --price-date 2026-08-31 \
  --summary "必須対象の確認完了。翌営業日の注文候補1件"
~~~

重要な取得失敗時:

~~~bash
.venv/bin/python scripts/nightly_operation.py fail \
  --run-id 2026-08-31 \
  --run-token '<prepareが返したrun_token>' \
  --completed-at 2026-08-31T21:35:00+09:00 \
  --summary "TDnet取得不能。カットオフを更新せず次回再確認"
~~~

現在の最終成功状態は次で確認する。

~~~bash
.venv/bin/python scripts/nightly_operation.py status
~~~

## 7. 注文日の最小作業

注文候補が0件なら、利用者の作業は日次レポートの確認だけである。`PAPER`では候補があっても証券会社へ入力しない。将来`LIVE`へ昇格するには、翌営業日8:45〜8:55に次を実行できることが現行v0.4の条件である。

1. `pretrade-check.md` を開く
2. 前回カットオフ後の重要開示、売買停止、分割、気配、障害を確認する
3. 各候補を `SUBMIT / CANCEL / WAIT` のどれかにする
4. `SUBMIT` だけ証券会社へ当日限りの指値で手入力する
5. 約定後に `orders.csv` の約定数量・価格・費用と `portfolio-register.csv` を更新する

成行化、午後の指値引き上げ、未承認注文の追加はしない。この時間を確保できない日、またはピンポイント注文を常態的にできない場合は「何もしない」を安全側の既定値とし、現行v0.4のLIVEは`NO-GO`とする。

## 8. 定期レビュー

最初の4週間は、週末に5分だけ `run-history.csv` を確認する。

- 実行漏れ、失敗、データ不足が続いていないか
- 同じ開示を重複処理していないか
- 注文候補と個別判断ログのルールIDが一致するか
- `PAPER_PROPOSED` が証券会社へ入力されていないか、`PROPOSED` が人間の承認なしに実行済み扱いになっていないか
- ログ量が多すぎる場合、`NO-ACTION`銘柄の個別ログを作っていないか

運用ルール自体の閾値は日次運用の都合で変更しない。手順の不具合と投資仮説の成績を分けてレビューする。

private remote との同期、LIVE 前または構成変更後の clean-clone 確認、実行漏れ検知、CIについては[継続稼働準備 v0.1](operation-readiness-v0.1.md)に従う。

## 非公開入力への移行

[非公開株価入力とActions](daily-stock-price-actions-v0.1.md)が現在の保存先・取得制限の正本。Yahoo一括取得CLIは停止中。J-Quants接続の安全な設定完了までは、指定private archiveが欠損ならpublic archiveへfallbackせずWAITとする。IR・開示の調査だけを別途継続しても、nightly成功とは記録しない。

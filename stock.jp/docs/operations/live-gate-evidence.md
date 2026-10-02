# LIVE gate 証跡の機械判定

実資金への昇格条件は、文章上のチェックだけでなく `scripts/live_gate_evidence.py` で検証する。検証に失敗した条件や証跡が存在しない条件は、必ず未達として扱う。

少額限定の実約定は通常LIVEと混ぜず、`scripts/limited_live.py`と
[専用仕様](limited-live-v0.1.md)で判定する。回顧的diagnosticをforwardとして記録せず、
通常LIVEの未完了gateもそのまま維持する。

## point-in-time 全母集団

`point_in_time_full_universe_validation` は、次のコマンドが成功した場合だけ証跡を作成できる。

~~~bash
stock.jp/.venv/bin/python stock.jp/scripts/live_gate_evidence.py \
  --root stock.jp point-in-time \
  --write-evidence stock.jp/operations/private/evidence/point-in-time.json
~~~

既定入力はprivateの`operations/private/historical-replay/2025-2026/output/point-in-time-validation.json`である。入力manifestは以下をすべて満たす必要がある。

- status が `COMPLETED` で、生成日時に UTC offset がある
- 当時点の security master を使い、上場廃止、合併、corporate action を含む
- 必須母集団数と評価済み数が一致する
- hard-gate 入力欠損と look-ahead 違反がともに0件
- 決算・重大開示の必須review数と完了review数が一致する
- `source_snapshot`、`universe_validation`、`quality_report`の3成果物が存在し、SHA-256が一致する

manifest や成果物を後から変更すると検証は失敗する。現在は必要な公式データが揃っていないため、証跡を作らず gate を `false` のまま維持する。

証跡の書き込み先は private の `operations/private/evidence/` 配下だけに制限される。公開 Actions では検証ロジックと fixture の回帰テストだけを行い、実口座・判断情報を artifact にしない。

## 2025〜2026年の履歴再生受入

`historical_replay_2025_2026_accepted`は、固定期間`2025-01-01`〜`2026-08-31`の回顧的stress testと、利用者のprivate reviewの両方を検証する。この期間はv0.4発効前を含むため、正式holdoutとは扱わない。

~~~bash
stock.jp/.venv/bin/python stock.jp/scripts/live_gate_evidence.py \
  --root stock.jp historical-replay \
  --write-evidence stock.jp/operations/private/evidence/historical-replay.json
~~~

再生器と入力契約は[point-in-time履歴再生](historical-replay-v0.1.md)を正本とする。privateの`operations/private/historical-replay/2025-2026/output/replay-result-2025-2026.json`は、`RETROSPECTIVE_STRESS_TEST`、`holdout_claimed: false`、point-in-time manifestのハッシュ、欠損・look-ahead違反0件、取引数、return、maximum drawdown、benchmarkと、取引・日次・月次・metrics成果物のハッシュを持つ。private側の`historical-replay-review.json`には、現在の再生結果のハッシュ、`ACCEPT`、承認者・承認日時、drawdown・集中損失・データ制約を確認した事実と、本人が受け入れたv0.4のreturn、最大DD、単一銘柄・業種損失寄与、取引数を記録する。

再生結果を変更すると過去の review は無効になる。成績の数値だけでは自動受入せず、利用者の review がない状態では gate を `false` のままにする。

## 任意diagnostic: v0.4 PAPER期間

`paper-duration` は private の `run-history.csv` と各成功 reportから365日分の運用履歴を診断できる。

~~~bash
stock.jp/.venv/bin/python stock.jp/scripts/live_gate_evidence.py \
  --root stock.jp paper-duration \
  --write-evidence stock.jp/operations/private/evidence/paper-12-months.json
~~~

この結果は運用品質の任意diagnosticであり、`operation-policy.json`のLIVE必須gate、最終承認bundle、v0.4昇格の入力にはしない。365日に満たなくてもLIVE昇格を阻害しない。

## 任意diagnostic: 実データ20営業日のshadow run

`shadow-run` は公開株価archiveの最新20営業日とprivateの実runを1対1で照合できる。

~~~bash
stock.jp/.venv/bin/python stock.jp/scripts/live_gate_evidence.py \
  --root stock.jp shadow-run \
  --write-evidence stock.jp/operations/private/evidence/shadow-20-days.json
~~~

この結果も任意diagnosticであり、LIVE必須gateと最終承認bundleには含めない。20営業日に満たなくてもLIVE昇格を阻害しない。

## 公式情報源 coverage

`official_source_coverage` は、最新の完了済みv0.4 PAPER runについて確認する。

~~~bash
stock.jp/.venv/bin/python stock.jp/scripts/live_gate_evidence.py \
  --root stock.jp official-coverage \
  --write-evidence stock.jp/operations/private/evidence/official-coverage.json
~~~

対象runは通常のrun integrity、alert 0件、data gap 0件を満たす必要がある。EDINETは`LIVE_NETWORK`のrequestが1回以上成功していなければならず、fixture実行は算入しない。TDnet、EDINET、JPXと、対象銘柄がある日の会社IRが`CHECKED`で、対応する一次資料行が存在することを要求する。判断に使用した公式カテゴリの行が二次資料なら失敗する。対象runより古いsource watermarkも拒否する。

公開株価 archive は再現用の二次データであり、この判定だけで注文価格の公式確認を代替しない。対象銘柄の価格、corporate action、会社IRは各 run の一次資料証跡へ残す。

## private repository の clean-clone 確認

`private_repository_recovery` は、通常の private remote から現行 repository layout を clean clone できた記録を検証する。公開テンプレート `operations/templates/live-gate-evidence/recovery-drill-template.json` を private の `operations/private/evidence/recovery-drill.json` へコピーし、実施結果を記入する。

~~~bash
stock.jp/.venv/bin/python stock.jp/scripts/live_gate_evidence.py \
  --root stock.jp repository-recovery \
  --write-evidence stock.jp/operations/private/evidence/repository-recovery.json
~~~

clean clone、submodule、workspace setup、state と bootstrap、最新成功 run と handoff、全台帳、未照合注文を照合する。復元した private commit と public submodule commit は元の40桁SHAと一致しなければならない。別媒体の mirror は要求しない。

証跡は日数では失効しない。setup、コード配置、状態 schema、台帳構成に影響する変更では、公開validatorの `REPOSITORY_LAYOUT_REVISION` を上げる。既存証跡はrevision不一致で失効するため、変更後にclean cloneを再確認する。稼働中checkoutを確認先にせず、一時ディレクトリを使う。repository URL、credential、口座情報は証跡へ記載しない。

## 利用者のリスク・税・証券会社確認

`personal_risk_and_broker_check` は利用者本人しか確定できない。`operations/templates/live-gate-evidence/personal-risk-and-broker-template.json` を private evidence へコピーし、公式の証券会社規則と本人の状況を確認して記入する。

~~~bash
stock.jp/.venv/bin/python stock.jp/scripts/live_gate_evidence.py \
  --root stock.jp personal-risk \
  --write-evidence stock.jp/operations/private/evidence/personal-risk.json
~~~

生活・納税・5年以内の予定・借入資金を除外し、全体損失停止、税区分、手数料、売買単位、指値、注文期限、8:45〜8:55の本人確認能力を確認する。個人上限は v0.4 の1銘柄10%、業種20%、初回5%、追加2.5%、参加率10%を緩和できない。発注は `HUMAN_ONLY` で broker API の自動送信を使わない。

金融庁の注意喚起に沿い、正しい証券会社URLの bookmark、MFAまたはpasskey、login・取引通知、phishing・口座lock時の手順も確認する。電話番号やcredentialはJSONに書かず、保管場所だけを記録する。この確認は90日で失効し、証券会社仕様や本人事情が変われば直ちに再実施する。

- [金融庁: フィッシングによる証券口座への不正アクセス等にご注意ください](https://www.fsa.go.jp/ordinary/chuui/chuui_phishing.html)
- [JPX: 内国株の売買制度](https://www.jpx.co.jp/equities/trading/domestic/01.html)

## v0.4 holdout 昇格

`v04_holdout_promotion`は通常の必須gateとは別に必要である。2025〜2026回顧的replayでは代替できない。まず`v04-holdout-plan-template.json`をprivate evidenceへコピーし、v0.4凍結後かつまだ観測していない期間、受入基準、入力・約定規則、再調整禁止を本人が結果を見る前に`FROZEN`へする。その期間のpoint-in-time全母集団でv0.2とv0.4を比較し、完了後に`v04-holdout-review-template.json`で本人が判断する。PAPER期間証跡は要求しない。
結果生成日時、plan凍結日時、review承認日時、参照する回顧的replay証跡のいずれかが将来日時なら停止する。

~~~bash
stock.jp/.venv/bin/python stock.jp/scripts/live_gate_evidence.py \
  --root stock.jp v04-promotion \
  --write-evidence stock.jp/operations/private/evidence/v04-promotion.json
~~~

planは`declared_by`、planとruleの凍結日時、固定期間、最低月次評価数・最低取引数、maximum drawdown・1銘柄・業種損失のfloor、5つの事前acknowledgementを必須とする。期間開始後の変更は同じholdoutを無効にし、新しい未観測期間で再開始する。

再生結果はplanのpathとSHA-256、事前宣言、再調整0回、固定基準の観測値と合否、v0.2/v0.4双方のreturn、maximum drawdown、1銘柄・業種の最大損失寄与を含める。本人はv0.4の資金配分による損失増幅、戦略待機資金が安全資産でないこと、時系列と固定基準を確認する。

reviewは再生結果、履歴再生受入証跡、凍結planの3つのSHA-256へ拘束する。いずれかを更新すると再承認が必要になる。PAPER期間証跡は任意diagnosticであり、この昇格の入力にはしない。判定は成績が正なら自動昇格するものではなく、本人が`PROMOTE_V0_4_TO_LIVE`を明示した場合だけ合格する。

## 最終 LIVE 昇格

各コマンドの合格結果を上記の既定 evidence path へ `--write-evidence` で保存する。最後に `live-approval-template.json` を `operations/private/evidence/live-approval.json` へコピーし、private/public commit、現在のPAPER policy、6証跡のSHA-256、本人の `LIVE` 判断を記入する。

まず変更を加えない判定を実行する。

~~~bash
stock.jp/.venv/bin/python stock.jp/scripts/live_gate_evidence.py \
  --root stock.jp promote-live \
  --write-evidence stock.jp/operations/private/evidence/live-readiness.json
~~~

このコマンドは8条件を元データから再計算し、保存証跡の `gate / eligible / blockers / inputs` と一致すること、最終承認が全証跡と現在のPAPER policyのSHA-256に一致することを確認する。`eligible: true` を本人が確認した後だけ、同じ入力のまま明示的に適用する。

~~~bash
stock.jp/.venv/bin/python stock.jp/scripts/live_gate_evidence.py \
  --root stock.jp promote-live --apply
stock.jp/.venv/bin/python stock.jp/scripts/operation_policy.py status
stock.jp/.venv/bin/python stock.jp/scripts/operation_bootstrap.py check
~~~

`--apply` は合格時だけ `operation-policy.json` を原子的に `LIVE` へ変更し、7 gate、v0.4 promotion、証跡path、承認者・日時を同時に設定する。不合格ならpolicyを変更しない。以後のbootstrapも最終承認のハッシュと各型付き証跡を検証し、ファイルを空で作るだけの昇格を拒否する。

LIVE後も注文は自動送信されない。各 `PROPOSED` ticket は8:45〜8:55の pretrade check 後に本人が証券会社へ手入力し、blocking gap、復旧不能、本人状況や証券会社仕様の変更があれば `PAUSED` へ戻す。

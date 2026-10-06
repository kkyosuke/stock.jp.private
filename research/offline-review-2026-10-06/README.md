# 新環境でのオフライン調査と次の工程

2026-10-06。市場provider通信・Actions起動・マージ・実取引は行わない。
旧環境を再稼働せず、新環境の最新GitHubと保存済み資料を確認した。

## 確認した状態

- main: `c80f9daad89c719370d76536f67163e5ff31cc56`。
- PR72: `4fce887b6159329e8c0bad5ba9e208232abd4c61`、非draft・未マージ。
  PR41（pypdf更新）も未マージ。開始時の実行中・待機中Actionsは0件。
- mainにAGENTS.mdは存在しない。`.agents/skills/japan-stock-operator/` と現行運用手順を確認。
- PAPER run `37394147163` は失敗終了。取得・保管成功とレビュー完了を混同しない。
- source inbox `ecd2ae91e5ca004aa5bdfb698d1c57a2470c6556` のmanifestを読み、
  315ファイル・78,416,862 bytesをGit blob、格納path、サイズ、SHA-256でオフライン照合。
  全件一致。`review_completed=false`。原本内容のレビュー完了を証明する検査ではない。
- 10月5日分の取得・独立復元済みデータは再取得しない。9/12消費をリセットしない。
- 本変更はmainから独立し、PR72の変更ファイルとは重ならない。

## 指数を使わない方針との不整合

利用者の「指数は使わないようにルールを変更してください」という方針は、現行実装へ
反映されていない。v0.4第3節はMRS-v0.1の倍率を購入枠へ適用し、UNAVAILABLEでは
新規・追加購入を禁止する。`operation/assessment.py` と `operation/orders.py` は
正式MRSを要求し、`strategy/regime.py` はTOPIX、Growth250、breadth、VI、CIを扱う。
`research/forward_holdout.py` の凍結契約もGrowth250とMRSを明示的に要求する。
したがってGrowth250の欠損を埋めるだけでは、方針との不整合は解消しない。

指数を使わない変更は単なる欠損免除ではない。MRSの倍率による抑制を外すと、旧STRESSや
UNAVAILABLE期間にも購入候補が生じ得て、投資率・集中損失・取引数が変わる。
NORMALを捏造する、Growth250だけを黙って削る、旧v0.4の名前で意味を変える方法は採らない。

次の実装案は新ルール版（仮称v0.5）としてレビューする。

1. 指数とMRSを売買・購入枠の必須入力から外し、MRS状態は `NOT_APPLICABLE` と明示する。
   会社の採点・逆算・開示、個別株価格、流動性、資金・銘柄・業種上限は維持する。
   98%のデータ品質条件も維持する。breadthを別の購入条件へ流用しない。
2. 変更箇所は新ルール文書、policyの版対応、assessment、actions、orders、sizing、
   日次テンプレート・skill、正式replayの入力契約・benchmark役割・比較基準。
   旧研究再現のため既存v0.4の指数処理は版別に保持する。
3. 旧 `operations/evidence/v04-holdout-plan.json` とその23凍結ファイル、入力、結果を保全する。
   今回の変更後も23ファイルのSHA-256は一致。旧計画の2026-10-06〜2027-04-01を
   新版の事前固定期間として転用しない。再固定していない。
4. 新計画には別study ID、将来の未観測開始日、期間、必要観測数・取引数、費用・約定モデル、
   損失floor、比較基準を明記してレビューする。旧研究の結果や承認は引き継がない。
   指数を比較指標にも使わない前提で案を作り、その評価契約を新計画で明示する。

このPRは影響調査とレビュー再開の修正であり、指数不使用の運用版への切替自体は未実装。
旧ルールを再固定せず新計画を具体化する作業が残る。PAPER/HUMAN_ONLYと承認ゲートは維持する。

## 保存から日次完了までの接続

PR72の `paper_preparation.py` は `start_nightly_run` の後に `fail_run` を呼び、
`REVIEW_REQUIRED` を返す。これは未レビューを成功扱いしないための意図的な停止である。
`paper_checkpoint.py restore` は検証済みquery cacheだけを戻し、state・台帳・レビューは戻さない。
そのためsource inboxが保存済みでも、canonical runが完了したことにはならない。

通常の `run start` による再開は `scan_sources`、`run_assessments`、成果物生成へ戻る。
今回 `run resume-review` を追加し、保存済みrunのleaseを取得してレビューへ戻る経路を設けた。
通信も再計算も行わず、cutoff・原本・未レビュー課題を保持する。日付を越えた再開も可能。
別writerはlocked、欠けた成果物や異なるpolicyは拒否する。再開時の投資判定はNOT_EVALUATED。
`finalize_nightly_run` はhandoffを書く前にもleaseを確認するよう修正した。

残る実作業は次のとおり。

1. inbox原本をcanonical stateから隔離して読む。hash一致を内容の確認済みに変換しない。
2. TDnet・EDINET・会社IR・JPXの対象と時刻を照合し、必須開示の網羅性を確定する。
   保存済み一覧だけで「開示なし」を証明できない期間は未解消として残す。
3. 必要な実レビューの根拠、公表・確認時刻、rule IDをsources・coverage・queue・work-plan・
   research-results・actions・handoffに記録する。入力不足はWAIT、繰越はpending_reviewsへ残す。
4. canonicalへの取り込みには現行stateとの突合が必要。inboxのstateや台帳を丸ごと復元しない。
   特に朝のpreview cutoffを今夜のcutoffとして流用しない。
5. 同じrunのtokenと元のcutoffでfinalizeを通す。必須確認が未完了なら停止する。
   正しく確認済みのWAIT・注文0件の日次完了は可能だが、投資可能やholdout完了には数えない。

## 実行と予算

- 今回の市場provider要求数0、Actions dispatch 0、売買0。GitHubは読取・独立draft PR保存のみ。
- 保存済み315ファイルの確認とレビュー再開はprovider予算0で実行できる。
- 10月5日のnightlyは9/12消費済みで追加取得不要。残3回を再取得の理由にしない。
- PR72のPAPERスキャンは1 pass最大12要求、最大5 passで最大60要求。
  これは日次市場取得の12回枠とは別経路で、再開前に保存済み消費・cacheを照合する必要がある。
  保存済み朝の結果は49照会が完了している。今夜のcutoffには新しい対象があり得るため、
  cache利用後の必要要求数をdry inventoryで積算してから、実行を別途承認対象として提示する。
- 新ルール検証のCI・PAPER workflow実行、正式取得の追加予算は今回は未承認・未実行。
  有料指数契約の追加は指数不使用の移行案には不要。認証・アクセス・保護設定は変更しない。
- draft PRには `[skip ci]` commitを使い、今回禁止されたActionsの自動起動も抑止する。
  ローカル検証はActions成功の代わりとは記載せず、マージ前には同じheadのCIが別途必要。

# 定時実行と正式入力の整備

2026年10月6日、mainのPR71マージ後に実データで確認した記録。
10月7日にmain `8424e11b69000cd9b81c38fd9bc1f0c32d4c2738`を統合した。
PR77・78の公式指数取得と厳格なデータPR CI、PR79の10月6日データ、
PR80のMRS原本再検証、PR81のStandard cache検証を引き継ぐ。
以下の10月6日の件数・世代・試行結果は当時の実測である。
運用はPAPER/HUMAN_ONLY。正式検証とLIVE昇格は未完了である。

## 市場データ

[定時run](https://github.com/kkyosuke/stock.jp.private/actions/runs/37365841728)は
hosted runner未割り当て、[mainでの再実行](https://github.com/kkyosuke/stock.jp.private/actions/runs/37375360101)は
`run configuration changed`でJ-Quants通信前に停止した。
公開前に作られた要求0・task0・過去消費0のjournalだけをownerの手動実行で接続し直し、
旧設定と新設定の原本を保存する。消費済み記録や別予算は接続し直さない。
21:00 JSTに加え21:20の再試行を追加する。runner割り当て自体はGitHubに依存する。

[修正後のpreview](https://github.com/kkyosuke/stock.jp.private/actions/runs/37378244594)は成功した。
10月5日分を9/12要求・9taskで収集し、42ファイルのデータ差分だけを作成した。
保存世代は`97318547b32f134618a0230140630af3260fd608c1c214ad8b5581d28541c712`。
保存storeは791506147 bytes（約0.737 GiB）、追加は4309988 bytesで2 GiB上限内。
同じheadの[dispatch CI](https://github.com/kkyosuke/stock.jp.private/actions/runs/37382523185)は3項目とも成功した。

[本番での再利用](https://github.com/kkyosuke/stock.jp.private/actions/runs/37384010782)は
追加J-Quants通信なしで[データPR74](https://github.com/kkyosuke/stock.jp.private/pull/74)を作成した。
独立したdispatch CIは成功したが、PR側の承認待ちCIにmainの必須`format`が紐づかず、
merge APIが405で停止した。保存世代・消費記録は維持されている。
[修正後の同一経路](https://github.com/kkyosuke/stock.jp.private/actions/runs/37388208152)は成功した。
同じheadのdispatch CIと、GITHUB_TOKENで起動したPR側CIが3項目とも成功し、
[結果原本](nightly-preview.json)に両run IDを保存した。API消費は9/12のままである。
その後PR74を同じ検証済みheadでsquash採用し、mainは`c80f9daad89c719370d76536f67163e5ff31cc56`。
保護ルールとコードPRの人によるレビューは維持した。財務観測は95000行、株数80957行、
決算予定78727行などを保存済みだが、取得時点の履歴を過去の原本vintageへ換算しない。

## 一次資料とforward入力

企業IR feed、4件のPDF、VI CSV、内閣府公表一覧の計7件を実際に収集した。
公表日時が日付のみの資料は23:59:59 JST以後に利用可能として扱う。
原本をSHA-256名で保存し、内容変更を黙って置換しない。
VI・景気統計のURLは`config/sources.json`の正本から読み、取得記録にその設定ハッシュも残す。

収集結果と採用済みJ-Quants世代は[forwardの収集記録](../historical-replay/v04-forward/collection/)に保存した。
TLS設定・feed件数型の初回失敗も記録として残し、成功記録へ上書きしない。
[入力manifest](../historical-replay/v04-forward/input-manifest.json)は凍結済み計画へ接続したDRAFT。
9役割の経路とハッシュ、公表時刻のある4件のIR資料を登録した。
利用権と全母集団網羅性は未承認である。

初回の収集記録の価格は10月2日までで、開始前のwarmup資料。
[開始日の観測](holdout-observation.json)は`OBSERVING`、正式入力未完了、評価0回・取引0件。
日次snapshotを正式な月末評価や取引へ数えない。開始日の試行設定はLightだった。
その後、別PRでStandardの[公式日次指数](../index-introduction-2026-10-06/README.md)を導入し、
mainの設定はStandardとなった。後日取得した原本を過去cutoffのPIT証拠にせず、
[MRSの原本検証](../mrs-publication-2026-10-07/README.md)と保留条件を引き継ぐ。

## PAPERの実ネットワーク試行

[GitHubでの試行](https://github.com/kkyosuke/stock.jp.private/actions/runs/37378893292)は
既存Secretsで`operation run start`と同じ入口を実行した。
[provider health](paper-attempt/runs/2026-10-06/provider-health.json)により
EDINET 23日分・4147件、JPX master 3702件、J-Quants master 4442件、カレンダー82件を確認した。
翌営業日は10月7日。財務9ページ・160件でJ-Quants 12要求に達し、決算予定は未取得。
朝の試行では当日未公表の日足を満たせず、MRSはUNAVAILABLE、4052はINCOMPLETE。
WAIT・注文0件、`REVIEW_REQUIRED`で終了し、成功した開示cutoffを進めなかった。

朝のrun・state・原本・履歴は[試行資料](paper-attempt/)へ保存する。
10月6日のcanonical runへ朝のcutoffを採用せず、正式runは21:00以後の公開窓で開始する。
準備処理は同じcutoffと検証済みページを使って1回だけscannerを実行する。
planと取得予算は`config/sources.json`が正本で、1回最大12要求。
予算停止を理由にscannerを作り直さず、未完了の資料と実消費・cache hitを保存する。
各要求1.05秒以上の間隔を保つ。
EDINETも直列1.05秒以上の間隔とする。

Actionsのartifactは400日指定でも実際には90日に制限された。
原本・未完了run・forward記録・query pageは`automation/paper-source-inbox`へ追記snapshotとして
保存し、期限なしのprivate Gitを長期保管先とする。次回はquery cacheだけを復元し、
運用state・判断・承認は復元しない。同日の公開後cutoffは再開メタデータとして使い、
朝のcutoffはownerの明示previewだけで再利用する。artifactは受け取り口として90日保持する。
同じ設定・plan・同夜に再利用できるcutoffのscopeだけを復元する。
Standard設定でLightのscopeを選ばない。過去のscopeはGitのsnapshotに残し、
翌夜のcacheへ累積させない。
保存はレビュー完了や正式入力への採用を意味せず、本人のLIVE承認も生成しない。

[旧準備処理でのページ再開](https://github.com/kkyosuke/stock.jp.private/actions/runs/37394147163)では
36照会を再利用し、新規12+1要求で計49照会を完了した。財務23日分314件、
決算予定23日分1253件を取得した。58 cache membersを復元し、79 membersを検証した。
[Git保存記録](https://github.com/kkyosuke/stock.jp.private/blob/ecd2ae91e5ca004aa5bdfb698d1c57a2470c6556/operations/acquisition/paper-source-inbox.json)の
315ファイルはartifactとSHA-256・blobが一致し、前回の280ファイルも保持された。
収集・復元・保存・凍結確認は成功し、レビュー未完了だけがexit 1として残った。

PR76のオフライン再開と合わせ、canonical runが既にある場合は自動準備で
再取得・再採点・成果物生成をせず、保存済みレビューを保持する。
`REVIEW_REQUIRED`が返した`resume-review`引数と[runbook](../../docs/operations/runbook.md)で
レビューへ進む。leaseも自動で取得・解放しない。完了済みrunは変更しない。

[最新の監視](https://github.com/kkyosuke/stock.jp.private/actions/runs/37455607042)は
保存済み日次runの最終成功が9月14日のため`MISSED`。原本保存を日次完了へ換算せず、
内容と網羅性のレビューが揃ったrunだけをfinalizeする。
指数不使用への移行案は[mainの調査記録](../offline-review-2026-10-06/README.md)を参照し、
現行v0.4の凍結した再生・forward契約とは分けて管理する。

## 残る条件

- 現行v0.4再生に必要な指数の利用権、2022年VI・訂正履歴の確認。
- 全母集団の採点、株式数、資金余力、企業行動、全重要開示の確認。
- 9役割の正式再生と財務・開示を含む出口検証。
- 2027年4月1日までのforward観測と既存の回数・損失基準。
- 本人による証券口座・リスクの確認と最終LIVE承認。

凍結済み23ファイルと計画は変更しない。復旧試験は要求しない。
実行リンクと最新CI結果はこの変更のPRへ記録する。

# 定時実行と正式入力の整備

2026年10月6日、mainのPR71マージ後に実データで確認した記録。
運用はPAPER/HUMAN_ONLY。正式検証とLIVE昇格は未完了である。

## 定時取得

[直近の定時run](https://github.com/kkyosuke/stock.jp.private/actions/runs/37365841728)は
GitHubのhosted runner未割り当てで停止していた。
[手動再実行](https://github.com/kkyosuke/stock.jp.private/actions/runs/37375360101)は処理を開始できたが、
`run configuration changed`で停止した。J-Quants通信前の停止である。

checkpointの10月5日記録は公開前の`WAIT_PUBLICATION`、要求0件、task0件。
復元journal、checkpoint、過去消費を照合できる未使用記録だけを、ownerの手動再実行で
現行設定へ接続する修正を加えた。旧状態と新設定ハッシュを原本と同じ保存世代へ残す。
消費済み記録、別予算、定時実行の設定不一致は停止を維持する。
21:20の再試行を追加したが、GitHubのrunner割り当てを保証するものではない。

## 一次資料とforward入力

企業IR feed、4件のPDF、VI CSV、内閣府公表一覧の計7件を実際に収集した。
公表日時が日付のみの資料は23:59:59 JST以後に利用可能として扱う。
原本をSHA-256名で保存し、同じ文書の内容変更は黙って置換しない。

収集結果と採用済みJ-Quants世代は
[forwardの収集記録](../historical-replay/v04-forward/collection/)に保存した。
初回のTLS設定・feed件数型の不一致も失敗した記録として残し、成功記録へ上書きしない。
[入力manifest](../historical-replay/v04-forward/input-manifest.json)は、凍結済み計画の
期間・ハッシュへ接続したDRAFT。9役割の経路とハッシュを用意し、公表時刻のある
4件のIR資料をsource registerに追加した。利用権と全母集団網羅性は未承認のままである。

採用済み価格の最終日は10月2日で、forward開始前のwarmup資料。
正式な月末評価0回、取引0件を維持する。宣言済み契約はLightで、
日次Growth250は保存されていない。プラン変更や代替指数は行っていない。

## 実際のPAPER run

[10月6日のrun](../../operations/runs/2026-10-06/report.md)を
`operation run start`で開始した。JPXマスターを実ネットワークで取得し、
採用済み公式価格は3659銘柄と監視対象4052を確認した。

4052はWAIT、注文0件。正式MRSはUNAVAILABLE、銘柄入力はINCOMPLETE。
ローカルのEDINET・J-Quants照会と必須開示レビューが未完了であるため、
runをfailedで保存し、成功した開示cutoffを9月14日から進めなかった。
run tokenのleaseは解放した。次回は同じcutoffで再開できる。

追加した`paper-research` jobは既存GitHub Secretsでsource scanを行い、
原本、run、未完了work plan、checkpointを400日保持するレビュー用artifactへ保存する。
artifactから取り込み、再検証してprivateのPRで採用した入力・結果をGitの正本とする。
自動準備は開示レビュー、正式採点、PAPER run完了を代行しない。

## 残る条件

- 利用権のある日次指数と、過去検証に必要な2022年VI・訂正履歴の確認。
- 全母集団の採点、株式数、資金余力、企業行動、全重要開示の確認。
- 9役割の正式再生と、財務・開示を含む出口検証。
- 2027年4月1日までのforward観測、正式月末評価6回・取引10件などの既存基準。
- 条件を確認した本人によるリスク・証券口座の確認と最終LIVE承認。

凍結済み23ファイルと計画は変更していない。復旧試験は要求しない。

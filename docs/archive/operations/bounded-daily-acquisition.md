# 日次Light取得runner

`stock.jp/scripts/jquants_daily.py` は日次取得だけを担当します。PAPER run、注文、
Git書込み、PR、auto-merge、定期タスク登録は行いません。98%/active target gateは変更せず、
成功時も `readiness: NOT_EVALUATED`、`remote_saved: false` を返します。

## 親の起動手順

毎日21:00 **Asia/Tokyo** に親のクラウド実行から開始します。Actionsは検証だけです。
本コードPRを通常review後にmainへ採用するまでは定期取得に使いません。

1. 非公開repoと既存契約・credentialの有無を確認する。値を出力しない。
2. 前回の未保存成果物、未merge日次PR、別writerがないことを確認する。
   同時実行数は全環境を通じて1。ローカルlockは分散lockではない。
3. 清潔なmainを `git pull --ff-only` で最新化する。別branchの未保存成果物を破棄しない。
4. 以下のコマンドをrepo rootで実行する。CLIもremote mainとHEADの一致を再確認する。

```sh
stock.jp/.venv/bin/python -B stock.jp/scripts/jquants_daily.py --root "$PWD"
```

中断したrunを日付跨ぎで再開する場合だけ、最初のJST運用日を明示します。

```sh
stock.jp/.venv/bin/python -B stock.jp/scripts/jquants_daily.py \
  --root "$PWD" --run-date 2026-10-02
```

`--run-date` は過去の新規バックフィルを許可するものではありません。親は既存journalの
再開対象だけを指定します。最新mainへの追従と、未保存のraw/checkpoint保護を両立させ、
競合があれば停止します。失敗を解消するためにjournalを削除・編集しません。

## 日付・公表状態

run日はJST起動日で、価格日は営業日に限りそのrun日です。土日・祝日は取引カレンダーの
`HolDiv=0/3`を非現物営業日として、価格日を作らず `HOLIDAY` を返します。
前日に取得した財務速報の補完は休日でも行います。21時より前の起動は
`WAIT_PUBLICATION` とし、同じrunを21時以降に再開できます。

Lightの9種を日付指定で1partitionずつ取得します。決算カレンダーだけはAPIが日付指定を
持たないため取得時点のlatest応答です。run日と応答内の予定日を混同せず、日付跨ぎの再開も
manifestの実際の取得時刻とqueryを正本とします。投資部門別・開示等の空応答は保持し、
日次価格/master/valuation/TOPIXの空応答は公表待ちで停止し、同runの残予算内で再開できます。
日付不一致はBLOCKEDとします。

財務情報は翌日00:30より前に開始したpartitionを `provisional`、以降を
`after_final_window` と記録します。次のrunは過去の速報を古い順に再取得してから当日分へ
進みます。公表目安の通過は提供元の最終確定保証ではありません。旧revisionは保持します。
同日run完了後に確報時間を過ぎても再取得せず、次のrunで補完します。

## 永続予算と再開

`jquants/runs/YYYYMMDDT120000Z.json` はJST運用日21時を表す固定IDのjournalです。
`started_at`は実際の開始時刻です。codecはこのjournalもchecksum付きで保存・復元します。
キー・個人運用状態は含めません。全期間取得のstatusはpendingのままです。

設定上限以下かつ最大12回を、論理run全体で共有します。各API送信の**前**に予約をatomicに
保存し、ページ追加、失敗、手動再試行も1回消費します。応答前のクラッシュも予約を返却しません。
自動再試行はありません。429は120秒の再開禁止時刻も永続化します。親は提供元の
待機時間を守って再開し、認証拒否を迂回しません。
プロセス再起動や復元でもjournalの使用数を引き継ぎます。失われた未保存journalを旧mainから
復元して再取得すると予算を保証できないため、環境喪失時は不明なrunを再実行せず報告します。

最新partitionに加え、保持する全revisionと全参照pageの存在・安全なpath・hashを
通信前およびcodecのpack/verify/restore時に検証します。過去revisionだけが参照する
pageも対象です。取得済みpartitionを検証後に再利用します。部分取得は既存checkpointから再開し、
破損、設定変更、lock競合、未完了の前日runがあれば新しいrunを開始しません。
上限到達は同runで解除せず、別run IDを作って上限を回避しません。

rawが存在しない新環境ではmainの `stock.jp/data/jquants-archive/` を空のraw領域へ復元します。
既存rawにはoverlayしません。親はそのrawが前回の採用世代と未保存成果物に対応することを確認します。
一般の全履歴CLIと同時実行しません。既存archive lockも保持します。

## 終了状態と保存

| status | exit | 親の処理 |
| --- | --- | --- |
| COLLECTED | 0 | 当日取得完了。readinessとは別に圧縮・保存を検証 |
| HOLIDAY | 0 | 価格日なし。財務補完・calendarの変更があれば保存 |
| WAIT_PUBLICATION | 2 | 同じrunを公表時間以降に再開 |
| PAUSED | 2 | 通信等の失敗。残予算内で同じrunだけ再開 |
| BUDGET_EXHAUSTED | 2 | 12回等の上限到達。停止して未取得範囲を報告 |
| BLOCKED | 2 | 認証・schema・checkpoint等。停止して原因を調査 |

完了済みrunの再実行はAPIを呼ばず既存結果を返します。BLOCKED/予算消尽も同日再実行で
リセットしません。未解決の停止runを抱えたまま翌日の新runも開始しません。
解消にコード変更や追加取得承認が必要なら別途扱い、journal手編集で解除しません。

親は既存codecでpack/restoreし全checksum、秘密情報、容量を確認します。
圧縮世代だけをdata専用PRにし、正確なheadの全CI・意味検証・reviewを確認します。
失敗runの保存PRは自動mergeしません。成功runでも全期間取得完了やPAPER成功を主張しません。
既存の[日次PR手順](daily-pr-publication.md)に従い、native auto-mergeがCLEANで拒否されたら
直接mergeへ切り替えず「保存済み・未採用」と報告します。この制約の変更は別途検討中です。
merge後はremote mainから独立復元して全checksumを照合してから保存完了とします。

## クラッシュ回復と回帰検証

partition公開後、checkpoint削除前のクラッシュでは、完了checkpointを公開済みmanifest、
不変revision、全pageと照合します。endpoint/query/開始時刻/page一覧が一致した場合だけ、
排他lock内でcheckpointを除去してdirectoryをfsyncします。元pageとrevisionは保持します。
不一致を「古いcheckpoint」と推測して消さず、通常の再開・調査へ戻します。

合成テストはこの境界から同日再開・翌日財務補完までを確認します。旧pageの破損・欠落は
通信前と圧縮検証で拒否し、既存current pointerを維持します。日次runner自身の20暦日統合試験は
休日、財務revision更新、通信中断・再開、同日重複、5日ごとのpack/restoreを含みます。
`operation_smoke.py --days 20` は別のPAPER状態遷移試験で、日次取得runnerの試験ではありません。

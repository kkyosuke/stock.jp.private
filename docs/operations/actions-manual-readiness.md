# 手動Actionの開始日と取得前の確認

## 2026-10-04のwriter・予算照合

- 親が旧dot task `6ac043c533dc8191b37cc7ab6aab1af7` を停止。
  親からの実施結果は `success:true`、`is_enabled:false`、`next_run_time:null`、
  更新時刻 `2026-10-04T00:51:24.564566+00:00`。親は旧runnerを今後起動しないと通知した。
  これは親が確認した事実であり、executorからautomation APIを再検証したものではない。
- executorの旧writer作業コピーとbatch 1復元コピーは、取得データ83ファイルが
  全件ハッシュ一致。旧作業コピーのHEADはremote branchと同じ `a765dd7` でclean。
  ローカルのみの追加結果・変更は見つからなかった。
- journal SHA-256は
  `cdf57bd1df68ba160b46c55f670b7bf50d84a38c80eb5fbc71292f7c5d75f83e`。
  12予約・1batch・10完了task。全予約を保存済み応答と1対1で照合した。
  最終予約は `2026-10-03T13:01:48.425907+09:00`。
- 確認時に取得プロセスはなく、取得lockは保持されていなかった。
  残存gitプロセスはzombieであり、実行中writerではない。
  このプロセス確認は当該executorに限る。
- 未採用payloadは取り込まない。旧12回・1batchを保持するmetadata-only debitを
  準備できる。最初の2025-08-07に共有2回を含む4回、08-08・08-12・08-13・08-14に
  各2回を計上する。新規取得に残る総枠は、この照合対象について588回・49batch。
  当該日付の12回上限にも旧消費を含める。

この確認は承認manifestの採用・checkpointの作成・Action有効化や実取得成功を
意味しない。停止証跡と予算metadataを既存のscopeへ反映してから実行時検証を行う。

## 設定不足を一括確認する

実取得を伴わないローカル診断:

```sh
python stock.jp/scripts/actions_readiness.py --root .
```

対象日Actionも同じ診断を最初に行い、`execution_disabled` だけでなく
承認pin・credentialの有無・承認manifest・退役証跡の不足をまとめて返す。
credentialの値は出力しない。診断はネットワーク通信も設定変更も行わない。
手元の環境にsecretがないことは、Actions側のsecret欠落を意味しない。
`LOCAL_CONFIGURATION_PRESENT` はファイルと設定の存在確認にすぎず、実行許可ではない。
真正な停止証跡、既存checkpoint、未解決履歴、消費予算と承認内容は実行時に検証する。
旧外部controllerサービスを新設する必要はない。

2026-10-04の再確認ではmain `151d8eb` に承認manifest・退役証跡がなく、
checkpoint refは404。実行中・待機中Actionsとopen市場データPRはなかった。
GitHubだけでは旧cloud writerやdotの停止を証明できない。追加の照合結果は上記参照。
未採用履歴branchは引き続き下記のcommitにあり、予算のリセットや採用は行っていない。
手動run `37164152501` では親が確認したログ上、取得有効化変数と承認pinが空で、
既存の2つのsecretは非空だった。secretの有効性・scopeは未検証である。

## 開始日の回答

既存の280営業日計画を継続する場合、先頭候補は **2025-08-07**。
ただし、これは現時点で取得を開始してよいという承認ではない。
テストfixtureの日付（2026-09-23など）を実運用の開始日に使わない。

PR #53採用後のmain `2649898519d4792edc2329e24cd83edb38d6b2dc` を
読み取り専用で確認した結果は次のとおり。

| 対象 | 確認結果 |
| --- | --- |
| mainの採用generation | `3a893a785ff56975e6ae15c461d207a0555608aa57d86542844d5b8cea1276c8` |
| mainのmaster・株価 | 2026-10-02の1日分。`one_day_validation_only` |
| mainの履歴・日次journal | なし |
| 未採用branch | `daily/market/2026-10-03`、commit `a765dd744a0c339b8b9cf8482ddfba1262c3075a` |
| 未採用branchの履歴計画 | 2025-08-07〜2026-10-02、280営業日 |
| 保存された履歴状態 | `BATCH_EXHAUSTED`、1 batch、計上12リクエスト、完了task 10件 |
| main上の承認manifest・退役証跡 | なし |
| Actions checkpoint ref | GitHub APIで404。実行時の自動再作成は禁止 |

未採用branchのデータをmain採用済みと扱わない。公開拒否された履歴をActionsで
再送しない。確認できた12回は保存済みbranchの値であり、旧writerに未保存の追加取得が
ないことまでは証明しない。最終台帳を照合するまで「残り588回」と断定しない。
新campaignで600回を再付与する初期化も行わない。

## このコード変更で可能になること

Actions画面の **Manual target-date acquisition** は、`kkyosuke` による
`main` の手動起動だけを受け付ける。cron、push、PR、他branch、他actorでは起動しない。
この入口をmergeしても、未設定の取得条件が自動的に承認されることはない。

承認条件が揃った後、所有者がActions画面で `main` と承認済み `target_date` を選ぶ。
今は下記の取得前条件が未解決なので、取得目的で実行しない。
`execution_disabled` などの停止結果は正常な安全動作であり、flagだけを変更して
突破しようとしない。CLIの設定不足メッセージにcredential値は表示しない。

## 手動実行の信頼境界

- workflow条件でrepository、`refs/heads/main`、`workflow_dispatch`、
  `github.actor` と `github.triggering_actor` が共に `kkyosuke` であることを要求する。
- CLIでも同じactor/context条件を検証する。GitHubのrun-attempt GET応答でもactor、
  triggering actor、event、workflow path、head、run ID、attempt、稼働状態を照合する。
  環境変数だけを偽装しても通らない。
- rerunでは元のrun IDを保持し、増加したattemptを使う。旧attemptの終了と
  未解決intent・不明な送信結果の不在を確認し、既存ledgerを引き継ぐ。
- checkoutはcredentialを永続化しない。実HEADと現在main、承認baselineからの
  許可差分を検証する。手動入力は引用した環境変数で渡し、shellへ直接展開しない。
- 既存の `AUTOMATION_PAT` / `JQUANTS_API_KEY` secret参照を使用する。
  新credentialや権限設定を作らない。default tokenは `contents: read` のまま。
- `stock-private-market-writer` concurrencyと永続owner/CASを維持する。
  12回／日付、600回／履歴campaign、50共有batchの予算はrerunで戻さない。
  外部writerの任意の復活まで防げるという保証ではない。

`workflow_dispatch` はUIだけでなくCLI/APIからも呼べるため、actor一致は人間が
画面をクリックした証明ではない。今回、自動dispatchは追加せず、旧writerの実停止を
別条件として要求する。起動手段は
[GitHubの手動実行手順](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)
を参照する。

GitHubのactor・rerunの意味は
[公式変数リファレンス](https://docs.github.com/en/actions/reference/workflows-and-actions/variables)、
照合するAPIは
[workflow run attempt取得](https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run-attempt)
に従う。既存CI runへの読み取りでもactor/triggering_actorフィールドの実在を確認した。

## 親・所有者側で実際に確認すること

1. 旧dotスケジュールを実際に停止する。対象task IDと実観測時刻を記録する。
2. 進行中・再開待ちの旧cloud writerを確認し、全て終了していることと最終取得台帳を
   確認する。スケジュール停止だけをwriter終了の証拠にしない。
3. 未採用・拒否campaignの扱いと、少なくとも保存済み12回を含む総消費をレビューする。
   拒否payloadを転送せず、既存上限内で予算を継承する方法が必要。
   metadata-only debitの形式は[対象日運用手順](actions-target-date.md)を参照する。
   単に空のcheckpointを作って既使用枠を無視してはならない。
4. [退役証跡の形式](actions-target-date.md#activation-prerequisites-not-performed-by-this-change)
   に従い、真正な停止証跡を用意する。時刻や終了状態を捏造しない。
5. 最終コード採用後のbaseline、開始日・終了日・営業日数、checkpoint初期SHA、期限、
   owner承認を含むscopeを別途レビューする。現在の未設定manifestを自動生成・承認しない。
6. 既存secretの有無と必要scopeを所有者側で確認する。今回、secret名一覧APIはHTTP 403
   だったため有無・scopeを確認できていない。値の表示や別経路での回避は行っていない。
7. 上記を満たした後に取得有効化を別途判断する。このcode PRはレビュー待ちで、
   merge・dispatch・旧scheduler停止・secret/権限変更を実行していない。

日次の最新日も同じ入口を使うが、JSTの実行日と対象日を偽装しない。
履歴開始日や既存campaignを変更して未解決予算を回避しない。

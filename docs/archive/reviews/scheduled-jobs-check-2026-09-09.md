# 日次定期処理の実行確認（2026-09-09）

確認時刻: 2026-09-09 21:10 JST。GitHub APIのworkflow・run・PR・remote mainと実データを確認した。

定期起動は継続しているが、データ取得からprivateへの反映までの自動処理は完了していない。
publicの最新データPRは未merge、privateの今朝の更新は失敗している。
夜間運用は依頼を受けて実行する構成で、remote main上の最終記録は9/4分である。

## 処理ごとの結果

| 処理 | 設定 | 確認結果 |
| --- | --- | --- |
| public `daily-stock-prices` | 平日18:30 JST、active | 最新の定期runは9/8 22:46:09開始、22:48:20成功。ただし生成PR #50は未merge |
| private `update-public-submodule` | 毎日7:00 JST、active | 9/9 08:57:58開始、08:58:20失敗。更新PR作成以降のstepはskip |
| 夜間PAPER運用 | 利用者の依頼で実行 | 最終run IDは2026-09-04、完了は9/5 11:58 JST。9/7・9/8分の完了記録なし |

9/9のpublic定期runは確認時点で履歴にない。設定時刻18:30を過ぎているが、直近も設定より
遅く起動しているため、当日分の欠落が確定したとは扱わない。遅延原因は未特定。

実行履歴:

- [public最新の定期run](https://github.com/kkyosuke/stock.jp/actions/runs/34234016222)
- [private今朝の失敗run](https://github.com/kkyosuke/stock.jp.private/actions/runs/34292944707)
- [夜間運用のremote main上の記録](https://github.com/kkyosuke/stock.jp.private/blob/5b4e88de22d0435f061ed1f52f32d9e5fc0e5be0/operations/private/run-history.csv)

## public側の未反映

[データPR #50](https://github.com/kkyosuke/stock.jp/pull/50)には9/8分までのデータがある。
auto-mergeは有効だが、PRは`OPEN / BLOCKED`。
PRイベントの[operation-tests](https://github.com/kkyosuke/stock.jp/actions/runs/34234230322)は
`action_required`で、jobは0件。別途dispatchされた
[operation-tests](https://github.com/kkyosuke/stock.jp/actions/runs/34234228284)は成功している。
mainの必須checkは`test`だが、PRの`statusCheckRollup`は空だった。
`action_required`の具体的な要求内容は今回取得したAPI情報では確認できていない。

public mainの最新価格日は9/7、privateが採用中のpublic commitの最新価格日は9/4。
workflowの成功表示だけでは、mainへのデータ反映成功を意味しない。

## private側の停止要因

今朝のrunはpublic `179016d58c968be5fd0c39df8f47020edc8891a0`へ切り替え、
private stateの検証は`valid: true`を返した。その直後のreadiness確認でexit code 1となった。
スクリプトはreadiness出力を一時ファイルに保存し、失敗時は`set -e`で表示前に終了するため、
runログにはreadinessの詳細が残っていない。

対象commitの価格validatorと実際のCSV・manifest、privateの価格設定を使い、
今朝の実行時刻を基準に価格検証部分を再現した。

| 検証対象 | 最新価格日 | 株価あり / 全銘柄 | カバー率 | 最低98%の価格検証 |
| --- | --- | --- | --- | --- |
| public main `179016d` | 2026-09-07 | 4 / 3,707 | 0.108% | 失敗 |
| PR #50 `82e3465` | 2026-09-08 | 3,662 / 3,707 | 98.786% | 成功 |

mainでは`tracked Yahoo full-universe coverage is below the daily minimum`を再現した。
したがって現行mainの価格不足はprivate更新を止める要因である。
再現対象は価格validatorのみであり、PR #50採用後のbootstrap全体の成功は未検証。

前日のprivate定期runは[9/8 09:06 JSTに成功](https://github.com/kkyosuke/stock.jp.private/actions/runs/34172245648)したが、
採用commitは既存の`a2f67e7ee838e7e164ca3e621e5857b477b91206`のままで、
価格日も9/4のままだった。9/3〜9/8は毎朝の定期runが成功、9/9に初めて失敗している。

## 復旧に必要な対応

1. public PR #50の`action_required`の要求を確認し、必須`test`の成功とmergeを確認する。
2. public mainの正常な価格データ反映後、private更新を再実行してreadinessとpointer PRのmergeを確認する。
3. 夜間運用が必要な日は、データ同期後に実行し、run-historyへの完了記録を確認する。
4. 再発防止として、publicの自動採用条件に価格カバー率の下限を設け、privateのreadiness失敗理由をログへ出す。

今回行ったのは状態確認と隔離した価格検証、本文書の作成。workflowの再実行、PR承認・merge、
本番設定変更、夜間運用の実行は行っていない。

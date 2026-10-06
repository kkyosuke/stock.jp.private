# MRS入力のpersonal PR公開形式

公式指数adapterが出力する`<date>-jquants.json`と
`information_cutoff_at_jst`を、personal PR validatorで検証する。

- 入力追加は1ファイルだけで、MRSログのcanonical appendと同時に公開する。
  既存入力の変更、任意のsuffix、別月・別日の入力は許可しない。
- baseで採用済みの`index_provider=jquants_archive`なら`-jquants.json`を要求する。
  従来providerは`<date>.json`を維持する。PR内の設定変更で切り替えない。
- 明示cutoffは文字列・timezone-aware・JST換算でas_of当日を要求する。
  公式指数では必須。従来入力のcutoff省略時の既存挙動は変更しない。
  閾値、未知フィールド、as_of、assessment、ログ追記の照合を維持する。
- 公式指数は保存原本から200営業日とPITを再検証し、入力系列全体を照合する。
  原本欠損、cutoff後の確認、終値改変、偽のPITフラグ・出典を拒否する。
  検証した系列・原本参照のhashを公開検証proofへ追加する。
- この検証はローカルの保存原本だけを読み、provider通信も原本補作も行わない。
  価格証拠と同様、validator実行環境には保存済み指数原本の復元が必要となる。

凍結23ファイル、MRS計算式、研究計画は変更しない。
10月6日取得の後日原本だけで過去cutoffのPITは立証できない。
形式が整合しても、現在の正式MRSはUNAVAILABLEのままであり、分析成功・
日次finalize・投資可能判定を意味しない。テストの成功例は合成原本だけを使う。

## operation-healthの実作業

main `a899bd5801e4304e5ec0e7af6cace0bffbb8d435`では、保存済みの最終正式日次runは
9月14日、次回予定は9月15日、繰越価格gapは3件である。10月6日の市場データ取得完了は
この日次台帳を更新しない。403になったActionsログの取得やアクセス回避は行わない。

1. 原本レビュー、対象と開示期間の網羅性、元のcutoff、未完了課題を確認し、
   work-plan・sources・coverage・research-queue・翌日行動・handoffを整える。
   4052の年次資料と投資ケースの繰越レビュー、PAPER source inboxの未確認項目を
   確認済みへ機械的に変更しない。cutoff後の証拠を旧runへ遡及しない。
2. 9月3日・9月4日・9月11日の4052価格gapについて、独立復元した保存価格CSVに
   各日1行があることを確認した。対応する公式原本、銘柄・日付・価格調整・単位を
   実際に突合し、解消根拠と確認時刻を記録する。当時確認できなかった事実は残す。
   行があるだけではgapを解消扱いにしない。未解消項目は理由と次の作業を引き継ぐ。
3. 必須の日次確認が済み、canonical finalizeの検証を通る正当なrunを完了・採用し、
   実際の完了時刻・cutoff・次回予定を台帳へ反映する。これがstaleness/繰越警告を
   更新する経路であり、stateの日時やgap件数だけを書き換えない。

注文0件のWAITでも必要な日次確認を完了すれば日次完了は可能だが、投資可能とは別。
必須確認が未完了なら警告を残す。今回のPRでは運用台帳やレビュー状態を更新しない。

## 差分がないMRSの検証

完了runにはassessment-statusを要求する。CURRENTは入力/ログの差分がない場合も
無条件通過させず、設定から決まる参照入力の実在、baseとの同一性、保存原本のPIT、
再計算結果、既存ログの全項目・出典・時刻を検証する。既存ログが返すcompact形式も
同じ再検証を通った場合だけ許可する。欠損・偽CURRENT・未来ログ・cutoffより前の
計算記録は停止する。UNAVAILABLEはゼロ倍率・log未記録・理由を持つ独立した保留状態とし、
CURRENTのスコアや参照入力、ログ変更を混ぜて公開することを許可しない。

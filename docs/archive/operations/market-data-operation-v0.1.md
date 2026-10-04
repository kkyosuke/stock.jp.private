# 非公開株価入力・監視範囲 v0.1

## 結論

市場データの保存・参照は非公開ローカル領域のみとする。現在、Yahoo全銘柄自動取得は停止中。J-Quants接続の安全な設定完了までは、一次資料調査を続け、全市場株価を確認できなければPAPERはWAITとする。

夜間運用は、マージ済み最新日の全市場CSVから保有＋active watchlistだけを読み、次をすべて満たす場合だけPAPER価格入力として使う。

- `latest.json`が採用providerと公式・非公式のsource identityを明示し、設定と一致している
- 最新CSVのSHA-256がmanifestと一致する
- 全市場の正常価格が98%以上
- 保有＋active watchlistが100%存在し、`取得状態=OK`で正のOHLCと非負出来高を持つ
- 価格日が未来でなく、7暦日を超えて古くない

不合格なら前日値で補完せず、`nightly_operation.py start`をrun作成前に停止する。

保有・active watchlistが0件でも、全市場archive自体が正常ならPAPERを開始できる。その場合は個別注文を作らず`GLOBAL / NO-ACTION`を保存し、初回の全市場候補レビューを必須タスクにする。AIは蓄積済み日足で候補を絞り、一次資料を確認してから少数だけactive watchlistへ登録する。

## 取得と利用の分離

| 範囲 | 取得頻度 | 用途 |
| --- | ---: | --- |
| JPX現行内国株式全体 | 平日ごと | 欠損監視、候補探索、月次候補集合の見直し |
| 保有＋active watchlist | 毎夜、全体CSVから100%照合 | PAPER価格計算、翌営業日アクション |
| 会社IR・TDnet・JPX・EDINET | 毎夜の人によるAI指示後 | 会社事実、開示、公式価格、コーポレートアクション、取引日を確定 |

Yahoo Financeは公式または契約APIではなく、候補探索とPAPERの二次価格源である。Yahooだけで会社の事実、分割・併合、上場状態、売買判断を確定しない。夜間runには公式価格・コーポレートアクション確認を含む一次資料タスクが常に作られ、根拠が揃うまでfinalizeしない。

## 日次自動収集

実コマンド、検証、非公開設定の順序は[株価収集とガードレール](daily-stock-price-actions-v0.1.md)を正本とする。利用条件・取得方法を承認された入力について、取得エラー0件、98% coverage、lookback内の変更、archive、全test、compile、20日smokeを確認する。失敗時は採用せず欠損を報告する。Actionsの手動起動でも収集・PR作成・mergeは行わない。

検証済みprivateデータと採用したコードcommitを別々に照合する。更新が未反映またはcheckoutが古い場合、鮮度ゲートが夜間runを停止する。スケジュールの別途設定と成功実行確認が必要で、この文書だけでは登録されない。

## 月次全体見直し

全市場データの取得済み範囲をmanifestで確認し、欠損を取得済みとみなさない。月次は許諾された蓄積済みデータを使う。月次タスクでは当月のJPX一覧と蓄積済み日足を使い、価格変化、欠損、上場区分、流動性を機械抽出した後、AIが少数候補だけ一次資料で確認する。利用者が全銘柄を手入力したり、AIが毎日3,713社を個別調査したりする必要はない。

候補をactive watchlistへ昇格した直後は、最新CSVにその銘柄の正常OHLCVがあることをreadinessが再確認する。月次候補抽出は買い判断ではなく、公式資料を読む対象を絞る工程である。

## 保存場所

- `operations/private/market-data/daily-prices/YYYY/YYYY-MM-DD.csv`: 許諾確認済みの全市場日足。Git追跡禁止
- `operations/private/market-data/daily-prices/latest.json`: 最新日、母集団、欠損、CSV checksum
- `operations/private/portfolio-register.csv`: 保有と価格由来指標
- `operations/private/watchlist.csv`: 人が確認対象としたactive銘柄
- `operations/private/runs/YYYY-MM-DD/sources.csv`: 採用した株価providerと一次資料の利用証跡

認証情報はGitへ追加しない。個人の保有数量・原価・資金、調整補助表は`operations/private/`に置き、public Gitではなく親のprivate Gitで追跡する。

## 既知の限界

- 現行JPX一覧は過去時点の上場廃止銘柄を完全復元しない。point-in-time検証は別LIVEゲートである。
- Yahoo日足は非公式で、価格訂正や仕様変更があり得る。公式価格・コーポレートアクション確認を省略しない。
- private保存先の不在・欠損・取得障害はreadinessを停止する。コードの採用状態とデータの鮮度は別々に確認する。
- 祝日中は最新価格日が進まない。公式JPXカレンダーを毎夜確認し、価格日と次取引日を明記する。

# 日次価格の戦略対象と投資開始条件の監査（2026-10-03）

基点は private main `f699f78b0736a2f2d2014b8767d4393c7a368f71`。
このレビューは現行checkoutと保存済みデータだけを使用し、新規API取得、注文、
設定・権限・基準変更、LIVE昇格は行っていない。

## 今回の変更

J-Quantsの日付別masterの内国普通株（ProdCat=011、5桁コードの末尾0）から、
市場コード0105（TOKYO PRO MARKET）を価格の有無によらず全件除外する。
過去市場0101/0102/0104/0106/0107と現在市場0111/0112/0113を認識し、
市場不明・欠損・未対応は全体をfail-closedとする。現在のmasterで過去を選別しない。
raw master・日足およびその全revisionは保持する。

manifestの`acquisition`は全masterに対する日足行の取得完全性、
`latest_session`は戦略対象の有効価格を示す。双方に次の診断件数を記録する。

| 診断 | 意味 |
| --- | --- |
| ROW_MISSING | masterにあるが日足行がない |
| NO_EXECUTION | O/H/L/C/Vo/Vaがすべて存在し、明示的null |
| PARTIAL_NULL_OR_INVALID | キー欠損、部分null、不正数値、OHLC矛盾など |
| VALID_PRICE | 未調整OHLCと正の出来高・売買代金が有効 |

NO_EXECUTIONはAPIの対象となる立会内で約定がなかった状態を示す。
売買停止と取引不成立の理由は判別できず、立会外取引の有無も示さない。
JPXの売買停止・監理整理等の確認を代替しない。
既存CSVのOK/NO_QUOTE/FETCH_ERROR契約を維持し、細分類はmanifestに置く。
無約定・欠落・不正価格の銘柄を戦略分母から都合よく落とさない。

価格98%、active-target価格100%の閾値を維持する。
旧scopeのmanifestは新しい戦略対象の証跡として受け付けず、保存済みrawから
snapshotを再生成する必要がある。scopeの文字列だけを書き換えて移行しない。
既存の完了済みdaily journalを再実行しても自動で再生成されるとは限らない。
本PRはコードのみであり、既存raw・snapshot・journalの書換えは含まない。

## 保存済み10月2日データでの検証

隔離した一時ディレクトリにsnapshotを生成し、価格validatorを実行した。
元のraw全ファイルは検証前後のSHA-256が一致した。

| 指標 | 結果 |
| --- | --- |
| master／日足行 | 4,442／4,442（行取得100%） |
| 全masterの診断 | VALID_PRICE 4,198、NO_EXECUTION 244、欠落・不正0 |
| 旧内国普通株 | 3,880 |
| PRO MARKET除外 | 191（有効価格あり1を含む） |
| 戦略対象／有効価格 | 3,689／3,659 |
| 戦略価格coverage | 99.1868%（3,659 / 3,689） |
| 戦略対象の無約定 | 30（欠落・不正0） |
| active-target価格coverage | 100% |

隔離コピーでbootstrapはREADY_WITH_WARNINGS、PAPER_GO=true、
LIMITED_LIVE_GO=false、LIVE_GO=false。暗黙migrationと初期化は0件。
これはPAPER準備チェックの結果であり、実データによる夜間run完了やBUY判定ではない。

## 投資開始までの残件

| 項目 | 実装／保存データ／残作業 |
| --- | --- |
| TOPIX・Growth250 | 公式取得・200観測MA計算は実装済み。保存入力は各286観測、2025-06-02〜2026-07-31。判定日と最終日一致が必要。Growth250は旧JPX抽出形式としてvalidatorが拒否し、正式形式での再抽出・確認が必要 |
| 日経VI | 取得・3年窓80パーセンタイル計算は実装済み。895観測を保存（2023-01-04〜2026-08-31）。3年窓内500以上と判定日一致が必要。現在判定用の更新が未実施 |
| CI先行指数 | 公式取得と公開日時チェック、厳密な3か月前との比較は実装済み。保存最終期2026-06、公開日時2026-08-25。現在cutoffで採用する公表版の確認・更新が必要 |
| breadth | 200観測MA、280session lookback、98%coverage、最終価格7日以内の検証は実装済み。現行private価格archiveは1sessionだけで「200必要、1件」と拒否。履歴不足を旧公開Yahooデータで補わない |
| active-target流動性 | 平均・中央値計算は実装済み。現取得は1日分で20日証跡を満たさない。現実装は45暦日の取得範囲から直近20の正の売買代金観測を選ぶ。「連続20営業日」と同義でなく、無約定日を飛ばす点は定義・実装修正の別課題 |
| 公式source coverage | TDnet、EDINET、会社IR、JPX注意情報、価格・企業行動、カレンダー確認経路はある。validatorは完了済みv0.4 PAPER runがないため不合格。EDINETキーは未設定。手動一次証跡でのPAPER経路はあるが、未確認を完了扱いしない |
| ledger・risk・source gate | bootstrap上の台帳／policy検証は通過。BUY/ADDには当該runのassessment PASSと利用可能なMRS、source証跡、資金・集中・流動性条件が別途必要。価格coverageだけでは通過しない |
| private復元 | archiveのpack/restore確認は実施済み。ただし正式recovery証跡は現行layout不一致、code SHAとclean-clone確認未充足。実験成功とLIVEゲートの証跡受理を分ける |
| 実スケジュール | 日次runnerのテストとCIはある。予約した時刻での最初の実発火、予算内取得、保存・PR・CI・復元までの一連の本番実行は未確認 |
| 発注 | HUMAN_ONLYを維持。翌朝08:45〜08:55の本人確認・手動発注が必要。自動証券注文は今回も対象外 |

LIVE必須6ゲートはすべて不合格：point-in-time全銘柄検証、2025–2026 replay受入、
公式source coverage、private復元、個人リスク／証券会社確認、v0.4 holdout昇格。
point-in-timeとreplayの正式outputが未作成で、レビュー承認も未入力。
v0.4は2026-09-01凍結のため、それ以前の結果をforward holdoutにできない。
将来の未観測期間と受入基準の事前固定、その結果の受入、最終LIVE承認が必要。
365日PAPERと20回shadowは任意診断であり、新しい必須条件として加えない。

LIMITED_LIVEも、承認済み計画、専用資金ledger、損失上限、公式coverage、復元、
個人確認が未充足。個人の資金・損失許容・税務・証券口座・手動実行可否・承認は
本人判断であり、エージェントが値や同意を埋めて通す対象ではない。

既存LIVE関連文書とholdout条件にはJ-Quants不使用の旧方針が残り、
許可済みの日次J-Quants経路と整合していない。今回のPRでLIVE証跡への採用可否や
holdout契約を変更せず、別途整合確認する。収集できた事実をLIVE承認と混同しない。

## 根拠と検証箇所

- [J-Quants日足仕様](https://jpx-jquants.com/ja/spec/eq-bars-daily)
- [J-Quants銘柄マスター](https://jpx-jquants.com/ja/spec/eq-master)
- [JPX TOKYO PRO MARKET](https://www.jpx.co.jp/equities/products/tpm/outline/index.html)
- `stock.jp/scripts/jquants_snapshot.py`、`price_snapshot.py`
- `stock.jp/scripts/market_regime.py`、`market_regime_sources.py`
- `stock.jp/scripts/non_edinet_sources.py`、`order_ticket.py`
- `stock.jp/scripts/live_gate_evidence.py`、`limited_live.py`
- `docs/live-readiness.md`

回帰テストは価格ありPRO、市場欠損・未知市場、日付別市場変更、行欠落、
全null／部分null／不正値、旧manifest拒否、合成3,659/3,689集団と
無約定active-targetの拒否を含む。金融API通信を禁止した環境で実行する。

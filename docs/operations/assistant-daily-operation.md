# アシスタント日次実行契約

## 所有者と稼働状態

両リポジトリは非公開・本人限定です。文書中のpublicはstock.jpの歴史的な呼称です。

この会話のアシスタントが、毎晩21:00頃（Asia/Tokyo）の情報収集、調査、PAPER処理、
結果保存と報告を担当します。GitHub Actionsは既存入力の検証とread-only監視だけです。
CloudflareやActionsへ夜間runを委譲しません。

これは実行契約であり、稼働宣言ではありません。定期起動は別途作成・確認し、最初のrunについて
実際の調査、検証、private remote保存、利用者への報告まで確認してから稼働中とします。
構成変更PRの作成・mergeや、start/finalizeの成功だけでは稼働確認になりません。

## 実行に必要な能力

- privateとpublic repositoryを読むこと、履歴・最新SHA・required checksを確認できること
- checkout、submodule、Python依存、既存scripts/skillを使える実行環境
- 公式サイト・一次資料へ到達できるブラウザまたは認可済みconnector
- 利用条件を確認した削除可能なprivate local市場データ領域とprivate Git結果保存経路
- 永続run状態を毎回読み、失敗・中断後にも再開または停止を判断できること
- JSTで定期起動し、この会話へ結果・blockerを報告できること

利用できるクラウド実行環境・connectorで満たしてよく、特定の端末だけを必須にしません。
ただしこの会話の継続だけでfilesystem永続性・接続・push成功を仮定しません。
能力不足や期限内に復旧できない接続障害はWAITとして報告し、成功状態を作りません。
credentialは認可済み保管場所から実行時だけ利用し、新規権限・秘密鍵・PATの作成を日次作業に含めません。

起動は毎日です。取引日は全nightly pipeline、週末・祝日は期限到来reviewとデータ可用性を確認し、
架空の価格日や営業日runを作りません。既存nightly engineの次回営業日handoffと毎晩の起動を区別します。

## 1. 同期と排他

1. private main、前回run、handoff、未解消data gap、未push/未保存結果を読む
2. 別端末・別runが稼働していないこと、worktreeに不明な変更がないことを確認する
3. `git pull --ff-only`、固定public submoduleの初期化、setup、state/ledger検証を実施する
4. `nightly_operation.py status`と既存run token/lockを確認する
5. 同じJST運用日とcutoffの完了済みrunなら二重startせず、保存済み結果を返す
6. RUNNINGなら同じtokenでのみ再開し、lockを推測で削除・上書きしない

既存run lockはローカル排他です。複数環境に対する分散lockと誤認しません。
定期runnerは同時実行数1とし、唯一のwriterに固定します。別writerやremote先行更新を検知したら停止します。
再試行で新しい注文・台帳eventを複製せず、run ID/token/event IDとremote SHAを照合します。

## 2. 市場データと一次資料の収集

既存の`stock.jp/.agents/skills/japan-stock-operator/SKILL.md`とdaily-run参照、凍結rule、
運用policy、source-configを毎run確認します。J-Quantsは契約範囲・安全な資格情報登録を確認後、
別途provider実装と取得を有効化します。このstorage変更だけで有効化しません。

- Yahoo bulk取得を停止し、許可されたprovider以外から全市場価格を自動取得しない
- 取得元の自動取得・保存・分析・削除条件を確認してから、認可済みcollectorで取得・検証する
- archiveは`operations/private/source-config.json`に明示したprivate local領域へ保存する
- rawの作業領域はGit追跡外とし、圧縮世代を非公開stock.jpの`data/jquants-archive/`へ保存する。旧`data/daily-prices/`へfallbackしない
- Yahoo等の補助データを公式証跡と偽らない
- JPXの取引日・銘柄master・価格、TDnet、EDINET、会社IR、必要な内閣府・日銀資料を調べる
- 公表時刻、取得時刻、URL、対象期間、単位、cutoff、取得失敗とcoverageを証跡化する
- search snippetや二次記事だけで判定せず、本文・原資料の該当箇所を確認する
- 公式資料が取れなければdata gapを記録し、欠損を推測で埋めない。EDINET未疎通をLIVE合格にしない

情報収集の対象は保有だけでなく、run work-planの全対象、期限到来review、開示event、
候補選定と必要な市場regime入力です。古いassessmentを無条件で持ち越しません。

## 3. 共通データ・個人分析の境界と採用

市場データの圧縮正本は非公開stock.jpの`data/jquants-archive/`です。個人分析はstock.jp.privateへ分離します。
共有は本人のプロジェクト間利用であり、第三者配信や公開の許可ではありません。
保有・監視リスト、資金、数量、個別判断、会社別research-inputs、私的source使用記録、
run成果物はprivateに置きます。取得元の条件が派生物にも適用される場合はその条件を守ります。
private dataをpublic job、PR本文、log、cache、artifactへ渡しません。迷う出力はprivate扱いです。

private archiveは既存coverage/checksum validatorで検証します。archiveが欠ける環境では
WAIT / NO-ACTIONとし、古いpublic archiveでfinalizeしません。公式IR・開示資料の独立した調査は
続けられますが、全市場価格の検証が未達のままnightly成功とは報告しません。
運用に使うコードは検証・review済みpublic mainのcommitだけです。
code/rule/schema/workflow変更は通常reviewにし、data-only validatorを迂回しません。
既存public archiveと履歴の処理は[非公開保存・移行手順](private-market-data-storage.md)に従い、
この構成変更だけで移動・削除・履歴書換え・再公開しません。

## 4. PAPER runの実作業

1. data鮮度、coverage、checksum、state、ledger、readinessを確認する
2. `operation_mode: PAPER`と`broker_submission: HUMAN_ONLY`を確認する
3. `nightly_operation.py start`を唯一のrun入口として、発行されたtokenを使う
4. work-planの全対象について、一次資料・rule ID・FACT/CALCULATION/JUDGMENTを記録する
5. 必要な会社別assessment、MRS、希薄化、SAM/SOM、競合、KPIを更新しvalidatorを通す
6. 全対象の`next-day-actions.csv`と`research-results.md`、work-planを完成させる
7. 許可される仮想注文だけを既存`order_ticket.py propose`で作り、`PAPER_PROPOSED`を維持する
8. gate未達・資料不足の対象はWAIT / NO-ACTIONとし、未完了項目をhandoffへ残す
9. 同じtokenで成果物検証後にfinalizeし、state/run-history/handoff/order/ledgerを照合する

本人のLIVE昇格、損失許容の承認、資金移動、broker操作は実施しません。LIMITED_LIVEもこの日次委任の
対象外です。modeが予期せず変わった場合は停止し、policyをPAPERへ書き戻して隠すこともしません。
readiness、risk、source gateを日次完遂の都合で緩和しません。実約定は証拠なしに記録しません。

## 5. 失敗と保存

例外、途中中断、一次資料欠損、ready未達、競合、push失敗は成功に変換しません。
可能な範囲で同一tokenの失敗/中断証跡、未完了task、次回handoffをprivateへ記録します。
既存CLIのhelpで対応する失敗・再開操作を確認し、未知のflagやstateの手編集でlockを解放しません。
検証エラーをreport-onlyにして夜間runを合格させません。

成功・失敗のstate、ledger、run成果物、採用public SHAを一つのprivate commitに揃えます。
diffを読みcredential、raw市場データ・manifest、一時download、巨大cache、不要な個人情報がないことを確認します。
private Gitの承認済み保存経路で保存し、remoteの実際のSHAを確認するまで「保存完了」と言いません。
保護branchへは通常reviewを用い、mainへのforce pushや競合の片側採用をしません。
結果がPRで保留なら「保存済み・未採用」と明示し、canonical mainへの反映前に次のrunを重ねません。

## 6. 報告とガードレール

報告は評価時刻、成功/WAIT/失敗、調査範囲、data gap、次営業日のaction、private保存結果、
次のreview/判断を短くまとめます。保有・金額・credentialを公開先へ出しません。

GitHub Actionsは既存入力のPR検証とprivate healthのみを実施します。
Actions healthは保存済みnightly state/履歴の鮮度、policy、HUMAN_ONLY、public pointer差分を監視し、
旧収集・同期workflowのrun履歴を稼働条件にしません。検証中の暗黙migrationは隔離copyで検出し、正本へ採用しません。
隔離copyは設定済みprivate archiveだけを一時利用し、public archiveや他のraw領域を複製しません。
healthのActions jobは`--persisted-only`で記録のみを監視し、実データreadinessはNOT_EVALUATEDとします。
required formatはstate/policyと合成smokeだけを検証します。実行環境の通常healthは引き続きprivate archive必須です。
監視のOKは実際の情報収集・run・remote保存の代わりになりません。

[Cloudflare退役](cloudflare-scheduler.md)と両repoのlegacy writer廃止、既存auto-merge待ち処理の確認、
定期runnerの有効化・初回実行確認が揃って移行完了です。未設定・未確認の項目はそのままblockerとして示します。

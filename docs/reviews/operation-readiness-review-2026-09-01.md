# 運用 readiness review — 2026-09-01

## 判定

| mode | 判定 | 理由 |
| --- | --- | --- |
| 開発・回帰test | GO | public code、公開fixture、20日off-line smokeをCIで検証できる |
| PAPER開始 | GO（警告付き） | private状態はschema 2.1でvalid、2026-08-31 dataを検証し、`paper_go: true`を実測 |
| LIVE実資金 | NO-GO | 全LIVE gate、v0.4 promotion、本人approvalが未完了 |

証券会社への通信は実装せず、`broker_submission` は `HUMAN_ONLY` を維持する。PAPER ticket は `PAPER_PROPOSED` であり、実注文しない。

## この変更で解消する設計上の問題

- public を submodule、private を実運用rootとして固定した
- 実運用dataを親privateの `operations/private/` へ集約した
- public 文書を architecture / operations / rules / research に分類した
- 個別のreadiness reviewをpublicからprivateへ移した
- `age`、encrypted archive、GitHub Release backup、31日backup gateを廃止した
- LIVE gateを `private_repository_recovery` へ置換した
- 旧state/policyのbackup項目はmigration時に除去・変換する
- clean-clone復旧、private Git同期、競合解消を新しい運用手順にした

## public/private 境界のreview

public に残すのは、公開情報だけで再現できるcode、test、template、rule、research、market dataである。次はprivateへ置く。

- state、policy、portfolio/trade/capital ledger
- 現在のwatchlist、注文候補、約定、判断、handoff
- 実資金、税、口座固有制約
- GO/NO-GO review、本人approval、recovery evidence

credential、password、token、cookie、MFA recovery codeはprivateにも置かない。

## 現在未完了のLIVE gate

1. point-in-time全母集団の入口・出口検証
2. 2025–2026履歴再生の受入
3. v0.4で最低12か月の前向きPAPER
4. 実data20営業日連続の重大取得漏れ・重複注文0件
5. 必須公式情報源coverage
6. private repository/mirrorのclean-clone復旧
7. 本人の損失許容・税・broker仕様・security確認
8. v0.4 holdout promotion
9. 全gate後の本人による明示approval

off-line 20日smoke、価格だけの過去data、CI成功は3・4・9を代替しない。

## 変更の検証

- public unit/integration test: 122件成功
- public Python compile: 成功
- off-line operation smoke: 20営業日成功、broker submission 0件
- private Markdown lint: 0件
- local Markdown link: public/privateとも全件解決
- secret pattern scan: 候補0件
- public PR #20のsquash merge commit `901f05e`をsubmoduleとして取得
- clean cloneでsetup、state/ledger validate、PAPER readinessを再現

このclean-cloneはPR候補branchと空の初期台帳に対する統合試験である。merge後の`main`、実runを蓄積した台帳、access-controlled mirrorから再度復旧して照合するまで、`private_repository_recovery` gateは`false`のままとする。

## PAPER readiness 実測

2026-09-01 21:45 JSTを基準に、private rootからmigrate、validate、bootstrap checkを実行した。

- state schema: `2.1`
- policy schema: `1.2`
- `paper_go: true`
- `live_go: false`
- active target: 0件
- 2026-08-31 Yahoo archive: 3,669 / 3,713、coverage 98.81%、checksum一致
- broker submission: `HUMAN_ONLY`
- EDINET API key: 未設定。PAPERでは手動一次資料fallbackが必須

active target 0件のため、最初は `GLOBAL / NO-ACTION` と全市場候補reviewに限定する。Yahooは二次情報であり、個別判断前にEDINET、TDnet、会社IR、JPXの一次情報を確認する。

## PAPERを開始する次の作業

1. private PRをmergeし、private mainからclean cloneして復旧性を確認する
2. 実保有がある場合だけ正確な値をprivate ledgerへ登録する
3. 最新public data PRを毎回reviewして採用する
4. 最初の `GLOBAL / NO-ACTION` runと全市場候補reviewを実施する
5. 一次資料を確認できた候補だけをactive watchlistへ登録する
6. 実data20営業日の連続runを開始し、incidentと手動fallbackを記録する

## review時の停止条件

次のいずれかがあれば、PAPER/LIVEとも新しい注文候補を作らない。

- state/ledger validate失敗
- 未照合・重複注文
- 必須一次情報源または価格の不一致
- 古いpublic data、checksum/coverage/freshness不合格
- private Gitの未push runまたは競合
- credential漏えい、不審login、broker障害

詳細なgate定義は[GO/NO-GO基準](../live-readiness.md)、実行手順は[private運用手順](../operations/private-operation-runbook.md)を正本とする。

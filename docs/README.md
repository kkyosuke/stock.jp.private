# private ドキュメント索引

## 正本

- [アシスタント日次実行契約](operations/assistant-daily-operation.md): 収集・調査・PAPER実行・private保存・停止条件
- [リポジトリ全体構成](repository-layout.md): public/private の境界と配置
- [市場データの非公開保存・移行手順](operations/private-market-data-storage.md): Git外のraw archive、移行前提、欠損時の停止
- [private 運用手順](operations/private-operation-runbook.md): setup、日次、競合、復旧
- [運用開始後の詳細スケジュール](operations/post-launch-operation-schedule.md): 日次フロー、人の作業、定期review、LIVE移行
- [旧submodule同期の廃止](operations/automatic-submodule-update.md): 単一repoでの通常review
- [LIVE GO/NO-GO 基準](live-readiness.md): gate、evidence、停止条件
- [2026-09-01 readiness review](reviews/operation-readiness-review-2026-09-01.md): 現在地と次の作業

汎用的な戦略ルール、コード仕様、研究は [共通ドキュメント](../stock.jp/docs/README.md)を正本とします。private 側には、個人・口座・現在の対象・実運用判断に依存する内容だけを置きます。

日次の詳細な report、decision、handoff、台帳は `operations/private/` に保存します。`docs/` に複製して二重の正本を作りません。

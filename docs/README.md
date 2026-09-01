# private ドキュメント索引

## 正本

- [リポジトリ全体構成](repository-layout.md): public/private の境界と配置
- [private 運用手順](operations/private-operation-runbook.md): setup、日次、競合、復旧
- [public submodule自動更新](operations/automatic-submodule-update.md): 毎朝のpointer PRとauto-merge
- [LIVE GO/NO-GO 基準](live-readiness.md): gate、evidence、停止条件
- [2026-09-01 readiness review](reviews/operation-readiness-review-2026-09-01.md): 現在地と次の作業

汎用的な戦略ルール、コード仕様、研究は [public ドキュメント](../stock.jp/docs/README.md)を正本とします。private 側には、個人・口座・現在の対象・実運用判断に依存する内容だけを置きます。

日次の詳細な report、decision、handoff、台帳は `operations/private/` に保存します。`docs/` に複製して二重の正本を作りません。

# ドキュメント索引

## 最初に読むもの

1. [リポジトリ境界](architecture/repository-boundaries.md)
2. [運用ガバナンス](operations/operation-governance-v0.1.md)
3. [運用状態・台帳](operations/operation-state-v0.2.md)
4. [PAPER/LIVE 準備判定](operations/operation-readiness-v0.1.md)
5. [少額限定実運用 LIMITED_LIVE](operations/limited-live-v0.1.md)
6. [LIVE 運用手順](operations/live-operation-playbook-v0.1.md)
7. [LIVE gate 証跡の機械判定](operations/live-gate-evidence.md)
8. [判断計算の自動化](operations/decision-assessment-automation-v0.1.md)

## 分類

- `architecture/`: public/private の責務、データ境界、復旧方針
- `operations/`: 日次実行、公式情報源、状態遷移、障害時の扱い
- `rules/`: 変更管理された売買ルール
- `research/`: 過去データ検証、制約、再生成手順

実資金投入の可否は研究成績だけでは決めません。`operation_bootstrap.py check` の全ゲート、最新PAPER runの公式情報源coverage、現行 private 構成の clean-clone 確認、利用者の明示承認をすべて満たした場合だけ GO とします。PAPERの経過日数と連続run数には最低条件を設けません。

## J-Quantsデータ取得

- [全契約対象データの履歴・差分保存](jquants-private-ingestion.md)

## 現在の保存境界

stock.jpは非公開の共通コード・市場データrepo、stock.jp.privateは個人分析・状態・台帳repoです。
[現行の境界](architecture/repository-boundaries.md)と
[private Git保存手順](private-git-market-data.md)を優先します。過去の研究・監査記録にあるpublicや
local-onlyの記述は当時の履歴で、現在の公開範囲や保存先を示しません。履歴自体は改変しません。

- [日次PR・native auto-mergeの対象と検証手順](daily-pr-publication.md)

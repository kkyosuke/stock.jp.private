# stock.jp.private

`kkyosuke/stock.jp` の個人運用状態を、`age` で暗号化して遠隔保存するための
Private リポジトリです。このリポジトリは処理系ではなく、暗号化済みバックアップの
受動的な保管庫です。

## 責務の分離

| 場所 | 正本にするもの | 実行するもの |
|---|---|---|
| [`kkyosuke/stock.jp`](https://github.com/kkyosuke/stock.jp) (Public) | ルール、仕様、プログラム、テスト、テンプレート、公開可能な市場データ、再現可能な集計結果 | CI、公開情報の収集、検証、バックテスト、定期更新 PR の作成 |
| 公開側 checkout の `operations/private/` (Git 管理外) | 保有数量、取得原価、資金、税、注文・約定、判断、日次実行状態 | 個人状態を使う判定、注文案の作成、バックアップの暗号化・復元検証 |
| `kkyosuke/stock.jp.private` (Private) | `age` 暗号化済みバックアップの GitHub Release | 原則として GitHub Actions を動かさない |
| リポジトリ外の秘密保管場所 | `age` 秘密鍵、証券会社や外部サービスの認証情報 | 鍵の保管と復旧 |

private リポジトリへ運用プログラムや市場データを複製しません。公開可能な処理を
public 側へ集約すると、[標準 GitHub-hosted runner の Actions minutes を消費せず](https://docs.github.com/en/billing/concepts/product-billing/github-actions#how-use-of-github-actions-is-measured)
定期収集や重い検証を実行でき、コードと結果の再現性も公開側だけで確認できます。

これは実行時間や同時実行数まで無制限になるという意味ではありません。GitHub
Actions のジョブ・同時実行・ストレージ等の上限には従い、larger runner は使わない
前提です。

## ドキュメントの置き場所

public 側を、機密情報なしで説明できるすべての仕様と手順の正本にします。

- アーキテクチャとリポジトリ間の責務
- GitHub Actions の workflow、障害対応、再実行手順
- データ形式、判定ルール、テスト方法、バックテスト条件
- バックアップの作成・検証・復元コマンド
- 機密情報を含まないテンプレートとサンプル

private 側の Git 履歴には、この README だけを置きます。ここには次の境界だけを
記載し、public 側の手順を複製しません。

- 保管できる Release asset の形式
- 保存禁止物
- public 側の正本文書への入口
- private リポジトリ固有の可視性・保持ルール

実際の保有、注文、判断、実行結果を説明する文書やログは、どちらのリポジトリにも
commit せず、ローカルの `operations/private/` だけに保存します。

詳細な構成と作成・検証・復元手順の正本は、public 側の
[`docs/repository-roles-and-private-backup-v0.1.md`](https://github.com/kkyosuke/stock.jp/blob/main/docs/repository-roles-and-private-backup-v0.1.md)
です。GitHub Actions の個別運用は public 側の各 runbook を正本とします。

## このリポジトリに保存するもの

GitHub Releases に、次の形式の暗号化済みバックアップだけを添付します。

```text
operation-YYYYMMDDTHHMMSS+0900.zip.age
```

バックアップ本体は Git へ commit しません。Release asset は不変として扱い、同じ
日時の Release を再作成したり既存 asset を上書きしたりしません。

## 保存してはいけないもの

- 暗号化前の `.zip`
- 展開済みの `operations/private/`
- `age` 秘密鍵
- API キー、口座番号、パスワード、認証情報
- 個別の保有・注文・約定・税・判断・実行ログ
- 公開側のソースコード、ドキュメント、市場データの複製
- Actions の artifact、cache、log に個人状態を含めたもの

秘密鍵はこのリポジトリ、GitHub Actions Secrets、チャットのいずれにも保存しません。
リポジトリの Visibility は常に `Private` とします。

## 処理の流れ

```text
public Actions
  公開情報を収集・検証 → public の更新 PR
                              ↓ review / merge / pull
local operations/private/
  個人状態を読み込む → 判定・記録 → age で暗号化・ローカル復元検証
                                      ↓ 暗号化済み asset のみ upload
private GitHub Release
  遠隔コピーを保管 → 再 download・ローカル復元検証
```

public Actions から private リポジトリや `operations/private/` を読みません。private
Actions に秘密鍵を登録してバックアップを自動復号・自動作成することもしません。
この境界により、public の無料計算資源を活用しながら、個人状態が PR、artifact、cache、
ログへ混入する経路を閉じます。

## public Actions に寄せる処理

次の条件をすべて満たす処理は public 側へ置きます。

1. 入力が公開情報、公開リポジトリ内のデータ、または合成 fixture だけである
2. 実行ログと出力が世界中へ公開されても問題ない
3. 特定個人の保有、資金、注文、判断履歴を知らなくても完結する
4. データ提供元の利用条件が取得、保存、再配布を許容する
5. 同じ commit と入力から結果を再生成できる

該当する代表的な処理は次のとおりです。

- 上場銘柄 universe、株価、法定・適時開示など公開情報の収集と品質検査
- 公開市場全体の指標、特徴量、候補ランキング、ルール別シグナルの計算
- 過去データ replay、バックテスト、パラメータ比較、回帰テスト
- schema、checksum、coverage、鮮度、欠損、異常値の検査
- ドキュメント、lint、unit/integration test、依存関係検査
- 公開データと再現可能なレポートを更新する PR の作成

候補ランキングやシグナルは「市場に対する一般的な計算結果」に留めます。実際に保有して
いる銘柄だけを対象にした出力、資金量を反映した数量、発注可否は public へ出しません。

Actions workflow は標準 runner を使い、最小権限の `GITHUB_TOKEN` で更新 PR を作成します。
定期実行は取得・差分更新、`workflow_dispatch` は履歴再構築に分け、同一処理の多重実行を
`concurrency` で抑止します。結果は artifact を正本にせず、検証情報を伴う PR として review
してから `main` へ取り込みます。cache は削除されても再生成できるものだけに使います。

## public と private の受け渡し契約

public 側の各データ更新は、少なくとも次の情報を機密性のない manifest に持たせます。

- 対象期間と情報の取得時刻
- 対象 universe と件数
- 成功・欠損・失敗件数および coverage
- データファイルの checksum
- schema version と生成コードの commit
- 利用した公開情報源と、再配布可否を含む注意事項

ローカル運用は `main` の特定 commit と manifest を入力として固定し、checksum、coverage、
鮮度、schema を検証してから個人状態と結合します。不足や不一致があれば、古い結果を最新と
みなしたり数量を推測したりせず停止します。private な出力から public 側へ戻せるのは、個人
状態を含まない一般化されたバグ修正、合成 fixture、テスト、ルール改善だけです。

## private に留める処理

次のいずれかを入力または出力に含む処理は GitHub Actions へ移さず、信頼するローカル環境で
実行します。

- 現在または過去の保有、取得原価、現金、税、損益
- 実際の注文、約定、証券会社 ID、個人別の売買候補
- 個人のリスク許容度やポートフォリオ制約を反映した数量
- `operations/private/` の日次状態、判断、引き継ぎ、実行ログ
- 秘密鍵や外部サービスの認証情報を必要とする処理

公開情報の収集は public Actions に一度だけ行わせ、ローカル側は検証済み public snapshot を
再利用します。これにより、無料の計算資源を最大限使いつつ、同じデータの再取得、private
minutes の消費、個人情報がネットワークやログへ漏れる面積を減らします。

## 保管と復旧の原則

- upload 前に public 側のツールで暗号化とローカル復元検証を完了する
- upload 後に別パスへ再 download し、SHA-256 と復元可能性を確認する
- 稼働中の `operations/private/` へ直接復元せず、検証用ディレクトリで比較する
- 最新の検証済み Release を、次の復元確認が完了する前に削除しない
- 秘密鍵は両リポジトリ外で保管し、別媒体にも復旧可能な複製を持つ

具体的なコマンドは public 側の正本文書を参照してください。

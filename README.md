# stock.jp.private

`kkyosuke/stock.jp` の個人運用データを管理する Private リポジトリです。

このリポジトリ自体が Private であるため、運用データは `age` で暗号化せず、通常の
Git ファイルとして保存します。新しい暗号化 archive やバックアップ用 GitHub Release
は作成しません。

## 責務の分離

| 場所 | 正本にするもの | 主な処理 |
| --- | --- | --- |
| [`kkyosuke/stock.jp`](https://github.com/kkyosuke/stock.jp) (Public) | ルール、仕様、プログラム、テスト、テンプレート、公開可能な市場データ、再現可能な集計結果 | CI、公開情報の収集、検証、バックテスト、定期更新 PR の作成 |
| `stock.jp.private` (Private) | 保有数量、取得原価、資金、税、注文・約定、判断、日次実行状態 | 個人状態を使う判定、注文案の確認、運用履歴の保存 |
| リポジトリ外の秘密保管場所 | パスワード、API キー、証券会社や外部サービスの認証情報 | 認証情報の保管と更新 |

公開可能で再現可能な計算は public 側へ集約します。public リポジトリでは、標準
GitHub-hosted runner の Actions minutes を消費せずに定期収集や検証を実行できます。
ただし、ジョブの実行時間、同時実行数、ストレージなどの上限は残るため、完全に無制限
という意味ではありません。larger runner は使わない前提です。

private 側では、public の結果へ個人状態を適用する処理と、その結果の保存だけを行います。
private の運用データを public Actions、public PR、artifact、cache、log へ渡しません。
private 側で Actions を追加する場合も Markdown lint などリポジトリ管理だけに限定し、
`operations/private/` を処理、出力、artifact 化しません。

## リポジトリ構成

```text
stock.jp.private/
├── README.md
├── stock.jp/                 # public リポジトリの submodule
└── operations/
    └── private/              # 平文の個人運用データ。private Git で追跡
```

public 側のスクリプトは `stock.jp/operations/private/` を参照するため、submodule 内には
private repo 側のディレクトリを指すシンボリックリンクを作ります。

```text
stock.jp/operations/private -> ../../operations/private
```

submodule 内へ個人運用データの実体を置きません。submodule は public 側の Git 履歴しか
保持できないため、個人データの実体は必ず private repo 直下へ置きます。

## 初回セットアップ

submodule を含めて clone します。

```bash
git clone --recurse-submodules \
  git@github.com:kkyosuke/stock.jp.private.git
cd stock.jp.private
```

private データ用ディレクトリと、public スクリプトから参照するリンクを作ります。

```bash
mkdir -p operations/private
chmod -R go-rwx operations/private
ln -s ../../operations/private stock.jp/operations/private
```

`stock.jp/operations/private` に既存のディレクトリやリンクがある場合は上書きしません。
内容とリンク先を確認してから移行してください。Git は通常ファイルの `0600` やディレクトリの
`0700` を保存しないため、clone 後は上記の `chmod` を必ず実行します。public 側の運用
スクリプトも、初期化時に同じ権限へ補正します。

## 日常運用

1. private の `main` を `git pull --ff-only` し、未反映の変更がないことを確認する
2. public 側 Actions が作成したデータ PR を review し、public の `main` へ取り込む
3. submodule の `stock.jp/` を検証済みの public commit へ更新する
4. public 側のスクリプトを使い、private データを読み書きする
5. 個人運用データと submodule commit の変更を review する
6. 認証情報が含まれていないことを確認し、1回の運用を1 commitとしてpushする

submodule は detached HEAD で使用します。public の更新を取り込む場合は、review 済みの
`origin/main` を取得してから、その commit へ明示的に移動します。

```bash
git -C stock.jp fetch origin main
git -C stock.jp switch --detach origin/main
git add stock.jp
git diff --cached --submodule=log -- stock.jp
```

運用コマンドは private repo のルートから、submodule 内のスクリプトを指定して実行します。
スクリプトはシンボリックリンクを通して private repo のデータを更新します。

```bash
stock.jp/.venv/bin/python stock.jp/scripts/operation_state.py validate
stock.jp/.venv/bin/python stock.jp/scripts/nightly_operation.py status
```

実行後は、意図した private データだけが変更されたことを確認します。

```bash
git status --short
git diff
git diff --cached
```

成功、失敗、手動中断のいずれも、その run の状態と submodule commit を同じ private commit
へ含めます。push が完了する前に別端末で次の run を開始しません。競合時は台帳を機械的に
上書きせず、双方の run ID、約定、state revision を照合してから解消します。

## public Actions に寄せる処理

次の条件をすべて満たす処理は public 側へ置きます。

1. 入力が公開情報、public リポジトリ内のデータ、または合成 fixture だけである
2. 実行ログと出力が世界中へ公開されても問題ない
3. 特定個人の保有、資金、注文、判断履歴を知らなくても完結する
4. データ提供元の利用条件が取得、保存、再配布を許容する
5. 同じ commit と入力から結果を再生成できる

対象になる処理は次のとおりです。

- 上場銘柄 universe、株価、法定・適時開示など公開情報の収集と品質検査
- 公開市場全体の指標、特徴量、候補ランキング、ルール別シグナルの計算
- 過去データ replay、バックテスト、パラメータ比較、回帰テスト
- schema、checksum、coverage、鮮度、欠損、異常値の検査
- ドキュメント、lint、unit/integration test、依存関係検査
- 公開データと再現可能なレポートを更新する PR の作成

候補ランキングやシグナルは、市場に対する一般的な計算結果に留めます。実際に保有して
いる銘柄だけを対象にした出力、資金量を反映した数量、個人別の発注可否は公開しません。

Actions workflow は標準 runner と最小権限の `GITHUB_TOKEN` を使います。定期実行は
取得・差分更新、`workflow_dispatch` は履歴再構築に分け、多重実行を `concurrency` で
抑止します。結果は artifact を正本にせず、検証情報を伴う PR として review してから
`main` へ取り込みます。cache は削除されても再生成できるものだけに使います。

## public と private の受け渡し

public 側のデータ更新は、次の情報を機密性のない manifest に持たせます。

- 対象期間と取得時刻
- 対象 universe と件数
- 成功・欠損・失敗件数および coverage
- データファイルの checksum
- schema version と生成コードの commit
- 利用した公開情報源と再配布条件

private 運用は submodule の特定 commit と manifest を入力として固定し、checksum、coverage、
鮮度、schema を検証してから個人状態と結合します。不足や不一致があれば、古い結果を最新と
みなしたり数量を推測したりせず停止します。

private から public へ戻せるのは、個人状態を含まない一般化されたバグ修正、合成 fixture、
テスト、ルール改善だけです。

## private に保存するもの

- `operations/private/state.json` と運用 policy
- 保有、資金、税、取引、回収原資などの台帳
- 注文候補、注文、約定、取消、未照合注文
- watchlist、再購入禁止、レビュー期限
- 日次 report、判断、根拠、coverage、handoff、run history
- private な取得結果と作業用 queue

これらは平文のまま通常の Git commit に含めます。復旧は private repo の clone と Git 履歴で
行い、バックアップ用 Release、`.zip`、`.zip.age`、`age` の鍵は使用しません。

再生成できる cache、public データの複製、巨大なbinaryや一時ファイルはcommitしません。
GitHubの通常ファイルサイズに収まらない成果物が必要になった場合は、無条件にGit LFSへ
移すのではなく、保存期間と復旧要件を決めてから別のprivate storageを設計します。

## 保存してはいけないもの

Private リポジトリでも、次の情報は Git へ保存しません。

- パスワード、API キー、アクセストークン、秘密鍵
- 証券会社や銀行のログイン情報
- session cookie、ワンタイムパスワード、回復コード
- 不要な本人確認書類や法的文書の写し
- public submodule と重複するソースコードや公開データ

Private の可視性はアクセス制御であり、commitした情報は履歴へ残ります。認証情報を誤って
commitした場合は、削除commitだけで済ませず、直ちに失効・再発行して履歴から除去します。
リポジトリの Visibility は常に `Private` とし、collaborator と連携アプリの権限を最小限に
保ちます。

## 暗号化バックアップからの移行

- public 側の `operation_backup.py`、バックアップ鮮度 gate、旧 state 項目を廃止する
- public 側のテストとドキュメントを平文 Git 運用へ更新してから submodule を進める
- 新しい暗号化 archive とバックアップ用 Release は作成しない
- 既存 archive が必要なら、安全なローカル環境で一度だけ復元する
- 復元した内容を検証して `operations/private/` へ移し、通常の Git commit として保存する
- private repo の clone と履歴から復旧できることを確認する
- 既存 Release を削除する場合は、移行後のデータが揃っていることを先に確認する

旧 archive の復号手順は新しい日常運用へ持ち込みません。移行完了後は、この README に
記載した平文 Git 運用だけを正本とします。

現在の submodule が旧バックアップの存在を readiness 条件として要求する場合、その項目へ
架空の日時や checksum を記録して通過させてはいけません。先に public 側の gate とテストを
正規に変更し、その commit へ submodule を更新してから新運用を開始します。

## ドキュメントの置き場所

public 側には、機密情報なしで説明できるすべての仕様と手順を置きます。

- アーキテクチャ、データ形式、判定ルール
- GitHub Actions の workflow、障害対応、再実行手順
- テスト方法、バックテスト条件、公開可能な結果
- 機密情報を含まないテンプレートとサンプル

private 側の一般ドキュメントは、この README に集約します。個人の判断や実行内容は
`operations/private/` の report、decision、handoff として保存し、一般仕様と混在させません。
public 側に残っている暗号化バックアップの説明は旧運用として扱い、この README の方針へ
追随して別途更新します。

## Markdown lint

Node.js 22 以上を用意し、依存関係と pre-commit hook をインストールします。

```bash
npm install
npm run hooks:install
```

pre-commit では、stage 済みの Markdown ファイルだけを `markdownlint-cli2` で検査します。
リポジトリ内の Markdown ファイルをまとめて検査する場合は、次を実行します。

```bash
npm run lint:markdown
```

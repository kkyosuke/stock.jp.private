# 単一privateリポジトリの構成

正本は `kkyosuke/stock.jp.private` だけです。`stock.jp/` は通常フォルダで、
submodule・別repoのcheckout・AUTOMATION_PATは不要です。元の `kkyosuke/stock.jp` と
その履歴は保持しますが、新しい運用から参照・更新しません。

| パス（repository root基準） | 責務 |
| --- | --- |
| `stock.jp/scripts/`, `stock.jp/tests/` | 共通コードと合成テスト |
| `stock.jp/data/` | 移行済みの既存市場・研究データ |
| `stock.jp/data/jquants-archive/` | 許可された将来の圧縮市場データ世代 |
| `operations/private/` | 個人の分析・選定・状態・台帳・判断・run |
| `operations/private/market-data/` | Git除外の取得・復元作業領域 |
| `.github/workflows/` | 単一checkoutの検証とread-only監視 |

`stock.jp/operations/private -> ../../operations/private` はsetupで作るGit除外の互換linkです。
コードの既存root・importを維持し、個人ファイルを共通フォルダへ二重保存しません。
個人向けCLIは従来どおり `stock.jp/scripts/` を実行します。日次PR validatorと
`market_data_git.py stage` の `--root` はrepository rootを指定します。
市場データと個人分析は同じrepoでも別のallowlistと日次branchで管理します。

## 起動と検証

通常のclone一回でコードと追跡済みデータが揃います。旧submodule checkoutを持つ端末では
未保存ファイルを保全し、新しいディレクトリへcloneしてください。setupは残存する
`stock.jp/.git` を拒否し、自動削除・別repoのfetchを行いません。

```sh
git clone git@github.com:kkyosuke/stock.jp.private.git
cd stock.jp.private
./scripts/setup-workspace.sh
python scripts/validate_checkout.py
```

Python依存は `stock.jp/pyproject.toml` と運用手順に従います。通常の更新はclean確認後に
このrepoだけを `git pull --ff-only` します。共通コードの採用pointer PRは不要です。
取得・復元済みarchiveのcoverage・鮮度・checksumとPAPER readinessは別途必須です。
旧 `stock.jp/data/daily-prices/` へのruntime fallbackは許可しません。
EDINET API keyがないPAPERは公式一次資料の手動fallbackを許しますが、未確認判断はWAITです。

CIは標準checkout tokenで一度だけ取得し、圧縮市場archiveと旧日足を作業ツリーから除外します。
合成テスト、保存済み状態、既存研究結果の整合性を検証し、新規データ取得・復元・uploadはしません。
`stock.jp/.github/` は移行元ファイルとして保持した無効なコピーで、実行するworkflowはrootだけです。
既存secretの値・権限・設定は変更しません。

## 移行と運用境界

移行元はcommit `91682b33004c06cb9a3ab5c63c3da595503d0f90` の691ファイルです。
`docs/migrations/common-source-inventory.json` に全パス・mode・blob SHA・sizeを記録します。
既存527データファイルは価格履歴・派生研究・説明とmanifestです。提供元の記述は元のREADMEを
保全し、取得済みデータを新しい公式検証や運用可能性の証明とみなしません。
新たに取得したJ-Quants sample・checkpoint・圧縮世代はこの移行に含めません。
拒否済みuploadは再試行せず、別の保存先へ迂回しません。

PAPER/HUMAN_ONLY、risk、source、coverageのgateを維持します。復旧layout revisionは4です。
旧revisionの成功証跡を編集して合格へ流用せず、新しいclean cloneの確認が必要です。
復旧証跡はprivate commitと `git rev-parse HEAD:stock.jp` のcode tree SHAを照合します。
新構成のkeyは `source_code_tree` / `recovered_code_tree`、
`clean_clone_single_repository`、最終承認は `code_tree_reviewed` です。
市場・個人日次PRの両方で `format`・`test`・`guardrail` のexact-head成功が必要です。
コード・設定・本移行は通常reviewのdraft PRで、auto-merge対象ではありません。

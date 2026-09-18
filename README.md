# SeaSearcher scraping

SeaSearcher の Movement、AIS Positions、Vessels を取得するためのプロジェクトです。
普段編集するのは `notebooks/` のノートブックだけで、スクレイピングの実装は `tools/` にまとめています。

## フォルダ構成

```text
.
├── notebooks/                 # 普段使う実行用ノートブック
│   ├── movement_account_1.ipynb
│   ├── movement_account_2.ipynb
│   ├── ais_positions_account_1.ipynb
│   ├── ais_positions_account_2.ipynb
│   ├── vessels.ipynb
├── tools/
│   ├── scrapers/              # スクレイピングの公開入口と実装
│   └── coordinator/           # 共有アカウントの排他制御
├── tests/                     # 自動テスト
├── vessel/                    # 入力用の船舶CSV
├── apps_script/               # Google Sheets + Apps Script
├── docs/                      # 補足資料
├── pyproject.toml             # Pythonの対応範囲・直接の依存関係
├── uv.lock                    # PC間で共有する解決済みバージョン
└── .python-version            # 標準で使うPython（3.13系）
```

## 使うノートブック

| ノートブック | 用途 |
| --- | --- |
| `notebooks/movement_account_1.ipynb` | Movement取得。アカウント1、入力順 |
| `notebooks/movement_account_2.ipynb` | Movement取得。アカウント2、逆順 |
| `notebooks/ais_positions_account_1.ipynb` | AIS Positions取得。アカウント1、入力順 |
| `notebooks/ais_positions_account_2.ipynb` | AIS Positions取得。アカウント2、逆順 |
| `notebooks/vessels.ipynb` | Vessels一覧CSVの取得 |

MovementとAIS Positionsは、2つのSeaSearcherアカウントを並行利用するために2冊ずつ残しています。

ノートブックはプロジェクトのルートから実行できるように自動でパスを設定します。
VS Codeでは、このフォルダ（`seasearcher-main`）をワークスペースとして開いてください。

## セットアップ

環境管理には [uv](https://docs.astral.sh/uv/) を使います。
Windowsでは、初回だけPowerShellでインストールします。

```powershell
winget install --id=astral-sh.uv -e
```

ターミナルを開き直し、プロジェクトのルートで実行してください。

```powershell
uv sync --locked
```

必要なPython 3.13系と `.venv`、ノートブック用カーネルを含む依存関係が揃います。
Pythonのパッチ番号は固定せず、依存パッケージは `uv.lock` のバージョンで再現します。
VS CodeにPython・Jupyter拡張機能を入れ、ノートブック右上のカーネル選択で
**Python環境 → このプロジェクトの `.venv`** を選んでください。
仮想環境の手動activateは不要です。

Google Chromeは各PCに別途インストールしてください。
初回セットアップとChromeDriver取得にはインターネット接続が必要です。
Python 3.10以上を許容し、標準は3.13系としています。
macOS/Linux、別のPython、JupyterLab、pipでの利用や更新方法は
[`docs/environment.md`](docs/environment.md) を参照してください。

## ログイン情報

認証情報はソースコードやノートブックに書かず、`.env` または環境変数で指定します。

プロジェクトのルートで `.env.example` をコピーし、`.env` に実際の値を設定してください。
`.env` はGitに追加しません。

```powershell
Copy-Item .env.example .env
```

MovementとAIS Positionsは、ノートブックの番号に対応するアカウントを設定します。

```powershell
$env:SEASEARCHER_LOGIN_USER_1 = "notebook-1-user@example.com"
$env:SEASEARCHER_LOGIN_PASSWORD_1 = "notebook-1-password"
$env:SEASEARCHER_LOGIN_USER_2 = "notebook-2-user@example.com"
$env:SEASEARCHER_LOGIN_PASSWORD_2 = "notebook-2-password"
```

Vesselsでは、必要に応じて次を設定します。

```powershell
$env:SEASEARCHER_LOGIN_USER = "shared-user@example.com"
$env:SEASEARCHER_LOGIN_PASSWORD = "shared-password"
```

## 共有アカウント coordinator

複数PCで同じSeaSearcherアカウントを同時利用しないよう、Google Sheets + Apps Scriptのcoordinatorを使います。
設定方法は [`apps_script/README.md`](apps_script/README.md) を参照してください。

状態確認は次のコマンドです。

```powershell
uv run --locked python -m tools.coordinator status
```

緊急時以外は `SCRAPE_COORDINATOR_BYPASS=1` を使用しないでください。

## テスト

```powershell
uv run --locked python -m unittest discover -s tests -t .
```

取得結果、ログ、デバッグファイル、`.env`、PythonキャッシュはGit管理対象外です。
詳細は [`docs/workspace-overview.md`](docs/workspace-overview.md) と [`docs/ais_positions.md`](docs/ais_positions.md) を参照してください。

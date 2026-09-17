# SeaSearcher scraping

SeaSearcher の Movement、AIS Positions、Vessels 一覧を取得するためのノートブックと共通 utility です。

## Setup

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt

Chrome がインストールされている必要があります。

## Login

認証情報はソースコードやノートブックに書かず、環境変数で指定してください。
`.env.example` を `.env` にコピーして値を設定する方法と、PowerShellで直接設定する方法があります。
Movementの2つのノートブックは別アカウントを使用します。

    Copy-Item .env.example .env
    # .env を編集して実際の値を入力

    # scraping_seasearcher.ipynb（ノート1）
    $env:SEASEARCHER_LOGIN_USER_1 = "notebook-1-user@example.com"
    $env:SEASEARCHER_LOGIN_PASSWORD_1 = "notebook-1-password"

    # scraping_seasearcher_2.ipynb（ノート2）
    $env:SEASEARCHER_LOGIN_USER_2 = "notebook-2-user@example.com"
    $env:SEASEARCHER_LOGIN_PASSWORD_2 = "notebook-2-password"

    # AIS Positions・Vesselsノートブック、utility直接利用時（必要な場合）
    $env:SEASEARCHER_LOGIN_USER = "shared-user@example.com"
    $env:SEASEARCHER_LOGIN_PASSWORD = "shared-password"

## Notebooks

- scraping_seasearcher.ipynb, scraping_seasearcher_2.ipynb: Movement
- scraping_seasearcher_ais_positions.ipynb, scraping_seasearcher_ais_positions_2.ipynb: AIS Positions
- scraping_seasearcher_vessels.ipynb: Vessel CSV の取得

入力用の vessel CSV は vessel/ に置いてあり、リポジトリに含めます。Movement/AIS/LNGの取得結果、ログ、デバッグファイル、ローカル環境、`.env` は `.gitignore` で除外しています。

## Shared account coordinator

複数人が同じ SeaSearcher アカウントを同時利用しないように、Google Sheets + Apps Script の共有 coordinator を利用します。通常のスクレイピング実行は coordinator が未設定だと開始しません。

- `account_1` と `account_2` はそれぞれ同時に1ジョブだけ実行できます。
- 使用中なら後続ジョブは FIFO の待ち行列に入り、自動的に待機します。
- `Accounts` Sheet で実行者、処理種別、進捗、ETA、heartbeat を確認できます。
- PC の異常終了後は lease timeout によりロックが自動失効します。
- SeaSearcher のパスワードは coordinator には保存しません。

初期構築は `apps_script/README.md` を参照してください。各PCでは `.env.example` の `SCRAPE_COORDINATOR_URL`、`SCRAPE_COORDINATOR_TOKEN`、`SCRAPE_OPERATOR` を設定します。

既存 notebook のコードを変えずに、公開 utility (`utils_scraping_*.py`) の薄い facade が coordinator を自動適用します。実際の Selenium 実装は `_utils_scraping_*_impl.py` に保持されています。

- Movement の notebook 1 / 2 は `SEASEARCHER_LOGIN_USER_1` / `_2` から `account_1` / `account_2` を自動判定します。
- AIS Positions / Vessels / utility 直接利用は、ログインユーザーが `_1` / `_2` と一致すれば自動判定します。判定できない場合は `.env` の `SEASEARCHER_ACCOUNT_ID=account_1` または `account_2` を設定してください。
- `config["coordinator_account_id"]` を明示すると、その値が最優先されます。

状態確認だけなら次を実行できます。

    python scraping_coordinator.py status

緊急時のみ `SCRAPE_COORDINATOR_BYPASS=1` で coordinator を無視できますが、同一アカウントの二重実行を防げなくなるため通常運用では使用しないでください。


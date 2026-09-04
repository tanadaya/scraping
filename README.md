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

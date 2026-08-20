# SeaSearcher scraping

SeaSearcher の Movement、AIS Positions、Vessels 一覧を取得するためのノートブックと共通 utility です。

## Setup

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt

Chrome がインストールされている必要があります。

## Login

各ノートブックの LOGIN_CONFIG に login_user と login_password を設定してください。
ソースコードには認証情報を保存していません。utility を直接使う場合は、次の環境変数も利用できます。

    $env:SEASEARCHER_LOGIN_USER = "your-user@example.com"
    $env:SEASEARCHER_LOGIN_PASSWORD = "your-password"

## Notebooks

- scraping_seasearcher.ipynb, scraping_seasearcher_2.ipynb: Movement
- scraping_seasearcher_ais_positions.ipynb, scraping_seasearcher_ais_positions_2.ipynb: AIS Positions
- scraping_seasearcher_vessels.ipynb: Vessel CSV の取得

入力用の vessel CSV は vessel/ に置いてあります。出力データ、ログ、デバッグファイル、ローカル環境は .gitignore で除外しています。

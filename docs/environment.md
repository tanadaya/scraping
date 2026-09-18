# Python環境のセットアップと更新

## 管理方針

依存関係の編集元は `pyproject.toml` です。直接使うパッケージに下限を設け、
原則として同じメジャー内の更新を許容します。pandasは2系、Polarsは1系、
Seleniumは4系を使います。メジャー更新はCSV出力やブラウザ操作の互換性を確認してから行います。
HTML表を読む処理があるため、pandasには `html` extraを含めています。

`uv.lock` は間接依存も含めた解決結果です。Gitで共有し、通常は
`uv sync --locked` でその内容を再現します。`--locked` は設定とロックの不一致を
エラーにするため、セットアップのたびに意図せず依存関係が変わるのを防ぎます。
uvの推奨する[プロジェクト管理](https://docs.astral.sh/uv/guides/projects/)に沿った構成です。

`requirements.txt` はpip利用者向けの生成物です。直接編集せず、更新時にuvから再生成します。
`.venv` は各PCで作成してください。別PCからのコピーやGitへの追加は不要です。

## 初回セットアップ

WindowsのPowerShellでは、uvをインストールしてターミナルを開き直します。

```powershell
winget install --id=astral-sh.uv -e
```

macOS/LinuxやWinGetを使えない環境では、[uv公式のインストール手順](https://docs.astral.sh/uv/getting-started/installation/)を使ってください。
このプロジェクトはuv 0.9.0以上を必要とします。通常は最新の安定版を使います。

以後のuvコマンドはどのOSでもプロジェクトのルートで実行します。

```powershell
uv sync --locked
uv run --locked python --version
```

`.python-version` に従ってPython 3.13系を選び、見つからなければuvが取得します。
パッチ番号は指定していません。既に3.13系がある場合、毎回最新パッチに更新する動作ではありません。
システム全体のPythonを入れ替える必要はありません。
Google Chromeは別途インストールし、実行時のログイン情報とcoordinatorは
[`../README.md`](../README.md#ログイン情報) と [`../apps_script/README.md`](../apps_script/README.md) に従って設定します。

既存環境から移行するときは、実行中のノートブックのカーネルやPython処理を終了してから
`uv sync --locked` を実行し、カーネルを選び直してください。
uvは `.venv` をロックに揃えるため、手動で追加した未宣言パッケージは削除されます。
必要なものは `uv add` で宣言に追加します。

## ノートブック

通常の `uv sync --locked` は実行用依存に加え、`notebook` グループの `ipykernel` を入れます。
VS CodeではPython・Jupyter拡張機能を用意し、プロジェクト全体を開いてから
ノートブックの **カーネルの選択 → Python環境 → `.venv`** を選んでください。

- Windows: `.venv/Scripts/python.exe`
- macOS/Linux: `.venv/bin/python`

古いカーネルの表示名にPythonの別バージョンが残っている場合も、実際のパスで選び直します。
セル内の `import sys; print(sys.executable)` で確認できます。
[uvのVS Code/Jupyter連携手順](https://docs.astral.sh/uv/guides/integration/jupyter/)も参照できます。

ブラウザでJupyterLabを使う場合は、追加の `lab` グループを指定します。

```powershell
uv run --locked --group lab jupyter lab
```

CLIだけを使うPCではノートブック用の依存を省けます。

```powershell
uv sync --locked --no-default-groups
uv run --locked --no-default-groups python -m tools.coordinator status
```

## 別のPythonを使う

`requires-python = ">=3.10"` は許容範囲、`.python-version` の3.13は標準の選択です。
Pythonを3.13だけに限定する構成ではありません。別バージョンを選ぶ場合は、
同期時と実行時の両方に指定してください。

```powershell
uv sync --locked --python 3.12
uv run --locked --python 3.12 python -m unittest discover -s tests -t .
```

Pythonが異なれば、そのバージョンに対応した依存が同じ `uv.lock` から選ばれます。
Pythonの許容範囲は、すべての将来バージョンやOSでの動作保証ではありません。
新しい環境ではテストに加え、少数の船舶で実際の取得結果を確認してください。

## 依存関係の追加・更新

通常利用では更新不要です。更新担当者が許容範囲内の更新を解決し、ローカルで確認して共有します。

```powershell
uv lock --upgrade
uv sync --locked
uv run --locked python -m unittest discover -s tests -t .
uv export --locked --format requirements-txt --output-file requirements.txt
```

特定パッケージだけなら `uv lock --upgrade-package selenium`、
追加は `uv add "パッケージ名>=下限,<次のメジャー"`、
ノートブック専用なら `uv add --group notebook "パッケージ名"` を使います。
`pyproject.toml`・`uv.lock`・再生成した `requirements.txt` をセットで共有してください。
ノートブック内の `%pip install` ではこれらのファイルが更新されません。

## uvを使わずpipで再現する

対応するPythonを用意して、ルートから専用の仮想環境にインストールします。
以下はWindowsのPowerShell用です。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

macOS/Linuxでは上記のPythonパスを `.venv/bin/python` に読み替えます。
このファイルには標準の `notebook` グループを含め、任意の `lab` グループは含めていません。
pipはPython自体を用意せず、既存環境の余分なパッケージも削除しないため、新しい仮想環境を使ってください。

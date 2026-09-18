# Workspace overview

## 日常的に触る場所

通常は `notebooks/` だけを開いて設定セルを編集します。

| パス | 役割 |
| --- | --- |
| `notebooks/movement_account_1.ipynb` | Movement取得（アカウント1、入力順） |
| `notebooks/movement_account_2.ipynb` | Movement取得（アカウント2、逆順） |
| `notebooks/ais_positions_account_1.ipynb` | AIS Positions取得（アカウント1、入力順） |
| `notebooks/ais_positions_account_2.ipynb` | AIS Positions取得（アカウント2、逆順） |
| `notebooks/vessels.ipynb` | Vessels一覧CSV取得 |

## ツールの役割

| パス | 役割 |
| --- | --- |
| `tools/scrapers/movement.py` | Movement用の公開入口 |
| `tools/scrapers/ais_positions.py` | AIS Positions用の公開入口 |
| `tools/scrapers/vessels.py` | Vessels用の公開入口 |
| `tools/scrapers/*_impl.py` | Seleniumなどの実処理。通常は直接編集しない |
| `tools/coordinator/` | Google Sheets + Apps Script coordinatorとの通信・ジョブ管理 |
| `tests/` | ツールの自動テスト |
| `pyproject.toml` | Python・依存関係の許容範囲と用途別グループ |
| `uv.lock` | 各PCで再現する依存関係。uvが生成 |
| `requirements.txt` | uv.lockから生成するpip向け互換ファイル |
| `.python-version` | 標準のPythonバージョン（3.13系） |

ノートブックからは `tools.scrapers` 経由で公開入口を利用します。
実装ファイルを直接importする必要はありません。

## データと出力

| パス | 役割 |
| --- | --- |
| `vessel/` | 入力用の船舶CSV |
| `movement/` | Movement取得結果（実行時に作成） |
| `ais_positions/` | AIS Positions取得結果（実行時に作成） |
| `LNG_*/` | LNG関連の取得結果（実行時に作成） |
| `debug/`, `_tmp/`, `*_downloads/` | 実行時の一時ファイルやデバッグ情報 |

取得結果や一時ファイルは `.gitignore` で除外しています。

## coordinatorの確認

```powershell
uv run --locked python -m tools.coordinator status
```

初期設定は [`../apps_script/README.md`](../apps_script/README.md) を参照してください。

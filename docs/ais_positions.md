# scraping_seasearcher AIS Positions

Seasearcher の `Movements > AIS Positions` を取得するための実行手順です。
既存の `README.txt` は `Places and Passings` 系の説明としてそのまま残し、このファイルは AIS Positions 専用です。

前提
----
- Python 3.10 以上
- Google Chrome
- uvで依存関係とノートブック用カーネルがインストール済み

セットアップ
------------
uvをインストール後、プロジェクトのルートで実行します。

```powershell
uv sync --locked
```

VS Codeのノートブックでは、このプロジェクトの `.venv` をカーネルに選びます。
初回導入や別のPythonを使う手順は [`environment.md`](environment.md) を参照してください。

実行方法 1: ノートブックから実行
------------------------------
使うファイル:
- `notebooks/ais_positions_account_1.ipynb`
- `notebooks/ais_positions_account_2.ipynb`
- `vessel/` 配下の任意の vessel CSV 1 ファイル
- または従来の船種別 CSV 群 (`vessels_202504_<vessel_type>.csv` など)

ノートブック内で主に編集する項目:
- `SOURCE_CONFIG`
- `RUN_CONFIG`
- `SCRAPING_CONFIG`

このノートブックは 2 つの入力モードを持ちます。

- `SOURCE_CONFIG["source_mode"] = "single_file"`
  - 任意の vessel CSV 1 ファイルを指定して使います
  - 例: `vessel/Vessels_20260201_20260331_unique_llino.csv`
  - 例: `vessel/Vessels_20260301_20260331.csv`
- `SOURCE_CONFIG["source_mode"] = "split_files"`
  - 従来どおり船種別 CSV 群をまとめて使います
  - 例: `vessel/vessels_202504_bulk.csv`, `vessel/vessels_202504_container.csv`, ...

`RUN_CONFIG["run_vessel_type"]` には `"all"` か、CSV 内の `LLI Vessel Type` の値を指定できます。
`out_dir` は `RUN_CONFIG["out_dir"] = None` のとき、source 設定と `run_vessel_type` から自動で `ais_positions/test_<source_slug>_<vessel_type_slug>/` に設定されます。

設定例:

```python
SOURCE_CONFIG = {
    "source_mode": "split_files",
    "source_vessel_file": "vessel/Vessels_20260201_20260331_unique_llino.csv",
    "dir_vessel": "vessel",
    "vessel_type_list": ["bulk", "container", "cruise", "generalcargo", "roro", "tanker", "vehicle"],
    "file_template": "vessels_202504_{vessel_type}.csv",
}

RUN_CONFIG = {
    "run_vessel_type": "all",
    "run_start": 0,
    "run_end": 10,
    "max_workers": 1,
    "out_dir": None,
}

run_context = sss_ais.prepare_ais_run_context(
    source_config=SOURCE_CONFIG,
    run_vessel_type=RUN_CONFIG["run_vessel_type"],
    run_start=RUN_CONFIG["run_start"],
    run_end=RUN_CONFIG["run_end"],
    out_dir=RUN_CONFIG["out_dir"],
)

SCRAPING_CONFIG = {
    "period": {"from": "2026-03-26", "to": "2026-04-02"},
    "local_time": False,
    "out_dir": run_context["out_dir"],
    "headless": False,
    "login_user": None,
    "login_password": None,
    "log_level": "INFO",
    "log_steps": True,
    "periodic_rest_enabled": True,
    "work_session_hours": 6,
    "work_session_random_minutes": 30,
    "rest_session_hours": 1,
    "rest_session_random_minutes": 15,
    "retry_timeout_immediately": False,
    "relogin_on_confirmed_session_loss": True,
    "session_relogin_attempts": 1,
    "stop_on_access_block": True,
    "webdriver_restart_attempts": 0,
    "failed_llino_retry_max_attempts": 2,
    "period_window_retry_attempts": 1,
    "between_period_windows_delay_seconds": (2.0, 5.0),
    "max_consecutive_failed_llinos": 3,
    "check_status": False,
    "skip_if_exists": True,
    "show_progress": True,
    "encoding": "utf-8-sig",
}
```

従来の船種別 CSV 群を使う例:

```python
SOURCE_CONFIG = {
    "source_mode": "split_files",
    "dir_vessel": "vessel",
    "vessel_type_list": ["bulk", "container", "cruise", "generalcargo", "roro", "tanker", "vehicle"],
    "file_template": "vessels_202504_{vessel_type}.csv",
}
```

任意の 1 ファイルを使う例:

```python
SOURCE_CONFIG = {
    "source_mode": "single_file",
    "source_vessel_file": "vessel/Vessels_20260301_20260331.csv",
}
```

実行手順:
1. 使用するアカウントに対応する `notebooks/ais_positions_account_1.ipynb` または `notebooks/ais_positions_account_2.ipynb` を開く
2. `SOURCE_CONFIG` を編集する
3. `RUN_CONFIG` を編集する
4. `run_context = ...` のセルで対象 LLI と `out_dir` を確認する
5. `parallel_scraping_ais_positions(...)` のセルを実行する
6. 最後の `pl.DataFrame(results)` セルで結果一覧を確認する

実行方法 2: Python から直接実行
------------------------------

```python
from tools.scrapers import ais_positions as sss_ais

targets = [101474, 12903339]

SCRAPING_CONFIG = {
    "period": {"from": "2026-03-26", "to": "2026-04-02"},
    "local_time": False,
    "out_dir": "ais_positions/manual_run/",
    "headless": False,
    "login_user": None,
    "login_password": None,
    "log_level": "INFO",
    "log_steps": True,
    "periodic_rest_enabled": True,
    "work_session_hours": 6,
    "work_session_random_minutes": 30,
    "rest_session_hours": 1,
    "rest_session_random_minutes": 15,
    "retry_timeout_immediately": False,
    "relogin_on_confirmed_session_loss": True,
    "session_relogin_attempts": 1,
    "stop_on_access_block": True,
    "webdriver_restart_attempts": 0,
    "failed_llino_retry_max_attempts": 2,
    "period_window_retry_attempts": 1,
    "between_period_windows_delay_seconds": (2.0, 5.0),
    "max_consecutive_failed_llinos": 3,
    "check_status": False,
    "skip_if_exists": True,
    "show_progress": True,
    "encoding": "utf-8-sig",
}

results = sss_ais.parallel_scraping_ais_positions(
    targets,
    max_workers=1,
    config=SCRAPING_CONFIG,
)
```

主な設定
--------
- `period`
  - `"all"` ですべての期間
  - `{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}` で期間指定
- `RUN_CONFIG["run_vessel_type"]`
  - `"all"` ですべての Live 船を対象
  - それ以外は CSV 内の `LLI Vessel Type` 名で絞り込み
- `SOURCE_CONFIG["source_mode"]`
  - `"single_file"` で 1 ファイル入力
  - `"split_files"` で従来の船種別ファイル群を入力
- `SOURCE_CONFIG["source_vessel_file"]`
  - `single_file` モードで使う入力 CSV
- `local_time`
  - `True` にすると Local Time を ON
- `out_dir`
  - 出力先フォルダ
  - `RUN_CONFIG["out_dir"] = None` のとき自動設定
- `headless`
  - `False` でブラウザを表示
  - 最初の確認時は `False` 推奨
- `login_user`
  - 別アカウントでログインしたいときのユーザー名
- `login_password`
  - `login_user` とセットで指定するパスワード
- `log_level`
  - `"DONE"` で一隻ごとの完了だけ表示
  - `"INFO"` で通常ログ
  - `"DEBUG"` で詳細ログ
- `log_steps`
  - `True` で各ステップのログを表示
- `skip_if_exists`
  - 既に `out_dir` に同じ LLI の AIS CSV がある場合はスキップ
  - `parallel_scraping_ais_positions(...)` では実行前に一括でローカル確認し、既存 CSV がある LLI は worker に渡しません
- `show_progress`
  - 進捗表示を出す
- `periodic_rest_enabled`
  - `True` で連続稼働後の休止を有効化
- `work_session_hours`
  - 休止に入るまでの基準稼働時間
- `work_session_random_minutes`
  - `work_session_hours` に対して前後何分ランダムにずらすか
- `rest_session_hours`
  - 休憩の基準時間
- `rest_session_random_minutes`
  - `rest_session_hours` に対して前後何分ランダムにずらすか
- `retry_timeout_immediately`
  - 互換性のため残していますが、通常は `False` にします。通常の timeout は再ログイン理由になりません
- `relogin_on_confirmed_session_loss`
  - `True` の場合、ログイン画面への遷移を確認したときだけ同じブラウザで再ログインします
- `session_relogin_attempts`
  - 1回の認証切れに対する再ログイン上限です。推奨値は `1` です
- `stop_on_access_block`
  - アクセス拒否、レート制限、CAPTCHAを検知したとき全workerを停止します
- `webdriver_restart_attempts`
  - WebDriver障害時のブラウザ再作成回数です。不要な再ログインを避ける推奨値は `0` です
- `failed_llino_retry_max_attempts`
  - 失敗LLIを同じセッションで試す総回数です。推奨値は `2` です
- `max_consecutive_failed_llinos`
  - この件数だけLLI失敗が連続したら、負荷を増やさず再開用データを残して停止します

動作仕様
--------
- `AIS Positions` タブを開いて取得します
- `Columns` は毎回すべて選択します
- `items per page` は毎回 `1000` に固定します
- 各ページで `Export -> CSV` を実行します
- 複数ページある場合は、ページごとの CSV を取得したあと自動で結合します
- ダウンロード時に同名ファイルでも、処理中に一時ファイルを固有名へリネームして衝突を防ぎます
- 結合成功後はページ別の一時 CSV は削除し、最終 CSV だけ残します
- `periodic_rest_enabled=True` のときは、各 worker が `work_session_hours` を基準に前後 `work_session_random_minutes` 分のランダム幅を持たせて動き、休止時も `rest_session_hours` を基準に前後 `rest_session_random_minutes` 分だけランダムに休みます
- ページャーの `現在ページ / 総ページ数` を確認し、最終ページより先へは進みません。サイト側が範囲外ページを表示しても timeout ではなくページ終端として処理します
- 通常の timeout は同じブラウザ・同じログインセッションのまま、設定上限内でのみ再試行します
- ログイン画面への遷移を確認した場合だけ、同じブラウザで1回再ログインします
- アクセス拒否、レート制限、CAPTCHAを検知した場合は再ログインせず、証跡を保存して全workerを停止します
- `login_user` と `login_password` を両方 `None` のままにすると既定アカウントでログインします

出力ファイル
------------
最終 CSV:

```text
ais_positions_<LLINO8>_<period_label>.csv
```

例:

```text
ais_positions_00101474_20260326_20260402.csv
```

補助ディレクトリ:
- `out_dir/_tmp/...`
  - ページ別 CSV の一時保存先
  - 正常終了後は削除されます
- `out_dir/debug/`
  - 失敗時のスクリーンショットや HTML を保存します

最初に試すときのおすすめ
------------------------
- `max_workers=1`
- `headless=False`
- `RUN_CONFIG["run_end"] - RUN_CONFIG["run_start"]` を 1 か 2 にする
- まず短い期間で 1 船だけ試す

注意点
------
- Seasearcher 側の UI が変わると、`AIS Positions` タブや `Export` ボタンの検出が失敗することがあります
- データが 0 件の期間では `skipped=True` として終了し、最終 CSV は作成されません
- 並列実行時はワーカーごとにダウンロード先を分けているため、`max_workers=2` 以上でもファイルが混ざらない想定です

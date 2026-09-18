# scraping_seasearcher

使い方（初めての方向け）
=======================

このリポジトリは Seasearcher の Movements データを取得するための小さなスクレイピングユーティリティです。以下は最小限の準備と実行手順、出力の確認方法です。

前提（Prerequisites） ✅
- Python 3.10+ がインストールされていること
- Chrome ブラウザがインストールされていること（webdriver-manager を使って自動でドライバを取得します）
- 推奨: 仮想環境（venv）を作り、PowerShell で作業すること

インストール
-------------
1. 仮想環境を作成・有効化（PowerShell）:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. 依存パッケージをインストール:

```powershell
pip install -r requirements.txt
```

基本的な使い方
--------------
- ノートブックまたはスクリプトから `utils_scraping_seasearcher` を使ってスクレイピングを行います。

例（最小実行例）:

```python
import utils_scraping_seasearcher as sss

SCRAPING_CONFIG = {
    "max_hops": 10,                         # カレンダーで最大何回遡るか（古い日を探す用）
    "status_list": ["Calls", "Passings"],# 取得したいステータス種類
    # period: 'all' (デフォルト) または {'from': 'YYYY-MM-DD', 'to': 'YYYY-MM-DD'} のように指定可能
    # 例: "period": "all"  または  "period": {"from":"2025-06-21", "to":"2025-12-21"}
    "period": "all",
    "local_time": False,                    # Trueにすると時刻を現地時間に変換
    "out_dir": "movement/test_container/",# 出力フォルダ
    "headless": False,                      # Trueでブラウザを非表示にする
    "log_steps": True,                      # 各ステップでログを出す
}

targets = [12903339]  # LLI のリスト（複数可）
results = sss.parallel_scraping(targets, max_workers=1, config=SCRAPING_CONFIG)
```

- `parallel_scraping` は複数 LLI を並列処理できます。まずは `max_workers=1` で動作確認をすることを推奨します。

Period（期間）について（重要） 🔍
- `SCRAPING_CONFIG` の `period` によって挙動を制御できます。
  - `"period": "all"`（デフォルト）: ステータス選択後に期間フィルタを **全期間 (All)** にリセットします。
    - 優先動作: 日付レンジ要素（クラス: `lli-range-input lli-range-input--date lli-range-filter--selected date-range--gray`）内の `.lli-dropdown--cancel` をクリックしてクリアします。
    - フォールバック: `Period` ラベル近傍のクリアボタン、あるいは `From/To` 入力を直接クリアします。
  - `"period": {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}`: 指定した期間にセットします。
    - 受け付ける日付形式例: `YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`
    - 内部で `DD/MM/YYYY` に整形して From/To 入力にセットします（可能な場合）。
  - 簡易指定: `"period": ["2025-06-21","2025-12-21"]` のようなタプル/リストでも扱えます。
- メリット: 指定した期間で結果を限定したり、`all` を選んで全期間を取得したり用途に応じて切り替えられます。

出力とデバッグ
--------------
- データは `SCRAPING_CONFIG["out_dir"]` に保存されます。通常 `movement/` 以下にファイルが作られます。
- 問題発生時の診断情報（スクリーンショット、HTML、JSON）は `out_dir/debug/` に保存されます。
  - 例: `llino_<LLINO>_status_check_<TIMESTAMP>.html`
- 確認ポイント:
  - `log_steps=True` にしてログを観察する
  - デバッグ HTML を開き、Period の入力が空（または All）になっていることを目視で確認する

トラブルシューティング
----------------------
- Chrome のバージョンやサイトの UI が変わると、セレクタが動作しなくなる場合があります。エラーが出たら保存されたデバッグ HTML を確認し、該当要素のクラス名や構造を更新してください。
- `TimeoutException` が投げられた場合は、ログを確認して再試行してください。`log_steps` を有効にすると詳細ログが得られます。

運用のヒント ✅
- 最初は `max_workers=1`、 `headless=False`, `log_steps=True` で挙動を確認する。
- 正常に動作することを確認したら `headless=True` や `max_workers` を増やして性能を上げる。
- 既に `movement` フォルダに該当の LLI の出力がある場合は、その LLI をスキップする設定 (`skip_if_exists`) をデフォルトで有効にしています。再取得したい場合は `SCRAPING_CONFIG["skip_if_exists"] = False` を指定してください。
- `SCRAPING_CONFIG["show_progress"]` を `True` にすると進捗バーを表示します（`tqdm` がインストールされている場合はリッチなバーが使われます）。

注意点
----------------------
- 同時に動かす人はひとりまでにしてください。


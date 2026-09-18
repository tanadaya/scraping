# Workspace Overview

このワークスペースは SeaSearcher のスクレイピング実行用ノートブック、共通処理の Python ファイル、取得済みデータで構成されています。

## ノートブックと Python ファイルの対応

| ノートブック | 対応する Python ファイル | 用途 | 主な出力先 |
| --- | --- | --- | --- |
| `scraping_seasearcher.ipynb` | `utils_scraping_seasearcher.py` | Movement データ取得 | `movement/` |
| `scraping_seasearcher_2.ipynb` | `utils_scraping_seasearcher.py` | Movement データ取得の別実行設定 | `movement/` |
| `scraping_seasearcher_ais_positions.ipynb` | `utils_scraping_seasearcher_ais_positions.py` | AIS Positions 取得 | `ais_positions/` |
| `scraping_seasearcher_ais_positions_2.ipynb` | `utils_scraping_seasearcher_ais_positions.py` | AIS Positions 取得の別実行設定 | `ais_positions/` |
| `scraping_seasearcher_vessels.ipynb` | `utils_scraping_seasearcher_vessels.py` | Vessels 一覧取得 | `vessel/` |

補助スクリプト:

| ファイル | 用途 |
| --- | --- |
| `rerun_bad_feb_ais_positions.py` | `bad_feb_ais_llinos.txt` の LLI を使って 2026年2月 AIS Positions を再取得 |

## 主なデータ・設定ファイル

| パス | 内容 |
| --- | --- |
| `vessel/` | Vessels 一覧CSVと、船種・サイズ別に分けた入力CSV |
| `movement/` | Movement の取得済みCSV |
| `movement_20250101_20260411/` | 2025-01-01 から 2026-04-11 までの Movement 取得済みデータ |
| `ais_positions/` | AIS Positions の取得済みCSV |
| `AISデータ_2月/` | 2026年2月分の AIS Positions データ |
| `AISデータ_3月/` | 2026年3月分の AIS Positions データ |
| `places_port-subport-anchorage-passingplace_with-region.csv` | 港湾・停泊地・通過地点などの参照データ |
| `bad_feb_ais_llinos.txt` | 2026年2月 AIS Positions の再取得対象 LLI |
| `bad_mar_ais_llinos.txt` | 2026年3月 AIS Positions の再取得対象 LLI |
| `requirements.txt` | Python 依存ライブラリ |

## 整理方針

- ノートブックはルートに残しています。別フォルダへ移動すると `import utils_...` の参照パスが変わるためです。
- `utils_scraping_seasearcher.py` は Movement 共通処理、`utils_scraping_seasearcher_ais_positions.py` は AIS Positions 専用処理、`utils_scraping_seasearcher_vessels.py` は Vessels 専用処理です。
- `debug/`、`_tmp/`、`.ipynb_checkpoints/`、`__pycache__/`、ログファイル、展開済みデータと重複する zip は整理対象です。
- `.venv/` は再作成可能ですが、現在の実行環境として使われている可能性があるため残しています。

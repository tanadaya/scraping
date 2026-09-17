# Google Sheets + Apps Script coordinator setup

この仕組みは、SeaSearcher のスクレイピング自体を Google 上で実行するものではありません。各 PC の Python は従来どおりローカルで動き、Google Sheet / Apps Script は **2アカウントの排他ロック、待ち行列、進捗、ETA、異常終了検知**だけを管理します。

## 1. Google Sheet を作る

研究室で引き継げる Google アカウントまたは Google Workspace 上に新しい Sheet を作成してください。個人アカウントだけに依存させない運用を推奨します。

Sheet から **拡張機能 > Apps Script** を開き、このフォルダの `Code.gs` と `Storage.gs` を、同じ Apps Script プロジェクト内の2つのスクリプトファイルとして貼り付けます。

## 2. 初期化

Apps Script エディタで `setupCoordinator()` を1回だけ実行します。Sheet に次の2タブが作成されます。

- `Accounts`: 現在どのアカウントを誰が使用しているかを見る画面
- `Jobs`: 実行履歴と待ち行列

初期状態では `account_1` と `account_2` が登録されます。

## 3. Script Properties

Apps Script の **Project Settings > Script Properties** に以下を登録します。

- `COORDINATOR_TOKEN`: 十分長いランダム文字列（必須）
- `LEASE_SECONDS`: `900` 推奨。heartbeat が途絶えた実行を15分後に失効させる
- `QUEUE_TTL_SECONDS`: `300` 推奨。待機中クライアントが5分間pollしなければ待ち行列から失効させる

`COORDINATOR_SPREADSHEET_ID` は `setupCoordinator()` が自動設定します。

SeaSearcher のユーザー名・パスワードは Sheet や Apps Script に保存しません。

## 4. Web App として deploy

**Deploy > New deployment > Web app** からデプロイします。Python から到達できるアクセス設定を選び、発行された `/exec` URL を控えます。

組織の Google Workspace ポリシーにより匿名 Web App が禁止されている場合は、管理者が許可する範囲の設定を利用してください。この場合でも API token は必須のままにしてください。

## 5. 各 PC の `.env`

`.env.example` をコピーし、次を設定します。

```env
SCRAPE_COORDINATOR_URL=https://script.google.com/macros/s/...../exec
SCRAPE_COORDINATOR_TOKEN=ここにScript Propertiesと同じ値
SCRAPE_OPERATOR=利用者の名前
```

Movement notebook 1 / 2 は専用のログインユーザーから `account_1` / `account_2` を自動判定します。AIS Positions / Vessels など generic login を使う処理は、そのユーザーが `_1` / `_2` と一致しない場合 `.env` の `SEASEARCHER_ACCOUNT_ID=account_1` または `account_2` を設定してください。

## 6. 動作確認

```powershell
python scraping_coordinator.py status
```

設定が正しければ `account_1` / `account_2` の状態が表示されます。

その後、2台の PC で同じ account のジョブを起動して確認します。先に開始した PC だけが `RUNNING` になり、もう一方は `QUEUED` のまま待機します。先行ジョブ終了後、次のジョブへ自動的に権利が移ります。

## 安全側の挙動

- Coordinator URL / token が未設定ならスクレイピングは開始しません（fail closed）。
- 実行中は60秒ごとに heartbeat を送ります。
- heartbeat が途絶えて lease が切れたジョブは `EXPIRED` になります。
- 待機中プロセスが終了して poll が途絶えると `ABANDONED` になります。
- `LockService.getScriptLock()` により、複数PCが完全に同時に予約を試みても Sheet 更新は直列化されます。
- 緊急時のみ `SCRAPE_COORDINATOR_BYPASS=1` でロックを無視できます。通常運用では使用しないでください。

## 引き継ぎ時

卒業・異動前に次を確認してください。

1. Google Sheet と Apps Script を研究室側で継続管理できること
2. `COORDINATOR_TOKEN` の保管場所が引き継がれていること
3. GitHub のこの README、`Code.gs`、`Storage.gs` が最新版であること
4. 必要なら token を再発行し、各 PC の `.env` を更新すること

Apps Script を更新した場合は、Web App の deployment も新しい version に更新してください。

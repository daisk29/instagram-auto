# LINE連携と管理画面からの写真追加

| 入口 | 対象 | 動作 |
| --- | --- | --- |
| `POST /line/webhook` | 美趣 | 写真をDriveへ転送し、写真の後3分以内に届いた参考文でキャプション案を作成。LINE返信と承認後の即時投稿は設定で有効化できる。 |
| `POST /line/shared/webhook` | Crach29・二九写真店 | リッチメニューでブランドを切り替え、写真送信、キャプション案の確認・修正、投稿承認、日時指定を行う。 |
| `POST /direct-upload/photo` | 管理画面で選んだアカウント | JPEG・PNGをDriveの受信フォルダに保存して未処理一覧に追加する。投稿は行わない。 |

## 設定

- `.env.example` を参照し、既存の `.env`・Google OAuthファイル・SQLiteはサーバー側に保持します。公開Web領域に置きません。新規設定では `APP_SECRET_KEY` に固定のランダムな32文字以上の値と管理者パスワードを設定します。
- 美趣と共用LINEのチャネルは別々に設定します。許可ユーザー・承認者を複数指定するときはカンマ区切りにします。同じ人が担当する場合は共用LINEの両方のリストに同じIDを設定します。
- 美趣とCrach29は各ブランドの `*_DRIVE_INBOX_FOLDER_ID`、二九写真店は共用LINE側の `GOOGLE_INBOX_FOLDER_ID` が管理画面の該当アカウントの受信フォルダと一致していることを確認します。
- 共用LINEは `SHARED_LINE_ENABLE_POSTING=0` で写真・キャプション案まで確認できます。LINEからの実投稿と予約投稿は明示的に有効化します。
- LINE Developersに登録するWebhookは、各チャネルで上表のパスを公開アプリのURLに追加したものです。

## 美趣専用LINEでの確認と投稿

既存のWebhook URL・チャネル・Drive設定を維持し、サーバーの `.env` に以下を追加します。最初は実投稿を無効にしたまま確認します。

```dotenv
BISYU_LINE_ENABLE_CAPTION_REPLY=1
BISYU_LINE_ENABLE_POSTING=0
BISYU_LINE_APPROVER_IDS=
```

1. 美趣公式LINEへ写真を1〜10枚送ります。続けて3分以内に投稿の参考文を1通送ります。写真だけの場合は従来どおりDrive保存までです。
2. トークに案が届き、管理画面の美趣にも下書きができることを確認します。
3. LINEから投稿する場合は、承認担当者を既存の `LINE_ALLOWED_USER_IDS` と `BISYU_LINE_APPROVER_IDS` の両方に登録し、`BISYU_LINE_ENABLE_POSTING=1` にします。
4. 新しく写真と参考文を送り、案を確認します。写真を送った承認担当者が「承認して今すぐ投稿」を押すと `@bisyu_1995` に公開し、結果を同じトークに通知します。確認時に作った古い案にボタンは後から付きません。

投稿には美趣のInstagram接続、Driveの投稿済みフォルダ、HTTPSの `PUBLIC_BASE_URL` が必要です。管理画面で変更された案は古いボタンでは投稿できません。修正や日時指定は管理画面を使用します。

LINEの写真保存と承認処理には既存の `line_drive_worker.py` を使います。Webhookから起動できなかった場合に備え、同じワーカーを既存の設定どおりcronでも実行してください。結果の照合には既存の `run_scheduled.py` または管理画面が必要です。共用LINEの設定やワーカーは変更不要です。

## 共用LINEとリッチメニュー

```sh
venv/bin/python preflight.py
venv/bin/python richmenu_setup.py check
venv/bin/python richmenu_setup.py status
```

未導入の環境では `richmenu_setup.py install` で2つのメニューを作り、`richmenu_setup.py activate` で許可済みユーザーに割り当てます。すでに割り当て済みなら再実行は不要です。

Webhookはワーカーを非同期起動します。中断時の再処理と日時指定のため、対象環境に合わせて `line_drive_worker.py`、`shared_line_worker.py`、`shared_line_flow_worker.py` を定期実行します。共用LINEのフローワーカーと管理画面用の `run_scheduled.py` は同じ投稿DBを参照し、予約の取得を排他制御します。既設の同一cronは重複登録しません。

```sh
venv/bin/python -m unittest discover -s tests -v
```

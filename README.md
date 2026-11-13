# Instagram 投稿準備・管理システム（複数アカウント対応）

投稿する写真とキャプションを人が確認し、「この内容でInstagramに投稿」を押した時だけInstagram Graph APIで投稿するWebアプリです。

管理画面上部でアカウントを切り替えられます。既存の3アカウントに加え、Meta認証で新規アカウントを追加できます。写真、投稿案、履歴、
Driveフォルダ、キャプション指示、Instagram API設定はアカウントごとに分離します。

- 二九写真店 `@29shashinten`
- Crach29 `@crach.29`
- 美趣 `@bisyu_1995`

既存DBは初回起動時に自動更新され、これまでの写真と投稿履歴はすべて
`@29shashinten`として保持されます。

## 写真の追加とLINE連携

管理画面の「写真をアップロード」で、選択中のアカウントのDriveへ直接追加できます。
美趣の専用LINEおよびCrach29・二九写真店の共用LINEの設定は [LINE連携と写真追加](docs/line-and-upload.md) を参照してください。

## アカウント0件からの初期設定

ヘッダーの「アカウント管理」からMeta認証を行い、Instagramアカウントを選んで接続できます。表示名・Driveフォルダ・投稿文の指示を画面で設定できます。既存アカウントの再認証にも対応しています。二九写真店・Crach29・美趣を含む既存アカウントも、Meta認証前から同じ設定画面でDriveのinbox／postedを編集できます。

`.env`のInstagram IDとアクセストークンが空で、DBにも認証済みアカウントがない場合、固定のアカウント枠は表示せず0件から開始します。同じユーザー名を再認証すると従来のアカウントキーを再利用するため、残してある写真・投稿履歴・予約へ再接続できます。

初回設定と既存環境への適用は [UPDATE-v4-OAUTH.md](UPDATE-v4-OAUTH.md) を参照してください。アカウント認証には共通のMetaアプリID・App Secret・リダイレクトURI設定が必要です。

## 実装済みの流れ

1. Google Driveの`inbox`へJPEGまたはPNGを入れる
2. 管理画面で「Driveから写真を読み込む」を押す
3. 1〜10枚を選び、OpenAI APIでキャプション案を生成する
4. 人が文章を編集・保存・再生成・保留する
5. 確認欄にチェックし、「今すぐ投稿」または日本時間で「予約投稿」を選ぶ
6. 1枚は通常投稿、2〜10枚はカルーセルとして自動投稿する
7. 投稿成功時だけDriveの`posted`へ写真を移す
8. SQLiteへ投稿ID、URL、日時、エラー履歴を保存する

投稿ボタンを押すと即座にボタンを無効化し、画像送信・Instagram側の
処理・公開・Drive移動の実進捗を1秒ごとに画面へ表示します。投稿内の
Meta API通信はHTTPS接続を再利用し、メディア処理の確認間隔も5秒から
1秒へ短縮しています。

予約投稿では、初期値として次に来る16:00を表示します。日時を確定すると
キャプションを固定し、指定時刻までは予約を取り消せます。cronが指定時刻を
迎えた投稿案を取得し、投稿・Drive移動・履歴保存を自動実行します。

「Driveから写真を読み込む」と「選択した写真でキャプション生成」も、
送信直後にボタンを無効化して二重実行を防ぎ、画面が切り替わるまで
ロードバーと現在の処理内容を表示します。

すべての操作ボタンには押下時の沈み込みを付けています。フォーム送信中は
画面全体を半透明グレーで覆い、中央に処理内容・スピナー・ロードバーを表示して
完了まで重複操作を防ぎます。Instagram投稿中は実際の進捗率を表示し、
安全に中止できる段階だけ全画面表示内の「中止する」を操作できます。

写真をDriveへ入れただけでは、解析も投稿も始まりません。投稿ボタンの二重クリックによる重複投稿もDB側で防止します。Instagram投稿成功後にDrive移動だけ失敗した場合も投稿済みとして保持し、再投稿させません。

投稿中に端末の回線切断やサーバープロセスの終了が起きた場合も、Instagramの
公開IDを取得した時点でDBへ先に保存します。通信中は定期的に稼働状態を記録し、
10分以上更新が止まった処理は管理画面の再表示またはcron実行時にInstagramの
最近の投稿と照合します。公開開始前の中断なら再実行可能に戻し、投稿IDまたは
一致する投稿を確認できれば完了処理を再開します。公開開始後に結果を確認できない
場合は「公開結果を要確認」にして自動再投稿を止めます。Instagramの該当アカウントで
未投稿と確認した場合に限り、投稿案画面で確認欄をチェックして再試行を許可します。
投稿済みなら再試行せず、手動での復旧が必要です。

投稿画面の進捗欄には「中止する」ボタンを表示します。画像送信やInstagram側の
メディア準備中は安全に中止でき、写真は確認待ちへ戻ります。Instagramへの
公開APIを呼び出す直前に状態を`committing`へ原子的に変更し、それ以降は
投稿結果が曖昧になるのを防ぐため中止を受け付けません。

保留または中止した投稿案には「投稿案を削除する」ボタンを表示します。
削除すると写真は未処理へ戻るため、必要なら再度投稿案を作成できます。
Drive上の原本とサーバー上の画像ファイルは削除しません。

## ファイル構成

```text
instagram-post-manager/
├── app.py                 Web管理画面
├── caption_client.py      OpenAI画像解析・文章生成
├── drive_client.py        Drive読込・移動
├── instagram_client.py    Instagram投稿
├── image_prepare.py       EXIF補正・投稿比率への中央トリミング
├── database.py            SQLite履歴管理
├── config.py              環境設定
├── create_token.py        MacでのGoogle OAuth認証
├── setup_crach29.py       Crach29のMeta設定を安全に登録
├── setup_bisyu.py         美趣のMeta設定を安全に登録
├── publish_service.py     即時・予約共通の投稿処理
├── run_scheduled.py       cron用の予約投稿実行
├── instagram_schedule.sh  CORESERVERのcron実行用シェル
├── CRON.txt               CORESERVERのcron設定例
├── check_config.py        設定確認
├── wsgi.py                本番起動用
├── templates/             管理画面
├── static/                CSS
├── credentials/           OAuth JSONとtoken.json（配布物には含めない）
├── data/                   DBと投稿用画像
└── logs/                   実行ログ
```

## 1. サーバーへ設置

```bash
cd ~/instagram-auto
/usr/bin/python3.9 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
mkdir -p credentials data/media logs
chmod 700 credentials data logs
```

`.env`の`APP_SECRET_KEY`と管理パスワードには、それぞれ十分長い別の値を設定します。秘密値はチャットやGitへ貼り付けないでください。

## 2. Google OAuth

すでにダウンロードしたMac上のファイルを、プロジェクトの`credentials`へ置きます。

```text
/Users/daisk29/Desktop/google_oauth_credentials.json
```

Macでこのプロジェクト一式を展開し、`.env.example`を`.env`へコピーしてから実行します。

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python create_token.py
```

ブラウザでGoogle Driveへのアクセスを許可すると、`credentials/token.json`ができます。OAuth同意画面がテスト中の場合は、使用するGoogleアカウントをテストユーザーへ追加してください。

長期運用ではGoogle Auth Platformの公開ステータスを「本番環境」にします。
自分だけで使う場合はGoogleへの検証申請は不要ですが、「テスト中」のままでは
Drive権限を含む認証が7日で失効します。

作成した2ファイルをサーバーへ送ります。

```bash
scp /Users/daisk29/Desktop/google_oauth_credentials.json da29@m29.coreserver.jp:instagram-auto/credentials/google_oauth_credentials.json
scp credentials/token.json da29@m29.coreserver.jp:instagram-auto/credentials/token.json
```

サーバー側で権限を絞ります。

```bash
chmod 600 ~/instagram-auto/credentials/*.json
```

初回起動後は「アカウント管理」→対象アカウントの「設定」から、Driveの`inbox`と`posted`を設定します。フォルダURLをそのまま貼り付けるか、URLの`/folders/`以降のIDを入力できます。

既存の`.env`にあるDrive IDは初期値として引き続き読み込みます。管理画面で一度保存すると、そのアカウントはDBに保存した設定を優先します。`.env`は自動で書き換えません。

Crach29用の既存`.env`設定は次のとおりです。管理画面へ保存した後も、移行前の控えとして残せます。

```dotenv
CRACH29_DRIVE_INBOX_FOLDER_ID=
CRACH29_DRIVE_POSTED_FOLDER_ID=
```

美趣用の既存`.env`設定は次のとおりです。

```dotenv
BISYU_DRIVE_INBOX_FOLDER_ID=
BISYU_DRIVE_POSTED_FOLDER_ID=
```

## 3. OpenAIとInstagram

`.env`へ次を設定します。

```dotenv
OPENAI_API_KEY=...
INSTAGRAM_IG_USER_ID=...
INSTAGRAM_ACCESS_TOKEN=...
PUBLIC_BASE_URL=https://実際の公開URL
```

InstagramはプロアカウントとMetaアプリ側のContent Publishing権限が必要です。アクセストークンは長期運用用を使用し、期限管理を別途行います。写真をInstagram側が取得するため、`PUBLIC_BASE_URL/media/<ランダム値>`は外部からHTTPSで到達できる必要があります。

### Crach29のInstagram設定

Graph API Explorerで既存のPost Managerアプリを選び、必要な権限を付け、
Crach29に紐づくFacebookページへのアクセスを許可した短期トークンを生成します。
トークンやApp Secretはチャットへ貼らず、サーバーで次を実行します。

```bash
cd ~/instagram-auto
venv/bin/python setup_crach29.py
```

画面の質問へMetaアプリID、App Secret、短期トークンを順に入力します。
App Secretとトークンは入力中も画面に表示されません。スクリプトはCrach29を
自動判定し、`.env`へ次だけを保存します。

```dotenv
CRACH29_INSTAGRAM_IG_USER_ID=...
CRACH29_INSTAGRAM_ACCESS_TOKEN=...
```

### 美趣のInstagram設定

Graph API Explorerで既存のPost Managerアプリを選び、必要な権限を付け、
美趣に紐づくFacebookページへのアクセスを許可した短期トークンを生成します。
トークンやApp Secretはチャットへ貼らず、サーバーで次を実行します。

```bash
cd ~/instagram-auto
venv/bin/python setup_bisyu.py
```

画面の質問へMetaアプリID、App Secret、短期トークンを順に入力します。
スクリプトは`@bisyu_1995`を自動判定し、`.env`へ美趣専用のInstagram User IDと
長期運用用ページアクセストークンを保存します。

設定後は`venv/bin/python check_config.py`で3アカウントを確認します。

## 4. 起動前確認

```bash
cd ~/instagram-auto
source venv/bin/activate
python check_config.py
python -m unittest discover -s tests
gunicorn --bind 127.0.0.1:8000 --workers 1 --timeout 180 wsgi:application
```

本番ではApache等からGunicornへリバースプロキシし、必ずHTTPSで公開します。共用サーバー側の常駐プロセス・リバースプロキシ可否によって起動方法が変わるため、契約プランのWebアプリ実行方式へ合わせてください。

## 5. 予約投稿用cron

`.env`では日本時間と初期表示時刻を指定できます。

```dotenv
APP_TIMEZONE=Asia/Tokyo
DEFAULT_SCHEDULE_HOUR=16
PUBLISH_STALE_MINUTES=10
```

CORESERVERでは`instagram_schedule.sh`へ実行権限を付けます。

```bash
chmod 700 /path/to/instagram-auto/instagram_schedule.sh
```

コントロールパネルの「シェルスクリプト名」には次を指定します。

```text
instagram-auto/instagram_schedule.sh
```

分欄に数値が必須の場合、`0`を指定すると毎時0分に実行されます。
一般的なcron書式を直接登録できる環境では、次を1分間隔で登録します。

```cron
* * * * * cd /path/to/instagram-auto && /path/to/instagram-auto/venv/bin/python /path/to/instagram-auto/run_scheduled.py >> /path/to/instagram-auto/logs/cron.log 2>&1
```

1分間隔が選べない場合は5分間隔でも動作しますが、指定時刻から最大約5分の
遅れが生じます。複数のcronが重なっても、DBの排他処理により同じ投稿案を
二重投稿しません。

## 画像の扱い

Driveから取り込む時にEXIFの回転を反映し、Instagramが受け付ける`4:5〜1.91:1`の範囲へ中央トリミングします。幅は最大1440px、JPEG品質94です。管理画面には処理後の画像を表示するため、投稿前に実際の構図を確認できます。元のDrive画像は変更しません。

## セキュリティ

- `.env`、OAuth JSON、`token.json`、SQLite DBは公開ディレクトリへ置かない
- 管理画面には独自ログインとCSRF対策を実装済み
- 投稿画像URLは推測困難なランダムトークン方式
- InstagramアクセストークンやOpenAI APIキーをログへ出さない
- HTTPS以外では公開しない

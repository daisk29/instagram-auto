# v4.2：アカウント0件からInstagram認証・Drive設定を開始

## 更新内容

- ヘッダーに「アカウント管理」を追加。
- 「Meta認証でアカウントを追加」→Facebookログイン→ページ／Instagramの利用許可→アカウント選択→保存。
- 同じInstagram IDでの再接続は認証情報を更新。既存のアカウントキー、写真、投稿案、予約、履歴、Drive設定、投稿文の指示を引き継ぐ。
- Instagram IDが未設定の既存アカウントは、Metaから取得したユーザー名が既存設定と一致すると既存の枠へ接続する。
- すべてのアカウントで、表示名、Drive inbox／posted、投稿文の基本指示を画面から設定・編集できる。DriveフォルダURLの貼り付けにも対応。
- `.env`で設定済みの二九写真店・Crach29・美趣も、Meta認証前から同じ設定画面を使用できる。一度保存するとDBの値を優先し、Instagram ID・アクセストークンは従来設定のまま維持する。
- `.env`のInstagram ID・アクセストークンとDBのアカウント設定を初期化した場合、二九写真店・Crach29・美趣の固定枠を表示せず、登録0件からMeta認証を開始できる。
- 初期化後に同じInstagramユーザー名を認証すると従来のアカウントキーを再利用し、削除していない写真・投稿履歴・予約を再び表示できる。
- 新規接続を予約投稿cronと複数Web workerがDBから読み込む。追加のたびに.envを書き換える必要はない。
- アクセストークンと認証途中の候補を暗号化してSQLiteへ保存。認証情報はCookieやHTMLへ出力しない。
- ログイン、CSRF、セッションに紐付いたstate、10分の有効時間、認証リクエストの一度だけの利用で認証フローを保護。
- 投稿に必要な権限・MetaアプリID・IG IDを確認し、認証期限を表示。期限切れの投稿はAPI呼び出し前に停止する。
- 認証画面はキャッシュを禁止。投稿APIにもBearer認証を使用し、通信エラーにトークンを含めない。

## 1. 既存環境への適用

このZIPは全体版です。展開すると `instagram-post-manager/` にコードが入っています。
現在の設置先が `/path/to/instagram-auto` なら、**その直下へコードを上書き**してください。`instagram-post-manager` をさらに入れ子にしないでください。

更新前にアプリコード、`.env`、`credentials/`、`data/` をバックアップし、更新中は予約投稿cronを一時停止してください。投稿処理が完了してから切り替えます。

既存の `.env`、Googleの `credentials/`、SQLite DB、画像、ログは維持してください。このZIPに実データ・認証済みトークン・秘密情報は含まれていません。
`.env.example` は見本です。既存の `.env` へ必要項目だけ追記してください。

変更・追加ファイル：

- app.py / config.py / instagram_client.py / run_scheduled.py / check_config.py / requirements.txt
- account_store.py / account_routes.py / meta_oauth.py
- templates/base.html / templates/index.html
- templates/accounts.html / templates/account_select.html / templates/account_settings.html
- static/style.css
- .env.example / README.md / UPDATE-v4-OAUTH.md / tests/test_account_oauth.py / tests/test_instagram_client.py

サーバーのアプリ用venvで依存関係を更新します（現在のPython 3.9環境に対応）：

```bash
cd /path/to/instagram-auto
source venv/bin/activate
python -m pip install -r requirements.txt
```

venvの名前や設置先が異なる場合は、現在のアプリが使用している環境を指定してください。

## 2. .envの初期設定（一度だけ）

```dotenv
PUBLIC_BASE_URL=https://instagram.example.com
META_APP_ID=現在使用しているMetaアプリID
META_APP_SECRET=そのMetaアプリのApp Secret
META_LOGIN_CONFIG_ID=
ACCOUNT_TOKEN_ENCRYPTION_KEY=
```

- Meta App IDとApp Secretは、Facebook Login側のMetaアプリのものを設定します。既存の二九写真店などと同じMetaアプリを利用できます。
- `PUBLIC_BASE_URL` は実際のアプリの公開URLです。`/instagram` などのサブパスに設置している場合は、そのサブパスも含めます。
- Facebook Login for Businessの場合は「ユーザーアクセストークン」を取得する構成を作成し、その構成IDを `META_LOGIN_CONFIG_ID` に設定してください。通常のFacebook Login対応アプリは空欄で利用します。この版はシステムユーザートークン用の構成を対象にしていません。
- アカウント固有のInstagram ID／アクセストークンを追加用に手入力する必要はありません。

`APP_SECRET_KEY` は32文字以上のランダム値で、初期値のままにしないでください。
既に十分なランダム値ならそのまま使用します。新しく生成する場合：

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

通常はこの既存シークレットから、認証情報の暗号鍵を自動導出します。
認証情報保存後に `APP_SECRET_KEY` を変更すると、暗号鍵の未指定時は保存済みトークンが読めなくなります。アカウント管理から再認証してください。

暗号鍵をアプリシークレットから分けたい場合は、**最初の認証前に**次のコマンドで生成した値を `ACCOUNT_TOKEN_ENCRYPTION_KEY` に設定します。

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

この鍵も保存後は変更しないでください。Webとcronは同じ `.env` と暗号鍵を使用します。バックアップから復元する際にも必要です。

## 3. Meta開発者画面での設定

[Meta for Developers](https://developers.facebook.com/apps/)で対象アプリを開き、Instagram API with Facebook Login／Facebook Login for Businessの設定を確認します。

認証後の戻り先として、**有効なOAuthリダイレクトURI**へ次を登録します：

```text
https://instagram.example.com/accounts/oauth/callback
```

URLが異なる場合は、アカウント管理画面の「初回のMeta設定」に表示されるURLをそのまま登録します。末尾のスラッシュ、HTTPS、サブパスを含め完全一致させてください。

必要な権限：

```text
pages_show_list
pages_read_engagement
instagram_basic
instagram_content_publish
```

Facebook Login for Businessの構成では、上記権限を構成側へ含めてください。
この版はFacebookページに接続されたInstagramプロアカウント（ビジネス／クリエイター）を対象にします。Facebookページを管理できる人がログインし、対象ページとInstagramのアクセスを許可します。

開発モードやStandard Accessでは対象ユーザーにアプリロールなどの制約があります。アプリロール外の利用者を接続する場合は、Meta側の公開設定・必要なAdvanced Access／審査が整っているか確認してください。アプリからの認証だけでMeta側の制約が解除されることはありません。

公式参考：

- https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-facebook-login/get-started
- https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-facebook-login/business-login-for-instagram
- https://developers.facebook.com/documentation/facebook-login/guides/access-tokens/get-long-lived
- https://www.postman.com/meta/instagram/collection/6yqw8pt/instagram-api

## 4. 再起動と確認

.envを保存したら、現在の起動方式に合わせてアプリを再起動してください。DBの追加テーブルは起動時に自動作成されます。既存の写真・投稿テーブルを削除する処理はありません。

1. 管理画面にログインし、ヘッダーの「アカウント管理」を開く。
2. 二九写真店とCrach29の「設定」をそれぞれ開き、現在のinbox／postedが表示されることを確認する。
3. 必要に応じてフォルダURLまたはID、表示名、投稿文の基本指示を変更して保存する。
4. 新規アカウントを追加する場合は「Meta認証でアカウントを追加」を押す。
5. Facebookへログインし、対象のページとInstagramを選び、必要な権限を許可する。
6. 戻ってきた画面でアカウントを選択し保存する。
7. 新規アカウントの設定画面で、表示名、inbox、posted、投稿文の基本指示を設定する。
8. 「投稿画面へ」を開き、対象アカウントへ切り替えられることを確認する。
9. Driveに検証用の写真を用意して読み込み、投稿先と文章を確認する。
10. 実投稿は公開してよい検証写真・文章で1件実施し、Instagram掲載とDrive移動を確認する。
11. 予約投稿cronを再開する。

Drive接続はこれまでのGoogle OAuthを共有します。別企業のフォルダは、アプリに接続済みのGoogleアカウントへ共有してください。Instagram認証によりGoogle Driveが自動認証されるわけではありません。

inboxとpostedは別のフォルダにします。他アカウントで既に使用しているフォルダは重複指定できません。新規登録時に空欄で保存した場合は、写真の読み込み・投稿を始める前に設定してください。

管理画面で保存したDrive設定はSQLiteの`instagram_account_settings`に保存します。`.env`の既存値は削除・変更しません。管理画面の値があるアカウントではDB側を優先するため、二九写真店・Crach29も再起動後に同じ設定を使用します。

## 5. 再認証と有効期限

アカウント管理の「再認証」から、そのアカウントと同じInstagramを選択します。別のInstagramへ投稿履歴が移ることを防ぐため、IG IDが登録済みの場合は異なるIDを選べません。

長期ユーザートークンを取得し、Metaが返すトークン期限とデータアクセス期限の早い方を表示します。認証の無期限化・期限の自動延長は行いません。期限が近づいた場合、またはMeta側で許可が取り消された場合は再認証してください。期限表示がなくてもMeta側で無効化される場合があります。

アカウント管理の「認証済み」は保存済みの認証状態です。ページ表示のたびにMetaへ接続するライブ診断は行いません。期限前の権限取消・失効はAPI実行時に検出されます。

認証途中の候補選択は10分で失効します。複数アカウントを追加する場合は、1件保存するごとに追加ボタンから認証し直します。

## 6. 検証結果と実環境で残る確認

自動テスト44件が成功しました（Python 3.12 / Flask 3.0.3 / cryptography 46）。`python -m unittest discover -s tests -v` で再実行できます。
実アプリのFlaskテストクライアントでも、ログイン→一覧→候補選択→保存→設定変更→投稿画面の切替→ログアウトを確認しました。実ブラウザによる表示確認は未実施です。
二九写真店とCrach29に相当する既存`.env`アカウントについても、Meta認証前のDrive設定保存、再読込後のDB設定優先、Instagram認証情報の維持を実アプリ構成で確認しました。認証の成功・取消・拒否、CSRF、不正state、別セッション、期限切れ、再送信、候補改ざん、暗号保存、既存アカウントへの再接続、履歴・予約維持、Drive設定、cron参照、API権限不足をモックで検証しました。

実際のMetaログイン、アプリ審査・権限、ユーザーごとのページ管理権限、実Instagram投稿はこの開発環境では未検証です。適用後に手順4で確認してください。

## 7. 公開ディレクトリとバックアップ

DB・.env・Google認証JSONは公開対象にしないでください。現在のアプリ配信設定を維持し、アプリのコード・data・credentialsを静的ファイルとして丸ごと公開しないようにします。
暗号化されたSQLite DBにも投稿履歴が入ります。既存のバックアップを継続し、.envの鍵とDBを両方保管してください。

## 8. アカウント設定だけを完全に初期化する

`.env`のアカウント固有Instagram ID・アクセストークンを空にし、SQLiteの次の3テーブルだけを空にします。

- `instagram_accounts`
- `instagram_account_settings`
- `instagram_oauth_flows`

写真・投稿案・投稿履歴・予約のテーブルは削除しません。初期化後はトップ画面からアカウント管理へ移動し、「登録済みアカウントはありません」と表示します。

二九写真店の`29shashinten`、Crach29の`crach.29`、美趣の`bisyu_1995`を再認証すると、従来のアカウントキーへ自動的に結び直します。

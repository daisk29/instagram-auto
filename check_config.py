from config import Config
from account_store import AccountStore
from database import Database
from meta_oauth import MetaOAuth
from datetime import datetime, timezone


def main():
    common_checks = [
        ("管理パスワード", bool(Config.ADMIN_PASSWORD)),
        ("公開URL", Config.PUBLIC_BASE_URL.startswith("https://")),
        ("Google OAuth JSON", Config.GOOGLE_CREDENTIALS_FILE.is_file()),
        ("Google token.json", Config.GOOGLE_TOKEN_FILE.is_file()),
        ("OpenAI APIキー", bool(Config.OPENAI_API_KEY)),
    ]
    account_checks = []
    Config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    database = Database(Config.DATABASE_PATH)
    database.init()
    store = AccountStore(Config, database)
    for account in store.profiles().values():
        prefix = f"@{account.username}"
        account_checks.extend(
            [
                (f"{prefix} Drive inbox ID", bool(account.drive_inbox_folder_id)),
                (f"{prefix} Drive posted ID", bool(account.drive_posted_folder_id)),
                (f"{prefix} Instagram User ID", bool(account.instagram_ig_user_id)),
                (f"{prefix} Instagramアクセストークン", bool(account.instagram_access_token)),
            ]
        )
        if account.token_expires_at:
            account_checks.append((f"{prefix} 認証期限", datetime.fromisoformat(account.token_expires_at) > datetime.now(timezone.utc)))
    oauth_error = MetaOAuth(Config).configuration_error() or store.configuration_error()
    print("アプリ内Meta認証: " + (oauth_error or "設定OK"))
    checks = common_checks + account_checks
    for label, ok in checks:
        print(("OK  " if ok else "NG  ") + label)
    if not all(ok for _, ok in checks):
        raise SystemExit("未設定項目があります。アカウント管理画面・.env・credentialsを確認してください。")
    print("設定確認完了")


if __name__ == "__main__":
    main()

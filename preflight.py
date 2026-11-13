"""Non-destructive preflight for shared LINE. Never prints credential values."""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE=Path(__file__).resolve().parent
load_dotenv(BASE/'.env',override=False)


def is_set(name):
    return bool(os.environ.get(name,'').strip())


def main():
    from config import Config
    from database import Database
    from account_store import AccountStore, AccountRegistry
    print('=== Shared LINE preflight ===')
    for name in ('SHARED_LINE_CHANNEL_SECRET','SHARED_LINE_CHANNEL_ACCESS_TOKEN',
                 'SHARED_LINE_ALLOWED_USER_IDS','SHARED_LINE_APPROVER_IDS',
                 'CRACH29_DRIVE_INBOX_FOLDER_ID','GOOGLE_INBOX_FOLDER_ID'):
        print(name, 'OK' if is_set(name) else 'MISSING')
    print('SHARED_LINE_ENABLE_POSTING',os.getenv('SHARED_LINE_ENABLE_POSTING','0')=='1')
    print('CRACH29/29 usernames, folder ID actual values are not shown.')
    app_db=Database(Config.DATABASE_PATH)
    app_db.init()
    registry=AccountRegistry(AccountStore(Config,app_db))
    print('Available Instagram accounts (keys/usernames only):')
    for k in registry:
        p=registry.get(k)
        print(' ',k, '@'+p.username)
    for key,env_key in [
            (os.getenv('SHARED_LINE_CRACH29_ACCOUNT_KEY','crach29'),'CRACH29_DRIVE_INBOX_FOLDER_ID'),
            (os.getenv('SHARED_LINE_PHOTO29_ACCOUNT_KEY','29shashinten'),'GOOGLE_INBOX_FOLDER_ID')]:
        profile=registry.get(key)
        print(key,'ACCOUNT OK' if profile else 'ACCOUNT MISSING')
        if profile:
            print('  inbox matches LINE:',profile.drive_inbox_folder_id==os.getenv(env_key,''))
            print('  posted folder configured:',bool(profile.drive_posted_folder_id))
            print('  Instagram API credentials configured:',bool(profile.instagram_ig_user_id and profile.instagram_access_token))
    from shared_line_bridge import database
    from shared_line_flow import initialize
    conn=database()
    initialize(conn)
    conn.close()
    print('Shared LINE SQLite schema: OK')

if __name__=='__main__':main()

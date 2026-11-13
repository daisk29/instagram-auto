"""Shared LINE orchestration. Run from cron each minute; safe to run concurrently.

Photos are first uploaded by shared_line_worker.py; this process creates drafts,
processes confirmation, and publishes scheduled drafts using existing publisher.
"""
import fcntl
import json
import hashlib
import logging
import os
import secrets
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone

import requests
from shared_line_flow import (
    BRANDS, initialize, enqueue_message, text_message, proposal_messages,
    action_button, action_card, caption_hash,
)

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger('shared-line-flow')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')


def connect():
    from shared_line_bridge import database
    conn = database()
    initialize(conn)
    conn.execute('PRAGMA busy_timeout=15000')
    return conn


def app_context():
    from config import Config
    from database import Database
    from account_store import AccountStore, AccountRegistry
    Config.MEDIA_DIR.mkdir(parents=True,exist_ok=True)
    db = Database(Config.DATABASE_PATH)
    db.init()
    accounts = AccountRegistry(AccountStore(Config, db))
    return Config, db, accounts


def expected_account(group):
    return (os.getenv('SHARED_LINE_CRACH29_ACCOUNT_KEY','crach29').strip()
            if group['brand']=='crach29' else os.getenv('SHARED_LINE_PHOTO29_ACCOUNT_KEY','29shashinten').strip())


def get_account(conn, group, accounts):
    from shared_line_bridge import folder_for
    account = accounts.get(expected_account(group))
    if not account:
        raise RuntimeError('Instagramアカウントが見つかりません')
    expected = folder_for(group['brand'])
    if not expected or expected != account.drive_inbox_folder_id:
        raise RuntimeError('LINEとInstagramのDrive保存先設定が一致していません')
    return account


def photo_ids_for_group(conn, group, database, config, account):
    from drive_client import DriveClient
    from image_prepare import prepare_instagram_image
    from tempfile import NamedTemporaryFile
    ids = [x['message_id'] for x in conn.execute(
        'SELECT message_id FROM line_flow_items WHERE group_id=? ORDER BY rowid',(group['id'],))]
    if not 1 <= len(ids) <= 10:
        raise RuntimeError('写真は1投稿あたり1〜10枚にしてください')
    marks = ','.join('?' for _ in ids)
    jobs = conn.execute('SELECT * FROM jobs WHERE message_id IN ('+marks+')', ids).fetchall()
    by_mid = {j['message_id']: j for j in jobs}
    if any(mid not in by_mid or by_mid[mid]['status']!='done' or not by_mid[mid]['google_file_id'] for mid in ids):
        return None  # Upload in progress; retry on next worker run.
    drive = DriveClient(config, account)
    photo_ids = []
    for mid in ids:
        job = by_mid[mid]
        file_id = job['google_file_id']
        # Never mutate existing, already-drafted records.
        with database.connect() as c:
            found = c.execute('SELECT id,account_key FROM photos WHERE drive_file_id=?',(file_id,)).fetchone()
        if found:
            if found['account_key'] != account.key:
                raise RuntimeError('写真が別アカウントに紐付いています')
            photo_ids.append(found['id'])
            continue
        item = drive.service().files().get(fileId=file_id, fields='id,name,mimeType,modifiedTime,size').execute()
        if item['mimeType'] not in ('image/jpeg','image/png','image/webp','image/heic'):
            raise RuntimeError('投稿に対応していない画像形式です')
        local_path = config.MEDIA_DIR / f'{file_id}.jpg'
        with NamedTemporaryFile(suffix='.source', delete=True) as source:
            drive._download(drive.service(), file_id, Path(source.name))
            prepare_instagram_image(source.name, local_path)
        item['mimeType']='image/jpeg'
        database.upsert_photo(item,local_path,secrets.token_urlsafe(32),account.key)
        with database.connect() as c:
            found = c.execute('SELECT id FROM photos WHERE drive_file_id=?',(file_id,)).fetchone()
        photo_ids.append(found['id'])
    return photo_ids


def create_proposal(conn, group, config, database, accounts):
    from caption_client import CaptionClient
    now = int(time.time())
    account = get_account(conn,group,accounts)
    photo_ids = photo_ids_for_group(conn,group,database,config,account)
    if photo_ids is None:
        return False
    # Recover if a previous process inserted a draft before storing its id in our DB.
    draft_id = group['draft_id']
    if not draft_id:
        wanted = json.dumps(photo_ids)
        for d in database.list_drafts(account.key):
            if d['photo_ids']==wanted:
                draft_id = d['id']
                break
    if draft_id:
        draft = database.get_draft(draft_id)
        if not draft or draft['account_key'] != account.key:
            raise RuntimeError('既存投稿案との紐付けが不正です')
        caption = draft['caption']
    else:
        photos = database.get_photos(photo_ids)
        caption = CaptionClient(config,account).generate(photos)
        draft_id = database.create_draft(photo_ids,caption,account.key)
    conn.execute('''UPDATE line_flow_groups SET stage='ready',draft_id=?,closed=1,
        preview_hash=?,last_error=NULL,updated_at=? WHERE id=?''',
        (draft_id,caption_hash(caption),now,group['id']))
    conn.commit()
    row = conn.execute('SELECT * FROM line_flow_groups WHERE id=?',(group['id'],)).fetchone()
    enqueue_message(conn,group['line_user_id'],*proposal_messages(row,caption))
    LOG.info('Draft %s created for group %s',draft_id,group['id'])
    return True


def check_publish_ready(account):
    if os.getenv('SHARED_LINE_ENABLE_POSTING','0') != '1':
        raise RuntimeError('LINEからの投稿は未有効化です。SHARED_LINE_ENABLE_POSTING=1 が必要です')
    if not account.instagram_ig_user_id or not account.instagram_access_token:
        raise RuntimeError('Instagramの接続設定が未完了です。管理画面でアカウントを確認してください')
    if not account.drive_posted_folder_id:
        raise RuntimeError('Instagramの投稿後フォルダが未設定です')


def handle_action(conn, row, config, database, accounts):
    now = int(time.time())
    group = conn.execute('SELECT * FROM line_flow_groups WHERE id=?',(row['group_id'],)).fetchone()
    if not group:
        return
    uid = group['line_user_id']
    account = get_account(conn, group, accounts)
    draft_id = group['draft_id']
    draft = database.get_draft(draft_id) if draft_id else None
    operation = row['operation']
    if not draft or draft['account_key'] != account.key:
        raise RuntimeError('操作対象の投稿案がありません')
    if operation=='preview':
        if group['preview_hash'] != caption_hash(draft['caption']):
            conn.execute('UPDATE line_flow_groups SET preview_hash=?,version=version+1 WHERE id=?',
                         (caption_hash(draft['caption']),group['id']))
            conn.commit()
            group=conn.execute('SELECT * FROM line_flow_groups WHERE id=?',(group['id'],)).fetchone()
        enqueue_message(conn,uid,*proposal_messages(group,draft['caption']))
        return
    if operation=='replace':
        if group['stage']!='editing' or draft['status'] in ('posted','publishing','committing','scheduled'):
            return
        caption = row['instruction'].strip()
        database.update_draft(draft_id,caption=caption,status='draft',error_message=None)
        database.mark_photos(json.loads(draft['photo_ids']),'review')
        database.event(draft_id,'line_caption_edited')
        conn.execute("UPDATE line_flow_groups SET stage='ready',preview_hash=?,updated_at=? WHERE id=?",(caption_hash(caption),now,group['id']))
        conn.commit()
        group = conn.execute('SELECT * FROM line_flow_groups WHERE id=?',(group['id'],)).fetchone()
        enqueue_message(conn,uid,*proposal_messages(group,caption))
    elif operation=='regenerate':
        if group['stage']!='regenerating' or draft['status'] in ('posted','publishing','committing','scheduled'):
            return
        from caption_client import CaptionClient
        photos = database.get_photos(json.loads(draft['photo_ids']))
        caption = CaptionClient(config,account).generate(photos,row['instruction'])
        database.update_draft(draft_id,caption=caption,status='draft',error_message=None)
        database.mark_photos(json.loads(draft['photo_ids']),'review')
        database.event(draft_id,'line_caption_regenerated')
        conn.execute("UPDATE line_flow_groups SET stage='ready',preview_hash=?,updated_at=? WHERE id=?",(caption_hash(caption),now,group['id']))
        conn.commit()
        group = conn.execute('SELECT * FROM line_flow_groups WHERE id=?',(group['id'],)).fetchone()
        enqueue_message(conn,uid,*proposal_messages(group,caption))
    elif operation=='hold':
        if group['stage']=='hold' and draft['status'] in ('draft','hold','error','cancelled'):
            database.update_draft(draft_id,status='hold')
            database.mark_photos(json.loads(draft['photo_ids']),'hold')
            database.event(draft_id,'line_held')
            enqueue_message(conn,uid,text_message('投稿案を保留しました。'))
    elif operation=='book':
        if group['stage']!='schedule_queued':
            return
        check_publish_ready(account)
        if group['preview_hash'] != caption_hash(draft['caption']):
            raise RuntimeError('キャプションが管理画面で変更されています。LINEで再確認してください')
        scheduled_at = group['pending_schedule']
        if not scheduled_at or datetime.fromisoformat(scheduled_at) <= datetime.now(timezone.utc):
            raise RuntimeError('予約日時が過ぎています。再設定してください')
        if not database.schedule_draft(draft_id,draft['caption'],scheduled_at):
            raise RuntimeError('投稿中・投稿済みのため予約できません')
        database.mark_photos(json.loads(draft['photo_ids']),'scheduled')
        database.event(draft_id,'line_scheduled', scheduled_at)
        conn.execute("UPDATE line_flow_groups SET stage='scheduled',updated_at=? WHERE id=?",(now,group['id']))
        conn.commit()
        local = datetime.fromisoformat(scheduled_at).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Tokyo'))
        enqueue_message(conn,uid,text_message(f'予約しました：{local:%Y/%m/%d %H:%M}（日本時間）'),
           action_card('予約管理','予約を取り消す場合はこちら',[
             action_button('予約を取り消す','cancelbook',group['id'],group['version'])]))
    elif operation=='cancelbook':
        if group['stage']!='cancel_queued':
            return
        if database.cancel_schedule(draft_id):
            database.mark_photos(json.loads(draft['photo_ids']),'review')
            database.event(draft_id,'line_schedule_cancelled')
            conn.execute("UPDATE line_flow_groups SET stage='ready',pending_schedule=NULL,updated_at=?,version=version+1 WHERE id=?",
                         (now,group['id']))
            conn.commit()
            enqueue_message(conn,uid,text_message('予約を取り消しました。'))
        else:
            raise RuntimeError('投稿がすでに開始されたため、予約は取り消せません')
    elif operation=='publish':
        if group['stage']!='publish_queued':
            return
        check_publish_ready(account)
        if group['preview_hash'] != caption_hash(draft['caption']):
            raise RuntimeError('キャプションが管理画面で変更されています。LINEで再確認してください')
        if not database.claim_publish(draft_id,draft['caption']):
            raise RuntimeError('投稿済み、または他の処理が開始済みです')
        conn.execute("UPDATE line_flow_groups SET stage='publishing',updated_at=? WHERE id=?",(now,group['id']))
        conn.commit()
        from publish_service import publish_claimed_draft
        publish_claimed_draft(database,config,accounts,draft_id)
        # The result is picked up by sync_published(), including if a later step fails.
    else:
        LOG.warning('Unknown LINE operation %s', operation)


def sync_published(conn, database):
    now = int(time.time())
    rows = conn.execute('''SELECT * FROM line_flow_groups
         WHERE draft_id IS NOT NULL AND stage NOT IN ('posted','failed')''').fetchall()
    for group in rows:
        draft = database.get_draft(group['draft_id'])
        if not draft:
            continue
        if draft['status']=='posted':
            conn.execute("UPDATE line_flow_groups SET stage='posted',updated_at=? WHERE id=?",(now,group['id']))
            conn.commit()
            body = f"Instagram投稿が完了しました（{BRANDS[group['brand']]}）。"
            if draft['permalink']:
                body += '\n'+draft['permalink']
            enqueue_message(conn,group['line_user_id'],text_message(body))
        elif draft['status']=='error' and group['stage'] in (
            'scheduled','publishing','publish_queued','schedule_queued'):
            conn.execute("UPDATE line_flow_groups SET stage='failed',updated_at=? WHERE id=?",(now,group['id']))
            conn.commit()
            enqueue_message(conn,group['line_user_id'],text_message(
                'Instagramへの投稿でエラーが発生しました。管理画面で状態を確認してください。'))


def run_due_schedules(conn,config,database,accounts):
    if os.getenv('SHARED_LINE_ENABLE_POSTING','0') != '1':
        return
    from publish_service import publish_claimed_draft
    from database import utcnow
    due = utcnow()
    for group in conn.execute("SELECT * FROM line_flow_groups WHERE stage='scheduled' AND draft_id IS NOT NULL LIMIT 20").fetchall():
        draft = database.get_draft(group['draft_id'])
        if not draft or draft['status']!='scheduled' or not draft['scheduled_at'] or draft['scheduled_at']>due:
            continue
        # Atomic claim shared with the existing scheduler; only one process publishes.
        if database.claim_scheduled_publish(draft['id'],due):
            conn.execute("UPDATE line_flow_groups SET stage='publishing',updated_at=? WHERE id=?",(int(time.time()),group['id']))
            conn.commit()
            try:
                publish_claimed_draft(database,config,accounts,draft['id'])
            except Exception:
                LOG.exception('Scheduled Instagram publish failed for draft %s',draft['id'])


def send_outbox(conn):
    token = os.environ.get('SHARED_LINE_CHANNEL_ACCESS_TOKEN','').strip()
    if not token:
        LOG.error('SHARED_LINE_CHANNEL_ACCESS_TOKEN is unset')
        return
    now = int(time.time())
    rows = conn.execute("SELECT * FROM line_flow_outbox WHERE status='queued' AND run_after<=? ORDER BY id LIMIT 20",(now,)).fetchall()
    for row in rows:
        payload = json.loads(row['payload'])
        request_body = {'to':row['line_user_id'],'messages':[payload]}
        key_input = f'shared-line-outbox:{row["id"]}:{row["created_at"]}:' + hashlib.sha256(row['payload'].encode()).hexdigest()
        retry_key = str(uuid.uuid5(uuid.NAMESPACE_URL,key_input))
        try:
            result = requests.post('https://api.line.me/v2/bot/message/push',json=request_body,
                 headers={'Authorization':'Bearer '+token,'X-Line-Retry-Key':retry_key},timeout=(5,15))
            if result.status_code not in (200,409):
                result.raise_for_status()
            conn.execute('DELETE FROM line_flow_outbox WHERE id=?',(row['id'],))
            conn.commit()
        except Exception as exc:
            attempts = row['attempts']+1
            failed = attempts >= 8
            conn.execute('''UPDATE line_flow_outbox SET status=?,attempts=?,run_after=?,last_error=? WHERE id=?''',
                         ('failed' if failed else 'queued', attempts,
                          now+min(3600,30*(2**min(attempts,6))),str(exc)[:300],row['id']))
            conn.commit()
            LOG.exception('Failed to push LINE notification id=%s',row['id'])
            # Do not starve other users if one recipient has blocked the bot.
            continue


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env', override=False)
    lock_dir = ROOT/'.shared-line-data'
    lock_dir.mkdir(mode=0o700,exist_ok=True)
    with (lock_dir/'flow.lock').open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.info('LINE flow worker already running')
            return
        conn = connect()
        try:
            if len(sys.argv)>1 and sys.argv[1]=='status':
                for row in conn.execute('SELECT stage,count(*) AS n FROM line_flow_groups GROUP BY stage ORDER BY stage'):
                    print(f'{row["stage"]}: {row["n"]}')
                for row in conn.execute('SELECT status,count(*) AS n FROM line_flow_outbox GROUP BY status ORDER BY status'):
                    print(f'line_notifications_{row["status"]}: {row["n"]}')
                print('posting_enabled:', os.getenv('SHARED_LINE_ENABLE_POSTING','0')=='1')
                return
            config,database,accounts = app_context()
            now = int(time.time())
            for group in conn.execute('''SELECT * FROM line_flow_groups
                    WHERE stage='collecting' AND ready_at<=? ORDER BY id LIMIT 10''',(now,)).fetchall():
                try:
                    create_proposal(conn,group,config,database,accounts)
                except Exception as exc:
                    LOG.exception('Could not create LINE draft for group %s',group['id'])
                    conn.execute('''UPDATE line_flow_groups SET stage='failed',last_error=?,updated_at=? WHERE id=?''',
                                 (str(exc)[:500],now,group['id']))
                    conn.commit()
                    enqueue_message(conn,group['line_user_id'],text_message('投稿案の生成に失敗しました。管理者に確認してください。'))
            for row in conn.execute("SELECT * FROM line_flow_actions WHERE status='queued' ORDER BY id LIMIT 10").fetchall():
                conn.execute("UPDATE line_flow_actions SET status='processing',updated_at=? WHERE id=? AND status='queued'",(now,row['id']))
                conn.commit()
                try:
                    handle_action(conn,row,config,database,accounts)
                    conn.execute("UPDATE line_flow_actions SET status='done',updated_at=? WHERE id=?",(int(time.time()),row['id']))
                    conn.commit()
                except Exception as exc:
                    LOG.exception('LINE action failed: %s',row['id'])
                    conn.execute("UPDATE line_flow_actions SET status='failed',updated_at=? WHERE id=?",(int(time.time()),row['id']))
                    conn.commit()
                    group = conn.execute('SELECT * FROM line_flow_groups WHERE id=?',(row['group_id'],)).fetchone()
                    if group:
                        if row['operation'] in ('book','publish','replace','regenerate','cancelbook'):
                            draft = database.get_draft(group['draft_id']) if group['draft_id'] else None
                            # Never undo a successful or in-flight publish on error.
                            if not draft or draft['status'] not in ('posted','publishing','committing','error'):
                                original = 'scheduled' if row['operation']=='cancelbook' else 'ready'
                                conn.execute('UPDATE line_flow_groups SET stage=?,last_error=?,updated_at=? WHERE id=?',
                                     (original,str(exc)[:500],int(time.time()),group['id']))
                                conn.commit()
                        enqueue_message(conn,group['line_user_id'],text_message('処理を完了できませんでした。管理画面で状態を確認してください。'))
            run_due_schedules(conn,config,database,accounts)
            sync_published(conn,database)
            send_outbox(conn)
        finally:
            conn.close()


if __name__ == '__main__':
    main()

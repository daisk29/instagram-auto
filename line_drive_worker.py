"""Cron worker: downloads queued images from LINE and uploads to Google Drive."""
from __future__ import annotations

import fcntl
import logging
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from line_drive_common import db, data_dir, get_setting

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('line-drive-worker')


def credentials():
    token_path = get_setting('LINE_DRIVE_GOOGLE_TOKEN_PATH')
    if not token_path or not Path(token_path).is_file():
        raise RuntimeError('LINE_DRIVE_GOOGLE_TOKEN_PATH is not an existing OAuth token JSON file')
    creds = Credentials.from_authorized_user_file(token_path)
    if not creds.valid:
        if not creds.refresh_token:
            raise RuntimeError('Google OAuth refresh_token is missing; reauthorize with offline access')
        creds.refresh(Request())
    return creds


def drive_service():
    return build('drive', 'v3', credentials=credentials(), cache_discovery=False)


def claim_job(connection: sqlite3.Connection):
    now = int(time.time())
    # Crash recovery. The Google-side appProperties check avoids re-upload if the prior upload finished.
    connection.execute("UPDATE jobs SET status='retry', run_after=?, updated_at=? WHERE status='processing' AND updated_at < ?",
                       (now, now, now - 900))
    connection.commit()
    connection.execute('BEGIN IMMEDIATE')
    row = connection.execute("""SELECT * FROM jobs WHERE status IN ('queued','retry') AND run_after <= ?
                                ORDER BY created_at ASC LIMIT 1""", (now,)).fetchone()
    if row is None:
        connection.commit()
        return None
    connection.execute("UPDATE jobs SET status='processing', attempts=attempts+1, updated_at=? WHERE message_id=?",
                       (now, row['message_id']))
    connection.commit()
    return dict(row)


def find_existing(service, folder_id: str, message_id: str):
    # IDs are restricted to [-_A-Za-z0-9] by the webhook.
    query = (f"'{folder_id}' in parents and trashed = false and "
             f"appProperties has {{ key='line_message_id' and value='{message_id}' }}")
    result = service.files().list(q=query, pageSize=1, fields='files(id,name)',
                                   supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    items = result.get('files', [])
    return items[0] if items else None


def download_line_image(message_id: str) -> tuple[str, str, str]:
    access_token = get_setting('LINE_CHANNEL_ACCESS_TOKEN')
    if not access_token:
        raise RuntimeError('LINE_CHANNEL_ACCESS_TOKEN is missing')
    max_bytes = int(get_setting('LINE_DRIVE_MAX_MB', '25')) * 1024 * 1024
    resp = requests.get(f'https://api-data.line.me/v2/bot/message/{message_id}/content',
                        headers={'Authorization': f'Bearer {access_token}'},
                        stream=True, timeout=(10, 45))
    resp.raise_for_status()
    try:
        content_type = resp.headers.get('Content-Type', 'application/octet-stream').split(';')[0].lower()
        fd, filename = tempfile.mkstemp(prefix='image-', suffix='.tmp', dir=data_dir())
        os.chmod(filename, 0o600)
        try:
            size = 0
            with os.fdopen(fd, 'wb') as out:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        size += len(chunk)
                        if size > max_bytes:
                            raise ValueError('LINE image exceeded configured size limit')
                        out.write(chunk)
            with open(filename, 'rb') as reader:
                first = reader.read(32)
            if first.startswith(b'\xff\xd8\xff'):
                media_type, extension = 'image/jpeg', '.jpg'
            elif first.startswith(b'\x89PNG\r\n\x1a\n'):
                media_type, extension = 'image/png', '.png'
            elif first.startswith(b'RIFF') and first[8:12] == b'WEBP':
                media_type, extension = 'image/webp', '.webp'
            elif first.startswith((b'GIF87a', b'GIF89a')):
                media_type, extension = 'image/gif', '.gif'
            elif first[4:12] in (b'ftypheic', b'ftypheix', b'ftypmif1'):
                media_type, extension = 'image/heic', '.heic'
            else:
                raise ValueError('LINE response is not a recognized image format: ' + content_type)
            if size == 0:
                raise ValueError('LINE image is empty')
            return filename, media_type, extension
        except Exception:
            try:
                os.unlink(filename)
            except OSError:
                pass
            raise
    finally:
        resp.close()


def upload_image(service, job: dict):
    existing = find_existing(service, job['folder_id'], job['message_id'])
    if existing:
        return existing['id'], existing.get('name', '')
    tmp_file, media_type, extension = download_line_image(job['message_id'])
    try:
        timestamp = datetime.fromtimestamp(job['created_at'], timezone.utc).astimezone(ZoneInfo('Asia/Tokyo')).strftime('%Y%m%d_%H%M%S')
        filename = f'LINE_{timestamp}_{job["message_id"][-12:]}{extension}'
        metadata = {
            'name': filename,
            'parents': [job['folder_id']],
            'appProperties': {'line_message_id': job['message_id']},
        }
        media = MediaFileUpload(tmp_file, mimetype=media_type, resumable=True, chunksize=2 * 1024 * 1024)
        result = service.files().create(body=metadata, media_body=media,
                        supportsAllDrives=True, fields='id,name').execute()
        return result['id'], result.get('name', filename)
    finally:
        try:
            os.unlink(tmp_file)
        except OSError:
            pass


def push_user_message(user_id: str, text: str):
    token = get_setting('LINE_CHANNEL_ACCESS_TOKEN')
    resp = requests.post('https://api.line.me/v2/bot/message/push',
                         headers={'Authorization': f'Bearer {token}'},
                         json={'to': user_id, 'messages': [{'type': 'text', 'text': text[:4500]}]},
                         timeout=10)
    resp.raise_for_status()


def process_job(connection, service, job):
    now = int(time.time())
    message_id = job['message_id']
    try:
        file_id, filename = upload_image(service, job)
        connection.execute('''UPDATE jobs SET status='done', google_file_id=?,google_filename=?,
            last_error=NULL, updated_at=? WHERE message_id=?''', (file_id, filename, now, message_id))
        connection.commit()
        log.info('Uploaded LINE message %s to destination %s as %s', message_id, job['label'], file_id)
        if get_setting('LINE_DRIVE_NOTIFY_SUCCESS', '1') == '1':
            try:
                push_user_message(job['line_user_id'],
                                  f'Google Driveに保存しました。\n保存先：{job["label"]}\nファイル：{filename}')
            except Exception:
                log.exception('Success notice failed (the upload itself succeeded)')
    except Exception as exc:
        attempt = job['attempts'] + 1
        limit = max(1, int(get_setting('LINE_DRIVE_MAX_ATTEMPTS', '5')))
        status = 'failed' if attempt >= limit else 'retry'
        run_after = now + min(3600, 60 * (2 ** max(0, attempt - 1)))
        connection.execute('''UPDATE jobs SET status=?, attempts=?, run_after=?,
            updated_at=?, last_error=? WHERE message_id=?''',
            (status, attempt, run_after, now, str(exc)[:800], message_id))
        connection.commit()
        log.exception('Upload failure for LINE message %s (%s/%s)', message_id, attempt, limit)
        if status == 'failed':
            try:
                push_user_message(job['line_user_id'], '写真をGoogle Driveに保存できませんでした。管理者に確認してください。')
            except Exception:
                log.exception('Failure notice failed')


def sync_captions(connection, service, limit=50):
    """Store caption draft as Google Drive file description, including late-arriving text."""
    now = int(time.time())
    rows = connection.execute('''SELECT message_id,google_file_id,caption_text
        FROM jobs WHERE status='done' AND google_file_id IS NOT NULL
          AND caption_text IS NOT NULL AND caption_sync_pending=1
          AND caption_sync_after <= ? ORDER BY caption_at ASC LIMIT ?''',
        (now, limit)).fetchall()
    done = 0
    for row in rows:
        try:
            service.files().update(fileId=row['google_file_id'],
                body={'description': row['caption_text']},
                fields='id', supportsAllDrives=True).execute()
            connection.execute('''UPDATE jobs SET caption_sync_pending=0,
                caption_sync_after=0 WHERE message_id=? AND caption_text=?''',
                (row['message_id'], row['caption_text']))
            connection.commit()
            done += 1
        except Exception:
            log.exception('Failed to attach Drive caption metadata for LINE message %s', row['message_id'])
            connection.execute('''UPDATE jobs SET caption_sync_after=? WHERE message_id=?''',
                (int(time.time()) + 60, row['message_id']))
            connection.commit()
    return done


def main():
    base = data_dir()
    lock_path = base / 'worker.lock'
    with open(lock_path, 'a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info('Worker is already running; skipping')
            return 0
        connection = db()
        try:
            if len(sys.argv) > 1 and sys.argv[1] == 'status':
                for row in connection.execute('SELECT status,COUNT(*) AS num FROM jobs GROUP BY status ORDER BY status'):
                    print(f'{row["status"]}: {row["num"]}')
                waiting = connection.execute("SELECT COUNT(*) FROM jobs WHERE caption_sync_pending=1").fetchone()[0]
                print(f'caption_pending: {waiting}')
                for row in connection.execute("SELECT message_id,last_error FROM jobs WHERE status='failed' ORDER BY updated_at DESC LIMIT 5"):
                    print(f'failed {row["message_id"]}: {row["last_error"]}')
                return 0
            service = None
            processed = 0
            while processed < 20:
                job = claim_job(connection)
                if not job:
                    break
                if service is None:
                    try:
                        service = drive_service()
                    except Exception:
                        # Put the job back; a temporary configuration error should not silently lose photos.
                        connection.execute("UPDATE jobs SET status='retry', run_after=?,updated_at=? WHERE message_id=?",
                                           (int(time.time()) + 300, int(time.time()), job['message_id']))
                        connection.commit()
                        raise
                process_job(connection, service, job)
                processed += 1
            pending = connection.execute('''SELECT 1 FROM jobs WHERE status='done'
                AND caption_sync_pending=1 AND caption_sync_after <= ? LIMIT 1''',
                (int(time.time()),)).fetchone()
            captions_synced = 0
            if pending:
                if service is None:
                    service = drive_service()
                captions_synced = sync_captions(connection, service)
            log.info('Processed %d queued LINE image(s); synced %d caption(s)',
                     processed, captions_synced)
            return 0
        finally:
            connection.close()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        log.exception('Worker stopped due to fatal error')
        sys.exit(1)

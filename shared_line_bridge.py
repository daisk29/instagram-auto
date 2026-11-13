"""Shared LINE channel for Crach29/29shashinten; separate from BISYU.
Register in app.py: from shared_line_bridge import shared_line_blueprint; app.register_blueprint(shared_line_blueprint)
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from flask import Blueprint, request, abort, jsonify
from werkzeug.exceptions import HTTPException
from line_drive_common import enqueue_image

ROOT = Path(__file__).resolve().parent
BLUEPRINT = Blueprint('shared_line', __name__)
shared_line_blueprint = BLUEPRINT
LOG = logging.getLogger(__name__)
MESSAGE_ID = re.compile(r'[A-Za-z0-9_-]{5,80}')
FOLDER_ID = re.compile(r'[A-Za-z0-9_-]{10,160}')
LABELS = {'crach29': 'Crach29', 'photo29': '二九写真店'}


def setting(key):
    return os.environ.get(key, '').strip()


def folder_for(brand):
    key = 'CRACH29_DRIVE_INBOX_FOLDER_ID' if brand == 'crach29' else 'GOOGLE_INBOX_FOLDER_ID'
    return setting(key)


def private_dir():
    path = ROOT / '.shared-line-data'
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def database():
    conn = sqlite3.connect(str(private_dir() / 'jobs.sqlite3'), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=10000')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS jobs (
      message_id TEXT PRIMARY KEY, line_user_id TEXT NOT NULL,
      folder_id TEXT NOT NULL, label TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
      run_after INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
      google_file_id TEXT,google_filename TEXT,last_error TEXT,
      line_timestamp_ms INTEGER, caption_text TEXT,caption_at INTEGER,
      caption_sync_pending INTEGER NOT NULL DEFAULT 0,
      caption_sync_after INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS caption_events (
      message_id TEXT PRIMARY KEY, line_user_id TEXT NOT NULL,
      created_at INTEGER NOT NULL, photo_count INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS brand_state (
      line_user_id TEXT PRIMARY KEY, brand TEXT NOT NULL, updated_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS notices (
      id INTEGER PRIMARY KEY AUTOINCREMENT, line_user_id TEXT NOT NULL,
      body TEXT NOT NULL, created_at INTEGER NOT NULL
    );
    ''')
    conn.commit()
    return conn


def notice(conn, user, body):
    conn.execute('INSERT INTO notices(line_user_id,body,created_at) VALUES(?,?,?)', (user, body, int(time.time())))
    conn.commit()


def brand_get(conn, user):
    row = conn.execute('SELECT brand FROM brand_state WHERE line_user_id=?', (user,)).fetchone()
    return row['brand'] if row else None


def brand_set(conn, user, brand):
    conn.execute('''INSERT INTO brand_state(line_user_id,brand,updated_at)
        VALUES(?,?,?) ON CONFLICT(line_user_id) DO UPDATE SET
        brand=excluded.brand,updated_at=excluded.updated_at''', (user, brand, int(time.time())))
    conn.commit()


def allowed():
    return {v.strip() for v in setting('SHARED_LINE_ALLOWED_USER_IDS').split(',') if v.strip()}


def handle_event(conn, event):
    src = event.get('source') or {}
    if src.get('type') != 'user' or src.get('userId') not in allowed():
        return False
    uid = src['userId']
    if event.get('type') == 'postback':
        from shared_line_richmenu import handle_richmenu
        if handle_richmenu(conn, event, uid):
            return True
        from shared_line_flow import postback_received
        current = brand_get(conn, uid)
        return postback_received(conn, event, uid, current) if current else False
    if event.get('type') != 'message':
        return False
    msg = event.get('message') or {}
    mid = str(msg.get('id', ''))
    if not MESSAGE_ID.fullmatch(mid):
        return False
    typ = msg.get('type')
    ts = event.get('timestamp')
    if not isinstance(ts, int) or not 0 < ts < 10**15:
        ts = int(time.time() * 1000)
    if typ == 'text':
        value = str(msg.get('text') or '').strip()
        choices = {
            'ブランド Crach29': 'crach29', 'ブランド crach29': 'crach29',
            'ブランド 二九写真店': 'photo29', 'ブランド 29写真店': 'photo29',
        }
        brand = choices.get(value)
        if brand:
            folder = folder_for(brand)
            if not FOLDER_ID.fullmatch(folder):
                LOG.error('Missing Drive folder for %s', brand)
                notice(conn, uid, '保存先が未設定です。管理者に連絡してください。')
                return True
            brand_set(conn, uid, brand)
            notice(conn, uid, f'保存先を「{LABELS[brand]}」に切り替えました。写真を送信できます。')
            return True
        if value in ('現在のブランド', 'ヘルプ', 'ブランド一覧'):
            current = brand_get(conn, uid)
            suffix = f'現在：{LABELS[current]}' if current else '現在：未選択'
            notice(conn, uid, 'ブランド Crach29\nブランド 二九写真店\n写真の直後に文章を送るとキャプション原稿として保存します。\n' + suffix)
            return True
        current = brand_get(conn, uid)
        if not current:
            notice(conn, uid, '最初に「ブランド Crach29」または「ブランド 二九写真店」を送ってください。')
            return True
        from shared_line_flow import handle_text_command, caption_received
        if handle_text_command(conn, uid, current, value):
            return True
        changed = caption_received(conn, uid, current, mid, value, ts)
        if changed == -1:
            return True  # Duplicate LINE delivery was already processed.
        if changed:
            notice(conn, uid, f'キャプション原稿を写真{changed}枚に保存しました。')
            return True
        notice(conn, uid, '紐付け対象の写真がありません。写真を送った後3分以内に文章を送信してください。')
        return True
    if typ == 'image':
        current = brand_get(conn, uid)
        if not current:
            notice(conn, uid, '先にブランドを選択してください。「ブランド Crach29」または「ブランド 二九写真店」')
            return True
        folder = folder_for(current)
        if not FOLDER_ID.fullmatch(folder):
            notice(conn, uid, '保存先フォルダが未設定です。管理者に連絡してください。')
            return True
        added = enqueue_image(conn, uid, mid, folder, LABELS[current], ts)
        if added:
            from shared_line_flow import image_received
            image_received(conn, uid, current, mid)
        return added
    return False


def start_worker():
    py = ROOT / 'venv' / 'bin' / 'python'
    script = ROOT / 'shared_line_worker.py'
    fd = os.open(str(private_dir() / 'worker.log'), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        subprocess.Popen([str(py), str(script)], stdin=subprocess.DEVNULL,
                         stdout=fd, stderr=fd, cwd=str(ROOT),
                         close_fds=True, start_new_session=True)
    except OSError:
        LOG.exception('Failed to start shared LINE worker')
    finally:
        os.close(fd)
    # Text commands / postbacks do not need Google Drive. Launch the small
    # flow worker separately so the CGI webhook itself returns immediately.
    flow = ROOT / 'shared_line_flow_worker.py'
    if flow.exists():
        flow_fd = os.open(str(private_dir() / 'flow.log'), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            subprocess.Popen([str(py), str(flow)], stdin=subprocess.DEVNULL,
                             stdout=flow_fd, stderr=flow_fd, cwd=str(ROOT),
                             close_fds=True, start_new_session=True)
        except OSError:
            LOG.exception('Failed to start shared LINE flow worker')
        finally:
            os.close(flow_fd)


@BLUEPRINT.post('/line/shared/webhook')
def webhook():
    secret = setting('SHARED_LINE_CHANNEL_SECRET')
    token = setting('SHARED_LINE_CHANNEL_ACCESS_TOKEN')
    if not secret or not token:
        abort(503)
    raw = request.get_data(cache=False)
    if len(raw) > 1024 * 1024:
        abort(413)
    expected = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
    if not hmac.compare_digest(expected, request.headers.get('X-Line-Signature', '')):
        abort(403)
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or not isinstance(body.get('events'), list):
            abort(400)
        conn = database()
        try:
            dispatch = False
            for event in body['events']:
                dispatch = handle_event(conn, event) or dispatch
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception:
        LOG.exception('Shared LINE request failure')
        abort(500)
    if dispatch:
        start_worker()
    return jsonify(ok=True)

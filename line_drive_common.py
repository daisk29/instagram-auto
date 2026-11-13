"""Shared configuration and SQLite routines for LINE -> Google Drive bridge."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / '.env', override=False)


def get_setting(name: str, default: str = '') -> str:
    return os.environ.get(name, default).strip()


def allowed_users() -> set[str]:
    return {part.strip() for part in get_setting('LINE_ALLOWED_USER_IDS').split(',') if part.strip()}


def folder_aliases() -> dict[str, str]:
    raw = get_setting('LINE_DRIVE_FOLDERS_JSON', '{}')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('LINE_DRIVE_FOLDERS_JSON must be a JSON object')
    result = {}
    for name, folder_id in value.items():
        if not isinstance(name, str) or not isinstance(folder_id, str) or not FOLDER_ID.fullmatch(folder_id):
            raise ValueError('Invalid folder mapping: ' + str(name))
        result[name] = folder_id
    return result


FOLDER_ID = re.compile(r'[A-Za-z0-9_-]{10,160}')


def parse_folder_request(value: str, aliases: dict[str, str]) -> tuple[str, str] | None:
    """Resolve a configured alias, an explicit id:..., or a Google Drive folder URL."""
    if value in aliases:
        return value, aliases[value]
    if value.startswith('id:'):
        folder_id = value[3:].strip()
        if FOLDER_ID.fullmatch(folder_id):
            return '指定フォルダ', folder_id
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or parsed.netloc != 'drive.google.com':
            return None
        match = re.fullmatch(r'/drive(?:/u/\d+)?/folders/([A-Za-z0-9_-]{10,160})/?', parsed.path)
        if match:
            return '指定フォルダ', match.group(1)
    except ValueError:
        pass
    return None


def data_dir() -> Path:
    result = Path(get_setting('LINE_DRIVE_DATA_DIR', str(PROJECT_DIR / '.line-drive-data'))).expanduser()
    result.mkdir(mode=0o700, parents=True, exist_ok=True)
    return result


def database_path() -> Path:
    return data_dir() / 'jobs.sqlite3'


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(str(database_path()), timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA busy_timeout=15000')
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('''CREATE TABLE IF NOT EXISTS destinations (
        line_user_id TEXT PRIMARY KEY,
        folder_id TEXT NOT NULL,
        label TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    )''')
    connection.execute('''CREATE TABLE IF NOT EXISTS jobs (
        message_id TEXT PRIMARY KEY,
        line_user_id TEXT NOT NULL,
        folder_id TEXT NOT NULL,
        label TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0,
        run_after INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        google_file_id TEXT,
        google_filename TEXT,
        last_error TEXT,
        line_timestamp_ms INTEGER,
        caption_text TEXT,
        caption_at INTEGER,
        caption_sync_pending INTEGER NOT NULL DEFAULT 0,
        caption_sync_after INTEGER NOT NULL DEFAULT 0
    )''')
    # Add columns to existing jobs without discarding completed uploads.
    existing = {row['name'] for row in connection.execute('PRAGMA table_info(jobs)')}
    upgrades = {
        'line_timestamp_ms': 'INTEGER',
        'caption_text': 'TEXT',
        'caption_at': 'INTEGER',
        'caption_sync_pending': 'INTEGER NOT NULL DEFAULT 0',
        'caption_sync_after': 'INTEGER NOT NULL DEFAULT 0',
    }
    for column, definition in upgrades.items():
        if column not in existing:
            try:
                connection.execute(f'ALTER TABLE jobs ADD COLUMN {column} {definition}')
            except sqlite3.OperationalError as exc:
                if 'duplicate column name' not in str(exc).lower():
                    raise
    connection.execute('''CREATE TABLE IF NOT EXISTS caption_events (
        message_id TEXT PRIMARY KEY,
        line_user_id TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        photo_count INTEGER NOT NULL
    )''')
    connection.commit()
    return connection


def get_destination(connection: sqlite3.Connection, user_id: str) -> tuple[str, str] | None:
    row = connection.execute('SELECT folder_id,label FROM destinations WHERE line_user_id = ?', (user_id,)).fetchone()
    if row:
        return row['label'], row['folder_id']
    aliases = folder_aliases()
    default = get_setting('LINE_DRIVE_DEFAULT_FOLDER')
    if default in aliases:
        return default, aliases[default]
    return None


def save_destination(connection: sqlite3.Connection, user_id: str, label: str, folder_id: str) -> None:
    now = int(time.time())
    connection.execute('''INSERT INTO destinations (line_user_id,folder_id,label,updated_at)
        VALUES (?,?,?,?) ON CONFLICT(line_user_id) DO UPDATE SET
        folder_id=excluded.folder_id, label=excluded.label, updated_at=excluded.updated_at''',
        (user_id, folder_id, label, now))
    connection.commit()


def enqueue_image(connection: sqlite3.Connection, user_id: str, message_id: str,
                  folder_id: str, label: str, line_timestamp_ms: int | None = None) -> bool:
    now = int(time.time())
    timestamp_ms = line_timestamp_ms if line_timestamp_ms is not None else now * 1000
    cur = connection.execute('''INSERT OR IGNORE INTO jobs
        (message_id,line_user_id,folder_id,label,status,run_after,created_at,updated_at,line_timestamp_ms)
        VALUES (?,?,?,?,'queued',?,?,?,?)''',
        (message_id, user_id, folder_id, label, now, now, now, timestamp_ms))
    connection.commit()
    return cur.rowcount == 1


def attach_caption(connection: sqlite3.Connection, user_id: str, message_id: str,
                   caption: str, text_timestamp_ms: int, *, on_attach=None) -> int:
    """Attach text to the same user's recent uncaptioned images, once per LINE event.

    Multiple consecutive photos before one text receive the same caption.
    """
    if not caption:
        return 0
    max_chars = min(5000, max(1, int(get_setting('LINE_CAPTION_MAX_CHARS', '2200'))))
    caption = caption[:max_chars]
    now = int(time.time())
    window = min(600, max(10, int(get_setting('LINE_CAPTION_WINDOW_SECONDS', '180'))))
    # BEGIN IMMEDIATE serializes concurrent webhook deliveries and LINE redeliveries.
    connection.execute('BEGIN IMMEDIATE')
    try:
        cur = connection.execute('''INSERT OR IGNORE INTO caption_events
            (message_id, line_user_id, created_at, photo_count)
            VALUES (?,?,?,0)''', (message_id, user_id, now))
        if cur.rowcount == 0:
            connection.commit()
            return 0
        eligible = '''line_user_id=? AND caption_text IS NULL
              AND COALESCE(line_timestamp_ms, created_at*1000) BETWEEN ? AND ?
              AND status IN ('queued','retry','processing','done')'''
        bounds = (user_id, text_timestamp_ms - window * 1000, text_timestamp_ms)
        if on_attach is not None:
            photos = connection.execute(
                'SELECT message_id,folder_id FROM jobs WHERE ' + eligible
                + ' ORDER BY COALESCE(line_timestamp_ms, created_at*1000), rowid', bounds
            ).fetchall()
            if photos:
                on_attach(connection, user_id, photos)
        cur = connection.execute('''UPDATE jobs SET caption_text=?, caption_at=?,
            caption_sync_pending=1, caption_sync_after=0, updated_at=? WHERE ''' + eligible,
            (caption, now, now, *bounds))
        count = cur.rowcount
        connection.execute('UPDATE caption_events SET photo_count=? WHERE message_id=?',
                           (count, message_id))
        connection.commit()
        return count
    except Exception:
        connection.rollback()
        raise


def get_caption_for_drive_file(google_file_id: str) -> str | None:
    """Use inside the existing Instagram importer after locating a Drive file."""
    connection = db()
    try:
        row = connection.execute('''SELECT caption_text FROM jobs
            WHERE google_file_id=? AND status='done' LIMIT 1''', (google_file_id,)).fetchone()
        return row['caption_text'] if row else None
    finally:
        connection.close()

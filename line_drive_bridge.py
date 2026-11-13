"""BISYU-only LINE webhook: secure queue, fast CGI response, immediate background dispatch.

The existing app only needs its previous blueprint registration:
    from line_drive_bridge import line_drive_blueprint
    app.register_blueprint(line_drive_blueprint)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import subprocess

from flask import Blueprint, abort, jsonify, request

from line_drive_common import (FOLDER_ID, PROJECT_DIR, allowed_users, attach_caption,
                               data_dir, db, enqueue_image, get_setting)

log = logging.getLogger(__name__)
line_drive_blueprint = Blueprint('line_drive', __name__)
MESSAGE_ID = re.compile(r'[A-Za-z0-9_-]{5,80}')


def dispatch_worker() -> bool:
    """Never perform LINE download or Google upload inside the CGI request."""
    executable = Path(get_setting('LINE_DRIVE_PYTHON', str(PROJECT_DIR / 'venv' / 'bin' / 'python')))
    script = PROJECT_DIR / 'line_drive_worker.py'
    if not executable.is_file() or not script.is_file():
        log.error('LINE worker is missing: %s %s', executable, script)
        return False
    logfile = data_dir() / 'worker.log'
    try:
        # Exclude CGI stdout/stderr and stdin from the child; keep the parent response fast.
        fd = os.open(str(logfile), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            subprocess.Popen([str(executable), str(script)],
                             stdin=subprocess.DEVNULL, stdout=fd, stderr=fd,
                             cwd=str(PROJECT_DIR), close_fds=True, start_new_session=True)
        finally:
            os.close(fd)
        return True
    except OSError:
        log.exception('Immediate LINE worker launch failed; scheduled recovery can retry')
        return False


def _event_timestamp(event: dict) -> int:
    import time
    value = event.get('timestamp')
    if isinstance(value, int) and 0 < value < 10**15:
        return value
    return int(time.time() * 1000)


def process_event(connection, event: dict) -> bool:
    """Return True when an upload or caption update needs an asynchronous worker."""
    if event.get('type') != 'message':
        return False
    source = event.get('source') or {}
    if source.get('type') != 'user':
        return False  # Only one-to-one chats.
    user_id = source.get('userId')
    if not user_id or user_id not in allowed_users():
        if get_setting('LINE_DRIVE_LOG_UNAUTHORIZED_IDS', '0') == '1':
            log.warning('Unregistered LINE userId: %s', user_id)
        return False

    message = event.get('message') or {}
    message_type = message.get('type')
    message_id = str(message.get('id', ''))
    if not MESSAGE_ID.fullmatch(message_id):
        return False
    timestamp_ms = _event_timestamp(event)

    if message_type == 'image':
        folder_id = get_setting('BISYU_DRIVE_INBOX_FOLDER_ID')
        if not FOLDER_ID.fullmatch(folder_id):
            log.error('BISYU_DRIVE_INBOX_FOLDER_ID is missing or invalid')
            return False
        return enqueue_image(connection, user_id, message_id,
                             folder_id, '美趣', timestamp_ms)

    if message_type == 'text':
        # No folder selection: a following text is the caption draft, not a command.
        caption = str(message.get('text') or '').strip()
        linked = attach_caption(connection, user_id, message_id, caption, timestamp_ms)
        if linked:
            log.info('Caption linked to %d recent image(s)', linked)
        return linked > 0

    return False


@line_drive_blueprint.route('/line/webhook', methods=['POST'])
def line_drive_webhook():
    secret = get_setting('LINE_CHANNEL_SECRET')
    token = get_setting('LINE_CHANNEL_ACCESS_TOKEN')
    if not secret or not token:
        log.error('LINE channel configuration is missing')
        abort(503)
    if request.content_length is not None and request.content_length > 1024 * 1024:
        abort(413)
    raw = request.get_data(cache=False)
    if len(raw) > 1024 * 1024:
        abort(413)
    expected = base64.b64encode(hmac.new(secret.encode('utf-8'), raw,
                                            hashlib.sha256).digest()).decode('ascii')
    signature = request.headers.get('X-Line-Signature', '')
    if not hmac.compare_digest(expected, signature):
        abort(403)

    need_dispatch = False
    try:
        payload = json.loads(raw)
        connection = db()
        try:
            for event in payload.get('events', []):
                need_dispatch |= process_event(connection, event)
        finally:
            connection.close()
    except Exception:
        log.exception('LINE webhook processing failed')
        abort(500)

    if need_dispatch:
        dispatch_worker()
    return jsonify({'ok': True})

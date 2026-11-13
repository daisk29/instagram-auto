"""LINE-side state machine for shared Crach29 / 二九写真店 channel.

Kept separate from the existing 美趣 webhook, database and worker.
"""
import json
import logging
import os
import hashlib
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

LOG = logging.getLogger(__name__)
BRANDS = {'crach29': 'Crach29', 'photo29': '二九写真店'}
USER_ID = re.compile(r'^U[0-9a-fA-F]{32}$')
WINDOW_SECONDS = 180
MAX_PHOTOS = 10
JST = ZoneInfo('Asia/Tokyo')


def initialize(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS line_flow_groups (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      line_user_id TEXT NOT NULL, brand TEXT NOT NULL,
      stage TEXT NOT NULL DEFAULT 'collecting',
      closed INTEGER NOT NULL DEFAULT 0,
      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
      last_photo_at INTEGER NOT NULL, ready_at INTEGER NOT NULL,
      caption_text TEXT, draft_id INTEGER UNIQUE,
      preview_hash TEXT, version INTEGER NOT NULL DEFAULT 1,
      pending_schedule TEXT, last_error TEXT
    );
    CREATE INDEX IF NOT EXISTS line_flow_user ON line_flow_groups(line_user_id, brand, id);
    CREATE TABLE IF NOT EXISTS line_flow_items (
      message_id TEXT PRIMARY KEY, group_id INTEGER NOT NULL,
      FOREIGN KEY(group_id) REFERENCES line_flow_groups(id)
    );
    CREATE INDEX IF NOT EXISTS line_flow_item_group ON line_flow_items(group_id);
    CREATE TABLE IF NOT EXISTS line_flow_events (
      event_key TEXT PRIMARY KEY, received_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS line_flow_actions (
      id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL,
      operation TEXT NOT NULL, instruction TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'queued', created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS line_flow_outbox (
      id INTEGER PRIMARY KEY AUTOINCREMENT, line_user_id TEXT NOT NULL,
      payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
      created_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
      run_after INTEGER NOT NULL DEFAULT 0, last_error TEXT
    );
    ''')
    # Existing shared LINE SQLite files remain valid after future updates.
    fields = {r['name'] for r in conn.execute('PRAGMA table_info(line_flow_groups)')}
    if 'preview_hash' not in fields:
        conn.execute('ALTER TABLE line_flow_groups ADD COLUMN preview_hash TEXT')
    conn.commit()


def caption_hash(caption):
    return hashlib.sha256(caption.encode('utf-8')).hexdigest()


def text_message(text):
    return {'type': 'text', 'text': str(text)[:4900]}


def action_button(label, code, group_id, version):
    return {'type':'postback', 'label':label,
            'data':f'a={code}&g={group_id}&v={version}', 'displayText':label}


def action_card(title, body, actions):
    return {'type':'template', 'altText': title,
            'template':{'type':'buttons', 'title':title[:40],
                        'text': body[:160], 'actions':actions[:4]}}


def enqueue_message(conn, user, *messages):
    now = int(time.time())
    for message in messages:
        conn.execute('''INSERT INTO line_flow_outbox(line_user_id,payload,created_at,run_after)
             VALUES(?,?,?,?)''', (user, json.dumps(message, ensure_ascii=False), now, now))
    conn.commit()


def registered_event(conn, event):
    # LINE's webhookEventId is stable over redelivery; replyToken fallback is best-effort.
    key = str(event.get('webhookEventId') or event.get('replyToken') or '')
    if not key:
        return True
    cur = conn.execute('INSERT OR IGNORE INTO line_flow_events(event_key,received_at) VALUES(?,?)',
                       (key, int(time.time())))
    conn.commit()
    return cur.rowcount == 1


def image_received(conn, uid, brand, message_id):
    initialize(conn)
    now = int(time.time())
    # An image may be redelivered; do not re-assign it to another group.
    if conn.execute('SELECT 1 FROM line_flow_items WHERE message_id=?', (message_id,)).fetchone():
        return
    row = conn.execute('''SELECT g.id, g.last_photo_at,
      (SELECT count(*) FROM line_flow_items WHERE group_id=g.id) AS photo_count
      FROM line_flow_groups g WHERE line_user_id=? AND brand=?
      AND stage='collecting' AND closed=0 ORDER BY id DESC LIMIT 1''', (uid, brand)).fetchone()
    if row and now - row['last_photo_at'] <= WINDOW_SECONDS and row['photo_count'] < MAX_PHOTOS:
        group_id = row['id']
        conn.execute('UPDATE line_flow_groups SET last_photo_at=?,updated_at=?,ready_at=? WHERE id=?',
                     (now, now, now + WINDOW_SECONDS, group_id))
    else:
        cur = conn.execute('''INSERT INTO line_flow_groups
          (line_user_id,brand,created_at,updated_at,last_photo_at,ready_at)
          VALUES(?,?,?,?,?,?)''', (uid, brand, now, now, now, now + WINDOW_SECONDS))
        group_id = cur.lastrowid
    conn.execute('INSERT OR IGNORE INTO line_flow_items(message_id,group_id) VALUES(?,?)',
                 (message_id, group_id))
    conn.commit()


def caption_received(conn, uid, brand, event_mid, text, timestamp_ms):
    initialize(conn)
    if conn.execute('SELECT 1 FROM caption_events WHERE message_id=?',(event_mid,)).fetchone():
        return -1  # LINE webhook redelivery: previously handled; do not send an error.
    now = int(time.time())
    row = conn.execute('''SELECT * FROM line_flow_groups WHERE line_user_id=? AND brand=?
        AND stage='collecting' AND closed=0 AND last_photo_at>=?
        ORDER BY id DESC LIMIT 1''', (uid, brand, now - WINDOW_SECONDS)).fetchone()
    if not row:
        return 0
    conn.execute('BEGIN IMMEDIATE')
    try:
        cur = conn.execute('''INSERT OR IGNORE INTO caption_events
          (message_id,line_user_id,created_at,photo_count) VALUES(?,?,?,0)''',
                           (event_mid, uid, now))
        if cur.rowcount != 1:
            conn.commit()
            return 0
        mids = [item['message_id'] for item in conn.execute(
            'SELECT message_id FROM line_flow_items WHERE group_id=?', (row['id'],))]
        for mid in mids:
            conn.execute('''UPDATE jobs SET caption_text=?,caption_at=?,
                caption_sync_pending=1, caption_sync_after=0,updated_at=?
                WHERE message_id=? AND line_user_id=?''', (text[:2200], now, now, mid, uid))
        conn.execute('UPDATE caption_events SET photo_count=? WHERE message_id=?', (len(mids),event_mid))
        conn.execute('''UPDATE line_flow_groups SET caption_text=?,closed=1,
            ready_at=?,updated_at=? WHERE id=?''', (text[:2200], now, now, row['id']))
        conn.commit()
        return len(mids)
    except Exception:
        conn.rollback()
        raise


def current_group(conn, uid, brand):
    return conn.execute('''SELECT * FROM line_flow_groups
        WHERE line_user_id=? AND brand=? AND stage IN
        ('ready','hold','scheduled','awaiting_schedule_confirmation','failed')
        ORDER BY id DESC LIMIT 1''', (uid, brand)).fetchone()


def enqueue_action(conn, group, operation, instruction=''):
    now = int(time.time())
    conn.execute('''INSERT INTO line_flow_actions(group_id,operation,instruction,created_at,updated_at)
      VALUES(?,?,?,?,?)''', (group['id'], operation, instruction[:2200], now, now))
    conn.commit()


def handle_text_command(conn, uid, brand, text):
    """Return True if consumed. Photo notes are handled separately by caller."""
    initialize(conn)
    if text in ('投稿案','キャプション確認'):
        group = current_group(conn,uid,brand)
        if group and group['draft_id']:
            enqueue_action(conn,group,'preview')
        else:
            enqueue_message(conn,uid,text_message('確認できる投稿案がありません。写真と文章を送ってください。'))
        return True
    if text in ('投稿一覧', '投稿状況'):
        recent = conn.execute('''SELECT * FROM line_flow_groups WHERE line_user_id=? AND brand=?
          ORDER BY id DESC LIMIT 5''', (uid,brand)).fetchall()
        summary = '\n'.join(f"#{r['id']} {r['stage']}" for r in recent) or '投稿案はありません'
        enqueue_message(conn, uid, text_message(f'{BRANDS[brand]} の最近の投稿:\n{summary}'))
        return True
    if text in ('再試行','生成し直す'):
        row = conn.execute('''SELECT * FROM line_flow_groups WHERE line_user_id=? AND brand=? AND stage='failed'
            AND draft_id IS NULL ORDER BY id DESC LIMIT 1''',(uid,brand)).fetchone()
        if row:
            conn.execute("UPDATE line_flow_groups SET stage='collecting',closed=1,ready_at=?,updated_at=? WHERE id=?",
                         (int(time.time()),int(time.time()),row['id']))
            conn.commit()
            enqueue_message(conn,uid,text_message('投稿案の生成を再試行します。'))
        else:
            enqueue_message(conn,uid,text_message('再試行対象の投稿案がありません。'))
        return True
    if text.startswith(('修正:', '修正：', '再生成:', '再生成：')):
        is_replace = text.startswith(('修正:', '修正：'))
        value = text.split(':',1)[-1] if ':' in text else text.split('：',1)[-1]
        value = value.strip()
        group = current_group(conn,uid,brand)
        if not group or group['stage'] not in ('ready','hold','failed'):
            enqueue_message(conn,uid,text_message('修正できる投稿案がありません。投稿案を生成した後に送信してください。'))
            return True
        if not value or len(value)>2200:
            enqueue_message(conn,uid,text_message('文章を1〜2200文字で入力してください。'))
            return True
        conn.execute('UPDATE line_flow_groups SET stage=?,updated_at=?,version=version+1 WHERE id=?',
                     ('editing' if is_replace else 'regenerating', int(time.time()), group['id']))
        conn.commit()
        enqueue_action(conn,group,'replace' if is_replace else 'regenerate', value)
        enqueue_message(conn,uid,text_message('修正を受け付けました。処理後に新しい案を送ります。'))
        return True
    return False


def parse_schedule_datetime(value):
    try:
        dt = datetime.strptime(value,'%Y-%m-%dT%H:%M').replace(tzinfo=JST)
    except (TypeError,ValueError) as exc:
        raise ValueError('予約日時を選び直してください') from exc
    utc = dt.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if utc <= now + timedelta(seconds=60) or utc > now+timedelta(days=180):
        raise ValueError('予約は現在から1分後〜180日後の日時にしてください')
    return utc.isoformat(timespec='seconds')


def postback_received(conn, event, uid, brand):
    initialize(conn)
    if not registered_event(conn,event):
        return True
    postback = event.get('postback') or {}
    data = parse_qs(str(postback.get('data','')), keep_blank_values=False)
    try:
        action = data['a'][0]
        group_id = int(data['g'][0])
        version = int(data['v'][0])
    except (ValueError,KeyError,IndexError):
        return False
    group = conn.execute('SELECT * FROM line_flow_groups WHERE id=? AND line_user_id=? AND brand=?',
                         (group_id,uid,brand)).fetchone()
    if not group or group['version'] != version:
        enqueue_message(conn,uid,text_message('古い操作、または別ブランドの投稿です。現在の投稿案から操作してください。'))
        return True
    stage = group['stage']
    if action=='postnow' and stage=='ready':
        enqueue_message(conn,uid,
            action_card('公開前の最終確認','この投稿をInstagramに公開します。よろしいですか？',[
                action_button('公開する','publish',group_id,version),
                action_button('戻る','preview',group_id,version)]))
    elif action=='publish' and stage=='ready':
        approvers = {x.strip() for x in os.getenv('SHARED_LINE_APPROVER_IDS','').split(',') if x.strip()}
        if uid not in approvers:
            enqueue_message(conn,uid,text_message('投稿権限がありません。管理者にLINE承認者として登録してもらってください。'))
            return True
        conn.execute('UPDATE line_flow_groups SET stage=?,updated_at=? WHERE id=? AND stage=?',
                     ('publish_queued',int(time.time()),group_id,'ready'))
        conn.commit()
        enqueue_action(conn,group,'publish')
        enqueue_message(conn,uid,text_message('投稿処理を開始します。結果はLINEでお知らせします。'))
    elif action=='schedule' and stage in ('ready','scheduled'):
        chosen = (postback.get('params') or {}).get('datetime')
        try:
            at = parse_schedule_datetime(chosen)
        except ValueError as exc:
            enqueue_message(conn,uid,text_message(str(exc)))
            return True
        conn.execute('''UPDATE line_flow_groups SET pending_schedule=?,
            stage='awaiting_schedule_confirmation',updated_at=? WHERE id=?''',
                     (at,int(time.time()),group_id))
        conn.commit()
        label = datetime.fromisoformat(at).astimezone(JST).strftime('%Y/%m/%d %H:%M')
        enqueue_message(conn,uid,
            action_card('予約の最終確認',f'{label} にInstagramへ投稿しますか？',[
                action_button('予約を確定','book',group_id,version),
                action_button('戻る','preview',group_id,version)]))
    elif action=='book' and stage=='awaiting_schedule_confirmation':
        approvers = {x.strip() for x in os.getenv('SHARED_LINE_APPROVER_IDS','').split(',') if x.strip()}
        if uid not in approvers:
            enqueue_message(conn,uid,text_message('予約投稿の承認権限がありません。'))
            return True
        conn.execute("UPDATE line_flow_groups SET stage='schedule_queued',updated_at=? WHERE id=?",
                     (int(time.time()),group_id))
        conn.commit()
        enqueue_action(conn,group,'book')
        enqueue_message(conn,uid,text_message('予約を登録しています。完了後にお知らせします。'))
    elif action=='cancelbook' and stage=='scheduled':
        approvers = {x.strip() for x in os.getenv('SHARED_LINE_APPROVER_IDS','').split(',') if x.strip()}
        if uid not in approvers:
            enqueue_message(conn,uid,text_message('予約取り消し権限がありません。'))
            return True
        conn.execute("UPDATE line_flow_groups SET stage='cancel_queued',updated_at=? WHERE id=?",
                     (int(time.time()),group_id))
        conn.commit()
        enqueue_action(conn,group,'cancelbook')
    elif action=='hold' and stage=='ready':
        conn.execute("UPDATE line_flow_groups SET stage='hold',updated_at=? WHERE id=?",
                     (int(time.time()),group_id))
        conn.commit()
        enqueue_action(conn,group,'hold')
        enqueue_message(conn,uid,text_message('投稿案を保留にします。'))
    elif action in ('preview','edit') and stage in ('ready','hold','failed','scheduled','awaiting_schedule_confirmation'):
        if action=='edit':
            enqueue_message(conn,uid,text_message('文章の修正：\n修正: 完成させたい投稿文全文\n\nAIへの追加指示：\n再生成: 変更内容'))
        else:
            if stage=='awaiting_schedule_confirmation':
                conn.execute("UPDATE line_flow_groups SET stage='ready',pending_schedule=NULL WHERE id=?",(group_id,))
                conn.commit()
            enqueue_action(conn,group,'preview')
    else:
        enqueue_message(conn,uid,text_message('操作を受け付けられません。投稿状態を確認してください。'))
    return True


def proposal_messages(group, caption):
    gid, version = group['id'],group['version']
    brand = BRANDS[group['brand']]
    label = f'{brand} | 投稿案 #{gid}'
    schedule_action = {'type':'datetimepicker','label':'日時指定',
        'data':f'a=schedule&g={gid}&v={version}', 'mode':'datetime'}
    return [text_message(label+'\n\n'+caption[:4000]),
            action_card('投稿操作','内容を確認して次の操作を選択してください。',[
                action_button('今すぐ投稿','postnow',gid,version),
                schedule_action,
                action_button('文章を修正','edit',gid,version),
                action_button('保留','hold',gid,version)])]

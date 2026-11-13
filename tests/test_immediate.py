"""Dependency-light tests: python -m unittest discover -s tests -v"""
from __future__ import annotations
import importlib
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Only use this Flask shim on environments with no Flask installed.
try:
    import flask
except ImportError:
    flask = types.ModuleType('flask')
    class Blueprint:
        def __init__(self, *args, **kwargs):
            pass
        def route(self, *args, **kwargs):
            return lambda func: func
    flask.Blueprint = Blueprint
    flask.abort = lambda code: (_ for _ in ()).throw(RuntimeError(code))
    flask.jsonify = lambda data: data
    flask.request = None
    sys.modules['flask'] = flask

# Stubs for worker's Google clients when dependencies are not present.
def module(name, **items):
    result = types.ModuleType(name)
    result.__dict__.update(items)
    sys.modules[name] = result
    return result

try:
    import googleapiclient.discovery
except ImportError:
    module('google')
    module('google.auth')
    module('google.auth.transport')
    module('google.auth.transport.requests', Request=object)
    module('google.oauth2')
    module('google.oauth2.credentials', Credentials=object)
    module('googleapiclient')
    module('googleapiclient.discovery', build=Mock())
    module('googleapiclient.http', MediaFileUpload=object)

from line_drive_common import db, get_caption_for_drive_file
from line_drive_bridge import process_event
from line_drive_worker import sync_captions

USER = 'U' + '1' * 32
USER2 = 'U' + '2' * 32
FOLDER = 'fixture_bisyu_inbox'


def event(user, kind, mid, ts, text=None):
    return {'type': 'message', 'source': {'type': 'user', 'userId': user},
            'timestamp': ts, 'message': {'type': kind, 'id': mid, 'text': text}}


class ImmediateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            'LINE_DRIVE_DATA_DIR': self.tmp.name,
            'LINE_ALLOWED_USER_IDS': USER,
            'BISYU_DRIVE_INBOX_FOLDER_ID': FOLDER,
            'LINE_CAPTION_WINDOW_SECONDS': '180',
        })
        self.env.start()
        self.connection = db()
        self.now = int(time.time()*1000)

    def tearDown(self):
        self.connection.close()
        self.env.stop()
        self.tmp.cleanup()

    def test_image_then_caption(self):
        self.assertTrue(process_event(self.connection, event(USER,'image','501001',self.now)))
        self.assertTrue(process_event(self.connection, event(USER,'text','501002',self.now+1000,'本日のおすすめメニュー')))
        row = self.connection.execute('SELECT * FROM jobs WHERE message_id=?',('501001',)).fetchone()
        self.assertEqual(row['folder_id'],FOLDER)
        self.assertEqual(row['caption_text'],'本日のおすすめメニュー')
        self.assertEqual(row['caption_sync_pending'],1)
        self.assertEqual(row['line_timestamp_ms'],self.now)

    def test_multiple_photos_share_following_caption(self):
        self.assertTrue(process_event(self.connection, event(USER,'image','501010',self.now)))
        self.assertTrue(process_event(self.connection, event(USER,'image','501011',self.now+50)))
        self.assertTrue(process_event(self.connection, event(USER,'text','501012',self.now+70,'投稿文です')))
        count = self.connection.execute("SELECT COUNT(*) FROM jobs WHERE caption_text='投稿文です'").fetchone()[0]
        self.assertEqual(count,2)

    def test_redelivery_is_idempotent_and_does_not_label_next_image(self):
        process_event(self.connection, event(USER,'image','501020',self.now))
        process_event(self.connection, event(USER,'text','501021',self.now+100,'最初'))
        process_event(self.connection, event(USER,'image','501022',self.now+200))
        self.assertFalse(process_event(self.connection, event(USER,'text','501021',self.now+100,'最初')))
        row = self.connection.execute("SELECT caption_text FROM jobs WHERE message_id='501022'").fetchone()
        self.assertIsNone(row['caption_text'])

    def test_ignores_other_users_and_expired_caption(self):
        self.assertFalse(process_event(self.connection, event(USER2,'image','501030',self.now)))
        process_event(self.connection, event(USER,'image','501031',self.now-400000))
        self.assertFalse(process_event(self.connection, event(USER,'text','501032',self.now,'時間外')))
        self.assertEqual(1, self.connection.execute('SELECT count(*) FROM jobs').fetchone()[0])

    def test_done_file_metadata_update(self):
        process_event(self.connection, event(USER,'image','501040',self.now))
        process_event(self.connection, event(USER,'text','501041',self.now+1000,'投稿用の文章'))
        self.connection.execute("UPDATE jobs SET status='done',google_file_id='file_google1' WHERE message_id='501040'")
        self.connection.commit()
        svc = Mock()
        self.assertEqual(1, sync_captions(self.connection, svc))
        svc.files.return_value.update.assert_called_once_with(
            fileId='file_google1', body={'description':'投稿用の文章'}, fields='id', supportsAllDrives=True)
        self.assertEqual('投稿用の文章',get_caption_for_drive_file('file_google1'))
        self.assertEqual(0, self.connection.execute("SELECT caption_sync_pending FROM jobs WHERE message_id='501040'").fetchone()[0])

    def test_existing_db_upgrades_without_data_loss(self):
        # Mimic a database created by the previously deployed version.
        self.connection.close()
        from line_drive_common import database_path
        import sqlite3
        from pathlib import Path
        Path(database_path()).unlink()
        previous = sqlite3.connect(str(database_path()))
        previous.execute("""CREATE TABLE jobs (
            message_id TEXT PRIMARY KEY,line_user_id TEXT NOT NULL,folder_id TEXT NOT NULL,
            label TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0, run_after INTEGER NOT NULL,
            created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,
            google_file_id TEXT,google_filename TEXT,last_error TEXT)""")
        previous.execute("INSERT INTO jobs (message_id,line_user_id,folder_id,label,status,run_after,created_at,updated_at) VALUES ('old001',?,?,?,'done',0,0,0)",(USER,FOLDER,'美趣'))
        previous.commit()
        previous.close()
        self.connection = db()
        row = self.connection.execute("SELECT status,caption_text FROM jobs WHERE message_id='old001'").fetchone()
        self.assertEqual('done',row['status'])
        self.assertIsNone(row['caption_text'])


if __name__ == '__main__':
    unittest.main()

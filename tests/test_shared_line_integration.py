import tempfile, pathlib, sqlite3, sys, os, time, types, json, unittest
from unittest.mock import patch
from datetime import datetime,timezone,timedelta
HERE=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(pathlib.Path.cwd()))
sys.path.insert(0,str(HERE / 'src'))
sys.path.insert(0,str(HERE))
from database import Database
import shared_line_flow as f
import shared_line_flow_worker as w
UID='U'+'b'*32

class Account:
    key='crach29'
    instagram_ig_user_id='ig_id'
    instagram_access_token='dummy'
    drive_posted_folder_id='posted_id'
    drive_inbox_folder_id='folder_id'
    caption_prompt='日本語で書いてください'

class FlowIntegration(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'SHARED_LINE_APPROVER_IDS': UID, 'SHARED_LINE_ENABLE_POSTING': '1'})
        env.start()
        self.addCleanup(env.stop)
        self.tmp=tempfile.TemporaryDirectory()
        self.database=Database(pathlib.Path(self.tmp.name)/'app.sqlite3')
        self.database.init()
        self.conn=sqlite3.connect(':memory:')
        self.conn.row_factory=sqlite3.Row
        self.conn.executescript('''CREATE TABLE jobs(message_id TEXT PRIMARY KEY,line_user_id TEXT,
          status TEXT DEFAULT 'done',google_file_id TEXT,caption_text TEXT,
          caption_at INTEGER, caption_sync_pending INTEGER,caption_sync_after INTEGER,updated_at INTEGER);
          CREATE TABLE caption_events(message_id TEXT PRIMARY KEY,line_user_id TEXT,created_at INTEGER,photo_count INTEGER);
        ''')
        f.initialize(self.conn)
        self.old_get_account=w.get_account
        self.old_photos=w.photo_ids_for_group
        w.get_account=lambda conn, group, accounts:Account()
        self.accounts={'crach29':Account()}
        self.Config=types.SimpleNamespace(OPENAI_API_KEY='fake', OPENAI_MODEL='test')
        module=types.ModuleType('caption_client')
        class CaptionClient:
            def __init__(self,*_): pass
            def generate(self,photos,instruction=''):return '文章生成しました。 '+instruction
        module.CaptionClient=CaptionClient
        self.old_caption=sys.modules.get('caption_client')
        sys.modules['caption_client']=module
        item={'id':'drive_fake_123','name':'image.jpg','mimeType':'image/jpeg'}
        media_file=pathlib.Path(self.tmp.name)/'image.jpg';media_file.write_bytes(b'not-real')
        self.database.upsert_photo(item,media_file,'publictoken123','crach29')
        self.photo_id=self.database.list_photos()[0]['id']
        w.photo_ids_for_group=lambda *args:[self.photo_id]
        self.conn.execute('INSERT INTO jobs(message_id,line_user_id,google_file_id) VALUES(?,?,?)',('img-1',UID,'drive_fake_123'))
        f.image_received(self.conn,UID,'crach29','img-1')
        self.conn.commit()
        f.caption_received(self.conn,UID,'crach29','text-1','撮影メモです',int(time.time()*1000))
        self.group=self.conn.execute('SELECT * FROM line_flow_groups').fetchone()
        self.assertTrue(w.create_proposal(self.conn,self.group,self.Config,self.database,self.accounts))
        self.group=self.conn.execute('SELECT * FROM line_flow_groups').fetchone()
        self.assertEqual('ready',self.group['stage'])
        self.assertEqual('draft',self.database.get_draft(self.group['draft_id'])['status'])
    def tearDown(self):
        w.get_account=self.old_get_account
        w.photo_ids_for_group=self.old_photos
        if self.old_caption:sys.modules['caption_client']=self.old_caption
        else:sys.modules.pop('caption_client',None)
        self.conn.close()
        self.tmp.cleanup()
    def process_action(self,operation,instruction=''):
        group=self.conn.execute('SELECT * FROM line_flow_groups').fetchone()
        f.enqueue_action(self.conn,group,operation,instruction)
        row=self.conn.execute('SELECT * FROM line_flow_actions ORDER BY id DESC LIMIT 1').fetchone()
        w.handle_action(self.conn,row,self.Config,self.database,self.accounts)
    def test_edit_then_schedule_and_cancel(self):
        group_id=self.group['id']
        self.assertTrue(f.handle_text_command(self.conn,UID,'crach29','修正: 完成した文章'))
        self.process_action('replace','完成した文章')
        group=self.conn.execute('SELECT * FROM line_flow_groups WHERE id=?',(group_id,)).fetchone()
        self.assertEqual('ready',group['stage'])
        self.assertEqual('完成した文章',self.database.get_draft(group['draft_id'])['caption'])
        scheduled=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat(timespec='seconds')
        self.conn.execute('UPDATE line_flow_groups SET stage=?,pending_schedule=? WHERE id=?',('schedule_queued',scheduled,group_id))
        self.conn.commit()
        self.process_action('book')
        self.assertEqual('scheduled',self.database.get_draft(group['draft_id'])['status'])
        self.assertEqual('scheduled',self.conn.execute('SELECT stage FROM line_flow_groups WHERE id=?',(group_id,)).fetchone()[0])
        self.conn.execute("UPDATE line_flow_groups SET stage='cancel_queued' WHERE id=?",(group_id,))
        self.conn.commit()
        self.process_action('cancelbook')
        self.assertEqual('draft',self.database.get_draft(group['draft_id'])['status'])
    def test_generation(self):
        self.assertTrue(f.handle_text_command(self.conn,UID,'crach29','再生成: 短く'))
        self.process_action('regenerate','短く')
        self.assertIn('短く',self.database.get_draft(self.group['draft_id'])['caption'])
    def test_double_claim(self):
        self.assertTrue(self.database.claim_publish(self.group['draft_id'],'caption'))
        self.assertFalse(self.database.claim_publish(self.group['draft_id'],'caption'))

if __name__=='__main__':unittest.main()

import sqlite3, sys, time, unittest, os
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parent))
import shared_line_flow as f
UID='U'+'a'*32


def database():
    conn=sqlite3.connect(':memory:')
    conn.row_factory=sqlite3.Row
    conn.executescript('''CREATE TABLE jobs (message_id TEXT PRIMARY KEY,line_user_id TEXT,
    status TEXT DEFAULT 'queued',caption_text TEXT,caption_at INTEGER,
    caption_sync_pending INTEGER DEFAULT 0,caption_sync_after INTEGER DEFAULT 0,updated_at INTEGER);
    CREATE TABLE caption_events(message_id TEXT PRIMARY KEY,line_user_id TEXT,created_at INTEGER,photo_count INTEGER);
    ''')
    f.initialize(conn)
    return conn

class FlowTests(unittest.TestCase):
    def test_group_multiple_and_caption(self):
        c=database()
        for mid in ['img_a','img_b']:
            c.execute('INSERT INTO jobs(message_id,line_user_id) VALUES (?,?)',(mid,UID))
            f.image_received(c,UID,'crach29',mid)
        row=c.execute('SELECT * FROM line_flow_groups').fetchone()
        self.assertEqual(1,c.execute('SELECT count(*) FROM line_flow_groups').fetchone()[0])
        self.assertEqual(2,f.caption_received(c,UID,'crach29','txt1','撮影しました',int(time.time()*1000)))
        self.assertEqual(-1,f.caption_received(c,UID,'crach29','txt1','撮影しました',int(time.time()*1000)))
        self.assertEqual(2,c.execute('SELECT count(*) FROM jobs WHERE caption_text=?',('撮影しました',)).fetchone()[0])
        self.assertEqual(1,c.execute('SELECT closed FROM line_flow_groups').fetchone()[0])
    def test_brand_separation(self):
        c=database()
        for i,brand in enumerate(['crach29','photo29']):
            mid='img'+str(i)
            c.execute('INSERT INTO jobs(message_id,line_user_id) VALUES (?,?)',(mid,UID))
            f.image_received(c,UID,brand,mid)
        self.assertEqual(1,f.caption_received(c,UID,'photo29','txt2','写真店の本文',int(time.time()*1000)))
        self.assertIsNone(c.execute("SELECT caption_text FROM jobs WHERE message_id='img0'").fetchone()[0])
        self.assertEqual('写真店の本文',c.execute("SELECT caption_text FROM jobs WHERE message_id='img1'").fetchone()[0])
    def test_publish_double_tap(self):
        c=database()
        now=int(time.time())
        c.execute('''INSERT INTO line_flow_groups(id,line_user_id,brand,stage,created_at,updated_at,last_photo_at,ready_at)
            VALUES (1,?, 'crach29','ready',?,?,?,?)''',(UID,now,now,now,now))
        c.commit()
        os.environ['SHARED_LINE_APPROVER_IDS']=UID
        for n in range(2):
            ev={'type':'postback','webhookEventId':'e'+str(n),'postback':{'data':'a=publish&g=1&v=1'}}
            f.postback_received(c,ev,UID,'crach29')
        self.assertEqual(1,c.execute("SELECT count(*) FROM line_flow_actions WHERE operation='publish'").fetchone()[0])
    def test_non_approver_cannot_publish(self):
        c=database(); now=int(time.time())
        c.execute("INSERT INTO line_flow_groups(id,line_user_id,brand,stage,created_at,updated_at,last_photo_at,ready_at) VALUES(1,?,'crach29','ready',?,?,?,?)",(UID,now,now,now,now))
        os.environ['SHARED_LINE_APPROVER_IDS']='U'+'c'*32
        f.postback_received(c,{'webhookEventId':'blocked','postback':{'data':'a=publish&g=1&v=1'}},UID,'crach29')
        self.assertEqual(0,c.execute('SELECT count(*) FROM line_flow_actions').fetchone()[0])
        self.assertEqual('ready',c.execute('SELECT stage FROM line_flow_groups').fetchone()[0])
    def test_old_button_rejected(self):
        c=database(); now=int(time.time())
        c.execute("INSERT INTO line_flow_groups(id,line_user_id,brand,stage,version,created_at,updated_at,last_photo_at,ready_at) VALUES(1,?,'crach29','ready',3,?,?,?,?)",(UID,now,now,now,now))
        os.environ['SHARED_LINE_APPROVER_IDS']=UID
        f.postback_received(c,{'webhookEventId':'outdated','postback':{'data':'a=publish&g=1&v=2'}},UID,'crach29')
        self.assertEqual(0,c.execute('SELECT count(*) FROM line_flow_actions').fetchone()[0])
    def test_schedule_validation(self):
        now=datetime.now(ZoneInfo('Asia/Tokyo'))
        self.assertTrue(f.parse_schedule_datetime((now+timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M')).endswith('+00:00'))
        with self.assertRaises(ValueError):
            f.parse_schedule_datetime((now-timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'))
    def test_buttons(self):
        c=database(); now=int(time.time())
        c.execute('INSERT INTO line_flow_groups(id,line_user_id,brand,stage,created_at,updated_at,last_photo_at,ready_at) VALUES (1,?,?,?, ?,?,?,?)',(UID,'photo29','ready',now,now,now,now))
        group=c.execute('SELECT * FROM line_flow_groups WHERE id=1').fetchone()
        msgs=f.proposal_messages(group,'test')
        self.assertEqual('datetimepicker',msgs[1]['template']['actions'][1]['type'])
        self.assertEqual(4,len(msgs[1]['template']['actions']))

if __name__=='__main__': unittest.main()

"""Offline tests; no LINE or Instagram API calls, no credentials."""
import os
import re
import sqlite3
import sys
import types
import unittest

import shared_line_flow as f
import shared_line_richmenu as rm
import richmenu_setup as setup

UID='U'+'a'*32


class RichmenuTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.conn.row_factory=sqlite3.Row
        self.conn.executescript('''
        CREATE TABLE brand_state(line_user_id TEXT PRIMARY KEY,brand TEXT NOT NULL,updated_at INTEGER NOT NULL);
        CREATE TABLE jobs(message_id TEXT PRIMARY KEY,line_user_id TEXT NOT NULL,
          caption_text TEXT,caption_at INTEGER, caption_sync_pending INTEGER, caption_sync_after INTEGER);
        CREATE TABLE caption_events(message_id TEXT PRIMARY KEY,line_user_id TEXT,created_at INTEGER,photo_count INTEGER);
        ''')
        f.initialize(self.conn)
        self.original=sys.modules.get('shared_line_bridge')
        self.fake=types.ModuleType('shared_line_bridge')
        self.fake.FOLDER_ID=re.compile('[A-Za-z0-9_-]{10,160}')
        self.fake.folder_for=lambda brand: {'crach29':'drive_crch001','photo29':'drive_photo001'}[brand]
        def brand_set(conn,uid,brand):
            conn.execute('INSERT INTO brand_state VALUES(?,?,0) ON CONFLICT(line_user_id) DO UPDATE SET brand=excluded.brand',(uid,brand))
            conn.commit()
        def brand_get(conn,uid):
            row=conn.execute('SELECT brand FROM brand_state WHERE line_user_id=?',(uid,)).fetchone()
            return row[0] if row else None
        self.fake.brand_set=brand_set
        self.fake.brand_get=brand_get
        self.fake.setting=lambda key: UID if key=='SHARED_LINE_APPROVER_IDS' else ''
        sys.modules['shared_line_bridge']=self.fake
    def tearDown(self):
        if self.original is None:sys.modules.pop('shared_line_bridge',None)
        else:sys.modules['shared_line_bridge']=self.original
        self.conn.close()
    def ev(self,data,eid='test-1'):
        return {'postback':{'data':data},'webhookEventId':eid}
    def test_brand_switch_and_replay_protection(self):
        event=self.ev('a=rm_brand&b=photo29')
        self.assertTrue(rm.handle_richmenu(self.conn,event,UID))
        self.assertEqual(self.fake.brand_get(self.conn,UID),'photo29')
        count=self.conn.execute('SELECT COUNT(*) FROM line_flow_outbox').fetchone()[0]
        self.assertTrue(rm.handle_richmenu(self.conn,event,UID))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM line_flow_outbox').fetchone()[0],count)
    def test_menu_status_sets_brand(self):
        self.assertTrue(rm.handle_richmenu(self.conn,self.ev('a=rm_menu&b=crach29&op=status'),UID))
        self.assertEqual(self.fake.brand_get(self.conn,UID),'crach29')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM line_flow_outbox').fetchone()[0],1)
    def test_pending_approval_requires_explicit_confirmation(self):
        self.conn.execute("INSERT INTO line_flow_groups(line_user_id,brand,stage,closed,created_at,updated_at,last_photo_at,ready_at,draft_id) VALUES(?,?,'ready',1,0,0,0,0,9)",(UID,'crach29'))
        self.conn.commit()
        self.assertTrue(rm.handle_richmenu(self.conn,self.ev('a=rm_menu&b=crach29&op=approve'),UID))
        rows=self.conn.execute('SELECT payload FROM line_flow_outbox ORDER BY id').fetchall()
        import json
        self.assertEqual(len(rows),2)
        actions=json.loads(rows[-1][0])['template']['actions']
        self.assertTrue(any('a=postnow' in a['data'] for a in actions))
        self.assertFalse(any('a=publish' in a['data'] for a in actions))
    def test_images_and_geometry(self):
        for brand in setup.BRANDS:
            d=setup.menu_object(brand)
            self.assertEqual(len(d['areas']),8)
            self.assertEqual(d['size'],{'width':2500,'height':1686})
            self.assertEqual(d['areas'][0]['action']['type'],'richmenuswitch')
            self.assertEqual(d['areas'][1]['action']['type'],'richmenuswitch')
            self.assertTrue(all(a['bounds']['width']>0 for a in d['areas']))
    def test_non_menu_postback_remains_original_flow(self):
        self.assertFalse(rm.handle_richmenu(self.conn,self.ev('a=postnow&g=1&v=1'),UID))


if __name__=='__main__':unittest.main(verbosity=2)

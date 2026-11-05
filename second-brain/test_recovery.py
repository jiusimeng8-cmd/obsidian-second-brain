import json
from pathlib import Path
import tempfile
import time
import unittest
import storage
from collector import capture
from quality import verify, read_covers
from worker import complete, idle_for_manual, candidates, rotate
from api import public_text

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.old=storage.STATE
        storage.STATE=Path(self.temp.name)
        self.db=storage.connect()
        self.db.execute("INSERT INTO sources(id,updated,info) VALUES('source',1,'{}')")
        self.db.commit()
        self.cfg={'quiet_seconds':0,'chunk_chars':100,'vault':self.temp.name}
    def tearDown(self):
        self.db.close();storage.STATE=self.old;self.temp.cleanup()
    def test_capture_restart_no_loss(self):
        messages=[{'id':'msg_u','type':'user','text':'a'*250,'time':{'created':1}},
                  {'id':'msg_a','type':'assistant','content':[{'type':'text','text':'结论'}],
                   'finish':'stop','time':{'created':2,'completed':3}}]
        class Fake:
            def messages(self,*args):return messages,True
        capture(self.db,Fake(),self.cfg,self.db.execute('SELECT * FROM sources').fetchone())
        count=self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
        self.assertGreaterEqual(count,3)
        self.db.close();self.db=storage.connect()
        capture(self.db,Fake(),self.cfg,self.db.execute('SELECT * FROM sources').fetchone())
        self.assertEqual(count,self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0])
        chunks=self.db.execute("SELECT text FROM messages WHERE kind='user' ORDER BY position").fetchall()
        self.assertEqual('a'*250,''.join(r[0] for r in chunks))
        self.assertIsNone(self.db.execute('SELECT acknowledged_id FROM sources').fetchone()[0])
        jobs=self.db.execute('SELECT * FROM jobs ORDER BY position').fetchall()
        complete(self.db,jobs[-1],{'status':'skipped_verified'})
        self.assertIsNone(self.db.execute('SELECT acknowledged_id FROM sources').fetchone()[0])
        complete(self.db,jobs[0],{'status':'skipped_verified'})
        self.assertEqual(jobs[0]['last_id'],self.db.execute('SELECT acknowledged_id FROM sources').fetchone()[0])
    def test_claim_without_tool_evidence_rejected(self):
        Path(self.temp.name,'note.md').write_text('source msg_u 这里是足够长的实际笔记内容',encoding='utf8')
        job={'id':'j','source':'source','refs':json.dumps([{'id':'msg_u'}])}
        receipt={'job':'j','status':'written','reviewed_refs':['msg_u'],
                 'notes':[{'path':'note.md','quote':'这里是足够长的实际笔记内容'}]}
        with self.assertRaisesRegex(ValueError,'MCP'):
            verify(job,receipt,[],self.cfg,{})
    def test_no_value_cannot_hide_substantive_prefixed_text(self):
        job={'id':'j','refs':json.dumps([{'id':'msg_u'}]),'evidence':'[助手陈述，需核实] 服务器已迁移'}
        with self.assertRaises(ValueError):
            verify(job,{'job':'j','status':'no_value','reason':'无价值','reviewed_refs':['msg_u']},[],self.cfg,{})
    def test_manual_turn_blocks_dispatch(self):
        class Fake:
            def call(self,*args):return {'data':[]}
            def messages(self,*args):return [{'type':'user','time':{'created':1}}],True
        self.assertFalse(idle_for_manual(self.db,Fake(),{'id':'w'}))
    def test_model_cooldown_uses_next_available(self):
        class Fake:
            def call(self,*args):return {'data':[{'id':x,'providerID':'p','enabled':True,'capabilities':{'tools':True}} for x in ['deepseek-v4-flash','deepseek-v5-flash']]}
        cfg={'preferred_family':'deepseek.*flash','preferred_providers':['p']}
        self.assertEqual(candidates(self.db,Fake(),cfg,None)[0]['id'],'deepseek-v5-flash')
        self.db.execute('INSERT INTO models(key,cooldown) VALUES(?,?)',('p/deepseek-v5-flash',time.time()+60))
        self.assertEqual(candidates(self.db,Fake(),cfg,None)[0]['id'],'deepseek-v4-flash')
    def test_reasoning_and_secrets_excluded(self):
        text=public_text({'type':'assistant','content':[{'type':'reasoning','text':'private'},
             {'type':'text','text':'api_key=supersecretvalue'}]})
        self.assertNotIn('private',text);self.assertNotIn('supersecretvalue',text)
    def test_exclusive_lock(self):
        with storage.exclusive_lock() as first:
            self.assertTrue(first)
            with storage.exclusive_lock() as second:self.assertFalse(second)
    def test_line_read_must_cover_quoted_evidence(self):
        call={'tool':'obsidian.read_note_lines','input':{'path':'index.md','startLine':2,'endLine':2}}
        self.assertTrue(read_covers(call,'index.md','heading\nverified quote\nother','verified quote'))
        self.assertFalse(read_covers(call,'index.md','heading\nother\nverified quote','verified quote'))
    def test_rotation_archives_before_creating(self):
        calls=[]
        class Fake:
            def messages(self,*args):return [{'type':'user'},{'type':'assistant','content':[{'type':'text','text':'done'}]}],True
            def update(self,sid,**fields):calls.append(('archive',fields))
            def session(self,sid):return {'id':sid,'time':{'archived':1}}
            def sessions(self):return []
            def call(self,*args,**kwargs):calls.append(('create',None));return {'data':{'id':'new'}}
        self.assertEqual(rotate(self.db,Fake(),{'rotate_messages':2,'title':'OBS','vault':'vault'},{'id':'old'})['id'],'new')
        self.assertEqual([c[0] for c in calls],['archive','create'])
    def test_written_verified_requires_real_trace_and_citations(self):
        Path(self.temp.name,'note.md').write_text('source msg_u 这里是足够长的实际笔记内容',encoding='utf8')
        job={'id':'j','source':'source','refs':json.dumps([{'id':'msg_u'}])}
        receipt={'job':'j','status':'written','reviewed_refs':['msg_u'],'notes':[{'path':'note.md','quote':'这里是足够长的实际笔记内容'}]}
        messages=[{'content':[{'type':'tool','state':{'status':'completed','metadata':{'toolCalls':[
            {'tool':'obsidian.'+name,'status':'completed','input':{'path':'note.md'}} for name in ('write_note','read_note')]}}}]}]
        self.assertEqual(verify(job,receipt,messages,self.cfg,{})['status'],'written_verified')

if __name__=='__main__':unittest.main()

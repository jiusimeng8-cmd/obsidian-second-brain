import json
from pathlib import Path
import tempfile
import unittest
import storage
from focus import window, prepare, schema
from quality import verify
from api import API

class FocusTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.old=storage.STATE
        storage.STATE=Path(self.temp.name);self.db=storage.connect();schema(self.db)
    def tearDown(self):
        self.db.close();storage.STATE=self.old;self.temp.cleanup()
    def seed(self,sid):
        self.db.execute('INSERT INTO sources(id,title,directory,status,info) VALUES(?,?,?,?,?)',(sid,'设备恢复','project','captured','{}'))
        for i in range(60):
            text='决定以后只使用有线连接' if i==20 else ('[工具结果 shell / / completed]\n'+'log '*3000 if i%2 else '继续')
            if i==59:text='[助手陈述，需核实] 最终仍未恢复，等待用户决定下一步。'
            self.db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)',(sid,'msg_'+sid+str(i),i+1,i,'user' if i%2==0 else 'assistant',storage.digest(text),text))
        self.db.execute("INSERT INTO jobs(id,source,position,status,refs) VALUES(?,?,60,'pending','[]')",('old_'+sid,sid))
        self.db.commit()
    def test_middle_decision_survives_and_tools_are_omitted(self):
        self.seed('s')
        rows=self.db.execute('SELECT * FROM messages ORDER BY position').fetchall()
        selected=window(rows,4000)
        body='\n'.join(t for r,t in selected)
        self.assertIn('只使用有线连接',body);self.assertNotIn('log log',body)
        self.assertLess(len(body),4000)
    def test_migration_keeps_evidence_and_is_idempotent(self):
        self.seed('a');self.seed('b');cfg={'max_attempts':3,'focus_window_chars':4000}
        prepare(self.db,cfg)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE id LIKE 'focus_%'").fetchone()[0],1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs WHERE status='superseded'").fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],120)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM focus_ranges').fetchone()[0],2)
        prepare(self.db,cfg)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM focus_ranges').fetchone()[0],2)
        self.assertIsNone(self.db.execute('SELECT acknowledged_id FROM sources LIMIT 1').fetchone()[0])
    def test_no_candidate_is_not_verified_skip(self):
        job={'id':'focus_a','refs':json.dumps([{'id':'m'}])}
        result=verify(job,{'job':'focus_a','status':'no_value','reason':'所给窗口仅含过渡调试，无长期决定','reviewed_refs':['m']},[],{}, {})
        self.assertEqual(result['status'],'focus_screened')
    def test_fork_new_decision_keeps_correct_source(self):
        self.seed('original');self.seed('fork')
        text='用户决定取消旧方案，改成独立本地运行'
        self.db.execute('UPDATE messages SET text=?,hash=? WHERE source=? AND position=40',(text,storage.digest(text),'fork'))
        self.db.execute("UPDATE messages SET kind='user' WHERE source='fork' AND position=40")
        self.db.commit();prepare(self.db,{'max_attempts':3,'focus_window_chars':4000})
        job=self.db.execute("SELECT * FROM jobs WHERE id LIKE 'focus_%'").fetchone()
        self.assertIn('取消旧方案',job['evidence'])
        refs=json.loads(job['refs']);self.assertTrue(any(r['source']=='fork' and r['position']==40 for r in refs))
        self.assertEqual({r['source'] for r in refs},{'original','fork'})
    def test_bounded_api_keeps_opening_tail_and_marks_gap(self):
        class Fake(API):
            def __init__(self):self.calls=[]
            def call(self,path,query):
                self.calls.append(query)
                make=lambda i:{'id':str(i),'time':{'created':i}}
                if query.get('order')=='asc':return {'data':[make(i) for i in range(30)]}
                top=300 if not query.get('cursor') else 200
                return {'data':[make(i) for i in range(top,top-100,-1)],'cursor':{'next':'next'}}
        api=Fake();rows,found,gap=api.focus_messages('s')
        self.assertTrue(gap);self.assertEqual(len(api.calls),3)
        self.assertEqual(rows[0]['id'],'0');self.assertEqual(rows[-1]['id'],'300')
        self.assertNotIn('50',[r['id'] for r in rows])
        api=Fake();rows,found,gap=api.focus_messages('s','280')
        self.assertTrue(found);self.assertFalse(gap)
        self.assertEqual(len(api.calls),1);self.assertEqual(rows[0]['id'],'281')
    def test_small_sources_batch_without_exceeding_budget(self):
        for i in range(8):self.seed(str(i))
        prepare(self.db,{'max_attempts':3,'focus_window_chars':4000,'focus_batch_sources':8,'focus_batch_chars':3000})
        job=self.db.execute("SELECT * FROM jobs WHERE id LIKE 'focus_%'").fetchone()
        self.assertGreater(self.db.execute('SELECT COUNT(*) FROM focus_ranges').fetchone()[0],4)
        self.assertLess(len(job['evidence']),4000)

if __name__=='__main__':unittest.main()

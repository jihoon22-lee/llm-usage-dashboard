"""Independent verification follows source identity aliases without trusting event totals."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from llm_usage.antigravity import collect_database
from llm_usage.collect import model_provider
from llm_usage.store import Store
from llm_usage.verify import reconcile,failed
from test_antigravity_history import usage,metadata,blob,uint

class VerifyAliasesTests(unittest.TestCase):
    def make(self,root,rows):
        sources=[];store=Store(root/'usage.db')
        for i,items in enumerate(rows):
            path=root/f'source-{i}.db';sources.append({'kind':'antigravity','path':str(path)})
            with closing(sqlite3.connect(path)) as c,c:
                c.executescript('CREATE TABLE steps(idx INTEGER PRIMARY KEY,metadata BLOB); CREATE TABLE gen_metadata(idx INTEGER PRIMARY KEY,data BLOB);')
                c.executemany('INSERT INTO steps VALUES (?,?)',enumerate(items))
            collect_database(store,path,model_provider)
        return store,{'sources':sources,'homes':[]}

    def test_streaming_aliases_transitive_copies_and_order(self):
        old=metadata(usage(None,output=4,thinking=1,message_id='m'),1000,complete=False)
        new=metadata(usage('r',output=20,thinking=5,message_id='m'),1001)
        bridge=metadata(usage('r')+blob(12,'p'),1001)
        tail=metadata(usage(None)+blob(12,'p'),1001)
        for rows in ([[old],[new,bridge],[tail]],[[tail],[bridge,new],[old]]):
            with self.subTest(order=rows[0]==[old]),tempfile.TemporaryDirectory() as tmp:
                store,cfg=self.make(Path(tmp),rows)
                r=reconcile(store.path,cfg)
                self.assertFalse(failed(r),r['antigravity'])
                self.assertEqual(r['antigravity']['original_requests'],1)
                self.assertEqual(r['antigravity']['matched_requests'],1)
                self.assertEqual(r['antigravity']['output'],20)
                self.assertEqual(r['antigravity']['duplicate_source_rows'],3)

    def test_tampered_missing_or_split_alias_events_still_fail(self):
        for damage in ('tokens','missing','alias'):
            with self.subTest(damage=damage),tempfile.TemporaryDirectory() as tmp:
                store,cfg=self.make(Path(tmp),[[metadata(usage('r',message_id='m'),1000)]])
                with store.connect() as c:
                    if damage=='tokens':c.execute('UPDATE events SET output=output+1')
                    elif damage=='missing':c.execute('DELETE FROM events')
                    else:c.execute("UPDATE antigravity_request_aliases SET request_id='wrong' WHERE alias=(SELECT alias FROM antigravity_request_aliases LIMIT 1)")
                r=reconcile(store.path,cfg)
                self.assertTrue(failed(r),damage)
                key={'tokens':'mismatched_events','missing':'missing_events','alias':'alias_errors'}[damage]
                self.assertEqual(r['antigravity'][key],1)

    def test_incomparable_complete_sources_remain_a_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            store,cfg=self.make(Path(tmp),[[metadata(usage('r',input=100,output=20),1000)],
                                          [metadata(usage('r',input=10,output=30),1000)]])
            r=reconcile(store.path,cfg)
            self.assertTrue(failed(r));self.assertEqual(r['antigravity']['source_conflicts'],1)

    def test_invalid_output_split_and_missing_alias_still_fail(self):
        for damage in ('split','alias'):
            with self.subTest(damage=damage),tempfile.TemporaryDirectory() as tmp:
                raw=usage('r',message_id='m')+(uint(10,999) if damage=='split' else b'')
                store,cfg=self.make(Path(tmp),[[metadata(raw,1000)]])
                if damage=='alias':
                    with store.connect() as c:c.execute('DELETE FROM antigravity_request_aliases WHERE alias=(SELECT alias FROM antigravity_request_aliases LIMIT 1)')
                r=reconcile(store.path,cfg)
                self.assertTrue(failed(r))
                self.assertEqual(r['antigravity']['output_split_errors' if damage=='split' else 'alias_errors'],1)

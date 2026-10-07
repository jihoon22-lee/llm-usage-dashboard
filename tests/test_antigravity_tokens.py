from contextlib import closing
import concurrent.futures
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from llm_usage import statusline
from llm_usage.collect import collect_antigravity_tokens,collect_status
from llm_usage.store import Store


class AntigravityTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'usage.db')
        self.inbox=self.root/'inbox';self.inbox.mkdir()
        self.settings={'status_inboxes':[str(self.inbox)]}
        self.data={'conversation_id':'private-session','model':{'id':'Gemini 3.5 Flash (High)'},'version':'1.1.28',
                   'context_window':{'total_input_tokens':100,'total_output_tokens':20,'context_window_size':1000000,
                    'current_usage':{'input_tokens':70,'output_tokens':10,'cache_read_input_tokens':30,'cache_creation_input_tokens':0}},
                   'quota':{'gemini':{'remaining_fraction':.4}},'content':'PRIVATE BODY','email':'PRIVATE EMAIL','token':'PRIVATE KEY'}

    def rows(self):
        with self.store.connect() as c:return c.execute('SELECT * FROM agy_token_observations ORDER BY last_seen').fetchall()

    def test_capture_privacy_null_zero_and_invalid_numbers(self):
        d=self.data;d['context_window']['total_input_tokens']=None
        d['context_window']['current_usage'].update(input_tokens=True,output_tokens=-1,cache_read_input_tokens=float('inf'))
        safe=statusline.token_observation(d)
        self.assertIsNone(safe['totals']['total_input_tokens'])
        self.assertEqual(safe['current'],{'cache_creation_input_tokens':0})
        self.assertEqual(len(safe['session']),64)
        self.assertNotIn('PRIVATE',json.dumps(safe));self.assertNotIn('private-session',json.dumps(safe))
        self.assertEqual(statusline.validate_observation({**safe,'content':'PRIVATE'}),safe)
        missing=statusline.token_observation({})
        self.assertEqual(missing['context_state'],'missing');self.assertEqual(missing['totals'],{})
        self.assertEqual(statusline.token_observation({'context_window':None})['context_state'],'null')

    def test_replay_copies_models_and_resets_are_observations_not_usage(self):
        now=time.time()-10
        for checked in (now,now+1):statusline.record_tokens(self.inbox,self.data,checked)
        copy=self.root/'copy';copy.mkdir();shutil.copyfile(self.inbox/'antigravity-tokens.db',copy/'antigravity-tokens.db')
        self.settings['status_inboxes'].append(str(copy))
        collect_antigravity_tokens(self.store,self.settings)
        self.assertEqual(len(self.rows()),1)
        self.assertEqual(self.rows()[0]['last_seen'],now+1)
        self.data['model']['id']='claude-test';self.data['context_window']['total_input_tokens']=5
        statusline.record_tokens(self.inbox,self.data,now+2)
        collect_antigravity_tokens(self.store,self.settings);collect_antigravity_tokens(self.store,self.settings)
        self.assertEqual(len(self.rows()),2)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)
            self.assertEqual(c.execute("SELECT status FROM sources WHERE name='antigravity-tokens'").fetchone()[0],'partial')

    def test_delayed_older_observation_and_retention(self):
        now=time.time()
        statusline.record_tokens(self.inbox,self.data,now)
        collect_antigravity_tokens(self.store,self.settings)
        self.data['context_window']['total_input_tokens']=50
        statusline.record_tokens(self.inbox,self.data,now-10)
        collect_antigravity_tokens(self.store,self.settings)
        self.assertEqual(len(self.rows()),2)
        with self.store.connect() as c:
            c.execute('UPDATE agy_token_observations SET last_seen=?',(now-31*86400,))
        # Pruning runs at most hourly on the 2-second loop.
        collect_antigravity_tokens(self.store,self.settings)
        self.assertEqual(len(self.rows()),2)
        with self.store.connect() as c:self.store.save_state(c,'agy-tokens:pruned',now-3601)
        collect_antigravity_tokens(self.store,self.settings)
        self.assertEqual(len(self.rows()),0)

    def test_api_distinguishes_observed_tokens_from_missing_usage(self):
        now=time.time()
        with self.store.connect() as c:
            self.store.source(c,'antigravity-records','unavailable','past transcript unavailable',now)
        data=self.store.usage(now=now)
        self.assertNotIn('observed',data['unavailable_routes'][0])
        statusline.record_tokens(self.inbox,self.data,now)
        collect_antigravity_tokens(self.store,self.settings)
        data=self.store.usage(now=now)
        self.assertTrue(data['unavailable_routes'][0]['observed'])
        self.assertFalse(data['unavailable_routes'][0]['observation_stale'])
        self.assertTrue(self.store.usage(now=now+601)['unavailable_routes'][0]['observation_stale'])
        self.assertEqual(data['rows'],[])

    def test_parallel_callbacks_preserve_intermediate_snapshots(self):
        def save(n):
            d={**self.data,'context_window':{'total_input_tokens':n}}
            statusline.record_tokens(self.inbox,d,time.time())
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(save,range(20)))
        collect_antigravity_tokens(self.store,self.settings)
        self.assertEqual(len(self.rows()),20)

    def test_missing_fields_and_corrupt_inbox_report_separately(self):
        collect_antigravity_tokens(self.store,self.settings)
        with self.store.connect() as c:self.assertIn('수신 대기',c.execute("SELECT detail FROM sources WHERE name='antigravity-tokens'").fetchone()[0])
        statusline.record_tokens(self.inbox,{},time.time())
        collect_antigravity_tokens(self.store,self.settings)
        with self.store.connect() as c:self.assertIn('미제공',c.execute("SELECT detail FROM sources WHERE name='antigravity-tokens'").fetchone()[0])
        with closing(sqlite3.connect(self.inbox/'antigravity-tokens.db')) as c, c:
            c.execute('INSERT INTO observations VALUES (?,?,?,?)',('bad',time.time(),time.time(),'{'))
        collect_antigravity_tokens(self.store,self.settings)
        self.assertEqual(len(self.rows()),1)
        with self.store.connect() as c:self.assertEqual(c.execute("SELECT status FROM sources WHERE name='antigravity-tokens'").fetchone()[0],'error')

    def test_wrapper_preserves_original_io_and_quota_on_token_failure(self):
        original=self.root/'original.py'
        original.write_text('import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\nsys.exit(7)\n')
        (self.inbox/'antigravity-original.json').write_text(json.dumps({'command':f'"{sys.executable}" "{original}"'}))
        raw=json.dumps(self.data).encode()
        command=[sys.executable,statusline.__file__,'--route','antigravity','--directory',str(self.inbox)]
        result=subprocess.run(command,input=raw,capture_output=True)
        self.assertEqual(result.returncode,7);self.assertEqual(result.stdout,raw)
        collect_status(self.store,self.settings)
        self.assertEqual(len(self.rows()),1)
        self.assertEqual(self.store.limits()['limits'][0]['remaining'],40)
        self.assertNotIn(b'PRIVATE',(self.inbox/'antigravity-tokens.db').read_bytes())
        (self.inbox/'antigravity-tokens.db').unlink();(self.inbox/'antigravity-tokens.db').mkdir()
        result=subprocess.run(command,input=raw,capture_output=True)
        self.assertEqual(result.returncode,7);self.assertEqual(result.stdout,raw)
        self.assertEqual(json.loads((self.inbox/'antigravity.json').read_text())['data']['quota']['gemini']['remaining_fraction'],.4)

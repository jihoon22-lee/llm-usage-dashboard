"""Upgrading incremental Codex checkpoints must not relabel historical events."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from llm_usage.collect import collect_file
from llm_usage.store import Store

class CheckpointProjectTests(unittest.TestCase):
    def test_legacy_checkpoint_normalizes_only_new_event_ids(self):
        for legacy,expected in [(r'C:\Private\app','app'),('/private/projects','projects'),
                                (r'\\server\share\app','app'),('/',None),('ordinary','ordinary')]:
            with self.subTest(legacy=legacy),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);path=root/'session.jsonl';store=Store(root/'usage.db')
                def event(n):
                    return {'type':'event_msg','timestamp':f'2026-09-08T01:0{n}:00Z',
                        'payload':{'type':'token_count','info':{
                            'total_token_usage':{'input_tokens':100*n,'output_tokens':10*n},
                            'last_token_usage':{'input_tokens':100,'output_tokens':10}}}}
                meta={'type':'session_meta','payload':{'id':'fixture-session','cwd':legacy}}
                records=[meta,{'type':'turn_context','payload':{'model':'gpt-test'}},event(1)]
                path.write_text(''.join(json.dumps(r)+'\n' for r in records))
                # Reproduce a checkpoint emitted by the previous path-label contract.
                with patch('llm_usage.collect.project_label',return_value=legacy):collect_file(store,path,'codex')
                with path.open('a') as f:f.write(json.dumps(event(2))+'\n')
                store=Store(root/'usage.db');collect_file(store,path,'codex')
                with store.connect() as c:
                    self.assertEqual([tuple(r) for r in c.execute('SELECT project,output FROM events ORDER BY ts')],
                                     [(legacy,10),(expected,10)])
                    self.assertEqual(store.state(c,'file:'+str(path))['parser']['project'],expected)
                copied=root/'copy.jsonl';shutil.copyfile(path,copied);collect_file(store,copied,'codex')
                with path.open('a') as f:f.write(json.dumps(event(3))+'\n')
                collect_file(store,path,'codex');collect_file(store,path,'codex')
                with store.connect() as c:
                    self.assertEqual([tuple(r) for r in c.execute('SELECT project,output FROM events ORDER BY ts')],
                                     [(legacy,10),(expected,10),(expected,10)])
                self.assertEqual(store.usage(period='all')['totals']['output'],30)

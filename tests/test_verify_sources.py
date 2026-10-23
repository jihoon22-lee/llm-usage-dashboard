"""Verification detects ingestion omissions by independently reading source data."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from llm_usage.collect import collect_file,collect_opencode
from llm_usage.record_files import zstd
from llm_usage.store import Store,identity
from llm_usage.verify import failed,partial,reconcile
from llm_usage.verify_sources import reconcile_sources
from test_native_usage import meta,model,response,usage
from test_usage_details import message


class OriginalSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'usage.db')
        self.codex=self.root/'codex';self.codex.mkdir();self.claude=self.root/'claude';self.claude.mkdir()
        self.cfg={'homes':[],'sources':[dict(name=name,kind='jsonl',route=route,path=str(folder))
                  for name,route,folder in [('codex','codex',self.codex),('claude','claude-code',self.claude)]]}

    def source(self,rows,route='codex',name='record.jsonl'):
        path=(self.codex if route=='codex' else self.claude)/name
        content=''.join(json.dumps(row)+'\n' for row in rows)
        if name.endswith('.zst'):
            with zstd.open(path,'wt') as f:f.write(content)
        else:path.write_text(content)
        collect_file(self.store,path,route)
        return path

    def test_native_original_detects_silently_ignored_responses_even_if_events_and_points_agree(self):
        self.source([meta(),model(),response('one',usage(100,20))])
        result=reconcile(self.store.path,self.cfg)
        self.assertFalse(failed(result));self.assertEqual(result['codex_original']['matched_requests'],1)
        with self.store.connect() as c:
            c.execute('DELETE FROM events');c.execute('DELETE FROM codex_responses')
        result=reconcile(self.store.path,self.cfg)
        self.assertEqual(result['codex']['mismatches'],0)
        self.assertEqual(result['codex_original']['missing_events'],1);self.assertTrue(failed(result))

    def test_compressed_copy_is_checked_once_per_original_response(self):
        rows=[meta(),model(),response('one',usage(100,20))]
        self.source(rows,name='record.jsonl.zst');self.source(rows,name='copy.jsonl')
        counts=reconcile_sources(self.store.path,self.cfg)['codex_original']
        self.assertEqual((counts['source_files'],counts['requests'],counts['matched_requests']),(2,1,1))

    def test_appends_and_replacements_are_pending_until_the_collector_reads_them(self):
        path=self.source([meta(),model(),response('one',usage(100,20))])
        with path.open('a') as f:f.write(json.dumps(response('two',usage(10,2)))+'\n')
        result=reconcile(self.store.path,self.cfg);counts=result['codex_original']
        self.assertEqual((counts['requests'],counts['pending_files'],counts['missing_events']),(1,1,0))
        self.assertFalse(failed(result));self.assertTrue(partial(result))
        path.write_text(json.dumps(meta('replacement'))+'\n')
        counts=reconcile_sources(self.store.path,self.cfg)['codex_original']
        self.assertEqual((counts['requests'],counts['pending_files'],counts['missing_events']),(0,1,0))

    def test_deleted_advisor_and_changed_detail_are_failures(self):
        self.source([message()],'claude-code')
        counts=reconcile_sources(self.store.path,self.cfg)['claude']
        self.assertEqual((counts['requests'],counts['advisor_calls'],counts['matched_requests']),(2,1,2))
        with self.store.connect() as c:
            c.execute('DELETE FROM events WHERE parent_id IS NOT NULL')
            c.execute('UPDATE events SET cache_creation_1h=cache_creation_1h-1')
        result=reconcile(self.store.path,self.cfg)
        self.assertEqual((result['claude']['missing_events'],result['claude']['mismatched_events']),(1,1))
        self.assertTrue(failed(result))

    def test_retained_larger_historical_snapshot_is_partial_not_a_false_mismatch(self):
        row=message();row['message']['usage'].pop('iterations')
        self.source([row],'claude-code')
        row['message']['usage']['output_tokens']=30
        self.source([row],'claude-code')
        result=reconcile(self.store.path,self.cfg)
        self.assertEqual((result['claude']['retained_larger'],result['claude']['mismatched_events']),(1,0))
        self.assertFalse(failed(result));self.assertTrue(partial(result))

    def test_conflicting_original_ttl_is_unknown_and_invalid_details_do_not_drop_parent(self):
        row=message();self.source([row],'claude-code')
        row['message']['usage']['cache_creation']={'ephemeral_5m_input_tokens':20,'ephemeral_1h_input_tokens':10}
        self.source([row],'claude-code',name='copy.jsonl')
        conflict=reconcile(self.store.path,self.cfg)
        self.assertEqual(conflict['claude']['mismatched_events'],0);self.assertTrue(partial(conflict))
        row['message']['usage']['cache_creation']['ephemeral_1h_input_tokens']=11
        self.source([row],'claude-code',name='bad-detail.jsonl')
        result=reconcile(self.store.path,self.cfg)
        self.assertEqual(result['claude']['detail_inconsistent_records'],1)
        self.assertEqual(result['claude']['matched_requests'],2);self.assertFalse(failed(result));self.assertTrue(partial(result))

    def test_bad_json_in_committed_prefix_is_visible_as_partial(self):
        path=self.source([meta(),model(),response('one',usage(100,20))])
        with path.open('a') as f:f.write('broken JSON\n')
        collect_file(self.store,path,'codex')
        result=reconcile(self.store.path,self.cfg)
        self.assertEqual(result['codex_original']['unreadable_records'],1)
        self.assertTrue(partial(result));self.assertFalse(failed(result))
        with self.store.connect() as c:self.store.save_state(c,'verify',dict(ts=time.time(),ok=True,**result))
        source=next(s for s in self.store.usage(period='all')['sources'] if s['name']=='원본 대조')
        self.assertEqual(source['status'],'partial');self.assertIn('일부만 확인',source['detail'])

    def test_opencode_source_and_lag_are_checked_without_reusing_its_parser(self):
        path=self.root/'opencode.db'
        data=dict(role='assistant',modelID='gpt-test',providerID='opencode-go',
                  tokens=dict(input=10,output=20,reasoning=5,cache=dict(read=30,write=4),total=69))
        with closing(sqlite3.connect(path)) as c, c:
            c.execute('CREATE TABLE message(id TEXT PRIMARY KEY,time_created INTEGER,time_updated INTEGER,data TEXT)')
            c.execute('INSERT INTO message VALUES (?,?,?,?)',('one',1000,1000,json.dumps(data)))
        self.cfg['sources'].append(dict(kind='opencode',path=str(path)))
        collect_opencode(self.store,path)
        with closing(sqlite3.connect(path)) as c, c:
            c.execute('INSERT INTO message VALUES (?,?,?,?)',('new',2000,2000,json.dumps(data)))
        counts=reconcile_sources(self.store.path,self.cfg)['opencode']
        self.assertEqual((counts['requests'],counts['matched_requests'],counts['pending_records'],counts['detail_inconsistent_records']),(1,1,1,0))
        with self.store.connect() as c:c.execute('DELETE FROM events WHERE id=?',(identity('opencode','one'),))
        result=reconcile(self.store.path,self.cfg)
        self.assertEqual(result['opencode']['missing_events'],1);self.assertTrue(failed(result))

    def test_parser_replay_reports_committed_progress_without_claiming_completion(self):
        from llm_usage.collect import collect_local
        path=self.codex/'new.jsonl';path.write_text(json.dumps(meta())+'\n')
        clock=[0]
        with self.store.connect() as c:self.store.save_state(c,'collector',dict(status='collecting',started=1,checked=1))
        def ingest(store,record,route):
            result=collect_file(store,record,route);clock[0]=6;return result
        with patch('llm_usage.collect.time.monotonic',side_effect=lambda:clock[0]),\
             patch('llm_usage.collect.collect_file',side_effect=ingest),patch('llm_usage.collect.sd_notify') as notify:
            collect_local(self.store,self.cfg,include_derived=False)
        with self.store.connect() as c:
            state=self.store.state(c,'collector');self.assertEqual(state['status'],'collecting')
            self.assertGreater(state['checked'],1);self.assertTrue(self.store.state(c,'file:'+str(path)))
        notify.assert_called_with('WATCHDOG=1')

    def test_upgrade_rechecks_sources_even_when_the_old_daily_check_is_fresh(self):
        from llm_usage.collect import run
        from llm_usage.verify import VERIFY_FORMAT
        with self.store.connect() as c:
            self.store.save_state(c,'normalized_version',4)
            self.store.save_state(c,'verify',dict(ts=200000,ok=True))
            self.store.save_state(c,'backup',dict(ts=200000,ok=True))
        result=reconcile(self.store.path,self.cfg)
        with patch('llm_usage.collect.collect_status'),patch('llm_usage.collect.poll_limits'),\
             patch('llm_usage.collect.collect_local'),patch('llm_usage.collect.time.time',return_value=200000),\
             patch('llm_usage.collect.time.sleep',side_effect=StopIteration),\
             patch('llm_usage.verify.reconcile',return_value=result) as verify:
            with self.assertRaises(StopIteration):run({'database':self.store.path})
        verify.assert_called_once()
        with self.store.connect() as c:self.assertEqual(self.store.state(c,'verify')['format'],VERIFY_FORMAT)


if __name__=='__main__':unittest.main()

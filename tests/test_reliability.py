"""Failures and boundaries from the reliability review, using disposable data only."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from llm_usage.collect import collect_file, collect_local, collect_opencode, prepare_normalization, retire_legacy_sources, run, sync_thresholds
from llm_usage.store import Store, stamp


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'usage.db')

    def point(self,c,session,index,inp,out,last_inp=100,last_out=10):
        return self.store.codex_point(c,session,1000+index,'OpenAI','codex','gpt-test',
            dict(input_tokens=inp,output_tokens=out),dict(input_tokens=last_inp,output_tokens=last_out))

    def test_reset_can_repeat_old_counters_and_notifications(self):
        with self.store.connect() as c:
            for i,(inp,out) in enumerate([(100,10),(200,20),(100,10),(100,10),(200,20),(300,30)]):
                sid=self.point(c,'reset',i,inp,out)
            self.store.rebuild_codex(c,sid)
            quality=dict(c.execute('SELECT * FROM session_quality').fetchone())
        result=self.store.usage(period='all')
        self.assertEqual(result['totals']['uncached_input'],500)
        self.assertEqual(result['totals']['output'],50)
        self.assertEqual(quality['resets'],1)
        with self.store.connect() as c:self.store.rebuild_codex(c,sid)
        self.assertEqual(self.store.usage(period='all')['totals'],result['totals'])

    def test_existing_points_repaired_without_original_files(self):
        with self.store.connect() as c:
            for i,inp in enumerate([100,200,100,200]):sid=self.point(c,'saved',i,inp,inp//10)
            self.store.save_state(c,'normalized_version',3)
        self.assertTrue(prepare_normalization(self.store,{'sources':[]}))
        self.assertEqual(self.store.usage(period='all')['totals']['uncached_input'],400)
        with self.store.connect() as c:self.assertEqual(self.store.state(c,'normalized_version'),4)

    def test_zero_request_baseline_changes_never_create_consumption(self):
        with self.store.connect() as c:
            for i,(inp,out,last_inp,last_out) in enumerate([(1000,100,1000,100),(200,20,0,0),
                    (1100,110,100,10),(4000,400,0,0),(4100,410,100,10)]):
                sid=self.point(c,'baselines',i,inp,out,last_inp,last_out)
            self.store.rebuild_codex(c,sid)
        result=self.store.usage(period='all')
        self.assertEqual(result['totals']['uncached_input'],1200)
        self.assertEqual(result['totals']['output'],120)
        self.assertEqual(result['insights']['quality']['ambiguous_sessions'],1)

    def test_legacy_migration_keeps_missing_originals_and_version(self):
        with self.store.connect() as c:
            self.store.save_state(c,'normalized_version',2)
            self.store.event(c,'retained',1000,'OpenAI','codex','gpt-test',dict(uncached_input=100))
        config={'sources':[dict(name='Windows Codex',path=str(self.root/'missing'),route='codex',kind='jsonl')]}
        self.assertFalse(prepare_normalization(self.store,config))
        self.assertEqual(self.store.usage(period='all')['totals']['uncached_input'],100)
        with self.store.connect() as c:self.assertEqual(self.store.state(c,'normalized_version'),2)
        self.assertFalse(list(self.root.glob('normalization-*')))

    def codex_file(self,path,counters):
        records=[dict(type='session_meta',payload=dict(id='migration',model_provider='openai')),
                 dict(type='turn_context',payload=dict(model='gpt-test'))]
        for i,inp in counters:
            records.append(dict(type='event_msg',timestamp=1000+i,payload=dict(type='token_count',info=dict(
                total_token_usage=dict(input_tokens=inp,output_tokens=inp//10),last_token_usage=dict(input_tokens=100,output_tokens=10)))))
        path.write_text(''.join(json.dumps(row)+'\n' for row in records))

    def test_legacy_migration_requires_all_observations_not_just_session(self):
        path=self.root/'session.jsonl';self.codex_file(path,[(0,100),(1,200)])
        collect_file(self.store,path,'codex')
        with self.store.connect() as c:self.store.save_state(c,'normalized_version',2)
        self.codex_file(path,[(1,200)])
        config={'sources':[dict(name='Codex',path=str(self.root),route='codex',kind='jsonl')],
                'homes':[str(self.root/'no-antigravity-history')]}
        self.assertFalse(prepare_normalization(self.store,config))
        self.assertEqual(self.store.usage(period='all')['totals']['uncached_input'],200)
        self.codex_file(path,[(0,100),(1,200)])
        self.assertTrue(prepare_normalization(self.store,config))
        self.assertEqual(self.store.usage(period='all')['totals']['uncached_input'],200)

    def test_opencode_bad_row_does_not_block_and_retries_old_timestamp(self):
        path=self.root/'opencode.db'
        good=json.dumps(dict(role='assistant',modelID='gpt-test',tokens=dict(input=10,output=2)))
        with closing(sqlite3.connect(path)) as c, c:
            c.execute('CREATE TABLE message(id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT)')
            c.executemany('INSERT INTO message VALUES (?,?,?,?)',[('bad',1000000,1,'PRIVATE INVALID'),('good',2000000,2,good)])
        self.assertEqual(collect_opencode(self.store,path),1)
        self.assertEqual(self.store.usage(period='all')['totals']['uncached_input'],10)
        with self.store.connect() as c:
            self.assertEqual(self.store.state(c,'opencode:'+str(path)),2)
            self.assertNotIn('PRIVATE',str([tuple(r) for r in c.execute('SELECT * FROM import_errors')]))
        with closing(sqlite3.connect(path)) as c, c:c.execute('UPDATE message SET data=? WHERE id=?',(good,'bad'))
        self.assertEqual(collect_opencode(self.store,path),0)
        self.assertEqual(self.store.usage(period='all')['totals']['uncached_input'],20)
        self.assertEqual(collect_opencode(self.store,path),0)
        self.assertEqual(self.store.usage(period='all')['totals']['uncached_input'],20)

    def test_jsonl_error_survives_checkpoint_and_source_reports_partial(self):
        path=self.root/'bad.jsonl';path.write_text('{\n')
        config={'sources':[dict(name='Claude',path=str(self.root),route='claude-code',kind='jsonl')]}
        collect_local(self.store,config);collect_local(self.store,config)
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM sources WHERE name='Claude'").fetchone()[0],'partial')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM import_errors').fetchone()[0],1)
        path.write_text('{}\n');collect_local(self.store,config)
        # A changed-size rewrite may look like an append. Explicitly replacing its inode
        # is the supported recovery for earlier malformed lines in an append-only stream.
        replacement=self.root/'fixed';replacement.write_text('{}\n');replacement.replace(path)
        collect_local(self.store,config)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM import_errors').fetchone()[0],0)

    def test_aged_file_error_becomes_permanent_count(self):
        path=self.root/'bad.jsonl';path.write_text('{\n')
        config={'sources':[dict(name='Claude',path=str(self.root),route='claude-code',kind='jsonl')]}
        collect_local(self.store,config)
        with self.store.connect() as c:
            c.execute('UPDATE import_errors SET checked=?',(time.time()-8*86400,))
        collect_local(self.store,config)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM import_errors').fetchone()[0],0)
            self.assertEqual(self.store.state(c,'permanent_errors:Claude'),1)
            row=c.execute("SELECT status,detail FROM sources WHERE name='Claude'").fetchone()
            self.assertEqual(row['status'],'ok')
            self.assertIn('누적 영구 실패 1건',row['detail'])

    def test_missing_opencode_rows_do_not_starve_repaired_rows(self):
        path=self.root/'opencode.db';key='opencode:'+str(path)
        with closing(sqlite3.connect(path)) as c, c:
            c.execute('CREATE TABLE message(id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT)')
            c.execute('INSERT INTO message VALUES (?,?,?,?)',('zz-repaired',1000000,1,
                json.dumps(dict(role='assistant',modelID='gpt-test',tokens=dict(input=10)))))
        with self.store.connect() as c:
            self.store.save_state(c,key,2)
            with patch('llm_usage.store.time.time',return_value=1):
                for i in range(200):self.store.import_error(c,key,f'missing-{i:03d}','JSONDecodeError')
                self.store.import_error(c,key,'zz-repaired','JSONDecodeError')
        collect_opencode(self.store,path)
        collect_opencode(self.store,path)
        self.assertEqual(self.store.usage(period='all')['totals']['uncached_input'],10)
        with self.store.connect() as c:self.assertEqual(self.store.error_count(c,key),200)

    def test_request_arriving_during_collection_gets_another_pass(self):
        with self.store.connect() as c:
            self.store.save_state(c,'normalized_version',4)
            self.store.save_state(c,'refresh_request_id',1)
        clock=[100.0];requests=[];completed=[]
        def local(store,settings):
            with store.connect() as c:
                requests.append(store.state(c,'refresh_request_id'))
                if len(requests)==1:store.save_state(c,'refresh_request_id',2)
            clock[0]+=10
        def sleep(seconds):
            with self.store.connect() as c:completed.append(self.store.state(c,'refresh_completed_id'))
            clock[0]+=seconds
            if len(completed)==2:raise StopIteration
        with patch('llm_usage.collect.collect_status'),patch('llm_usage.collect.poll_limits'),patch('llm_usage.collect.collect_local',side_effect=local),patch('llm_usage.collect.time.time',side_effect=lambda:clock[0]),patch('llm_usage.collect.time.sleep',side_effect=sleep):
            with self.assertRaises(StopIteration):run({'database':self.store.path})
        self.assertEqual(requests,[1,2]);self.assertEqual(completed,[1,2])

    def test_five_minute_collection_and_manual_request_before_next_interval(self):
        with self.store.connect() as c:self.store.save_state(c,'normalized_version',4)
        clock=[1000.0];ticks=iter([1299.0,1300.0,1310.0]);collected=[]
        def local(store,settings):collected.append(clock[0])
        def sleep(seconds):
            clock[0]=next(ticks)
            if clock[0]==1310:
                with self.store.connect() as c:
                    self.store.save_state(c,'refresh_request_id',1)
                    self.store.save_state(c,'refresh_requested',clock[0])
        with patch('llm_usage.collect.collect_status'),patch('llm_usage.collect.poll_limits') as poll,patch('llm_usage.collect.collect_local',side_effect=local),patch('llm_usage.collect.time.time',side_effect=lambda:clock[0]),patch('llm_usage.collect.time.sleep',side_effect=sleep):
            with self.assertRaises(StopIteration):run({'database':self.store.path})
        self.assertEqual(collected,[1000.0,1300.0,1310.0])
        self.assertEqual(poll.call_count,3)
        with self.store.connect() as c:self.assertEqual(self.store.state(c,'refresh_completed_id'),1)


    def test_collector_applies_web_edited_thresholds_without_restart(self):
        settings={'database':self.store.path}
        seen=sync_thresholds(self.store,settings,False)
        self.assertEqual(self.store.thresholds['retention_days'],30)
        (Path(self.store.path).parent/'local.json').write_text(json.dumps({'thresholds':{'stale_seconds':600,'retention_days':3,'low_percent':15}}))
        sync_thresholds(self.store,settings,seen)
        now=time.time()
        with self.store.connect() as c:
            # Five days old: kept under the old 30-day retention, dropped under the new 3 days.
            self.store.limit(c,'codex','codex · 300분',50,now+3600,now-5*86400,'codex')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM limit_history').fetchone()[0],0)


    def test_failed_backup_and_verify_retry_after_an_hour(self):
        with self.store.connect() as c:self.store.save_state(c,'normalized_version',4)
        clock=[200000.0];ticks=iter([201000.0,203700.0]);backups=[];verifies=[]
        def backup(database):
            backups.append(clock[0]);return dict(ts=clock[0],ok=False,error='disk full')
        def reconcile(database,settings):
            verifies.append(clock[0]);raise RuntimeError('source locked')
        def sleep(seconds):clock[0]=next(ticks)
        with patch('llm_usage.collect.collect_status'),patch('llm_usage.collect.poll_limits'),patch('llm_usage.collect.collect_local'),\
             patch('llm_usage.backup.backup_now',side_effect=backup),patch('llm_usage.verify.reconcile',side_effect=reconcile),\
             patch('llm_usage.collect.time.time',side_effect=lambda:clock[0]),patch('llm_usage.collect.time.sleep',side_effect=sleep):
            with self.assertRaises(StopIteration):run({'database':self.store.path})
        # A failure is retried an hour later (203,700), not a day later; 201,000 is too soon.
        self.assertEqual(backups,[200000.0,203700.0])
        self.assertEqual(verifies,[200000.0,203700.0])


    def test_retired_source_rows_are_removed(self):
        with self.store.connect() as c:
            self.store.source(c,'codex-records','partial','일부 기록 형식을 해석하지 못했습니다.',1000)
            self.store.source(c,'WSL .codex/sessions','ok','',1000)
        retire_legacy_sources(self.store)
        names={s['name'] for s in self.store.usage(period='today')['sources']}
        self.assertNotIn('codex-records',names)
        self.assertIn('WSL .codex/sessions',names)


class InsightTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'usage.db')
        self.now=stamp('2026-09-09T03:00:00Z')  # Noon KST.

    def event(self,key,date,model='gpt-test',**tokens):
        with self.store.connect() as c:self.store.event(c,key,stamp(date),'OpenAI','codex',model,tokens)

    def test_comparison_aligns_elapsed_time_and_includes_creation_once(self):
        self.event('baseline','2026-09-07T00:00:00+09:00',uncached_input=1)
        self.event('before','2026-09-08T10:00:00+09:00',uncached_input=10,cache_creation=10,output=5,reasoning=3)
        self.event('excluded','2026-09-08T13:00:00+09:00',uncached_input=1000)
        self.event('today','2026-09-09T10:00:00+09:00',uncached_input=20,cache_creation=20,output=10,reasoning=6)
        result=self.store.usage(period='today',now=self.now)['insights']['comparison']
        self.assertEqual(result['current'],50);self.assertEqual(result['previous'],25)
        self.assertEqual(result['percent'],100);self.assertEqual(result['elapsed_seconds'],12*3600)
        self.assertEqual(sum(r['delta'] for r in result['contributions']),25)
        self.assertEqual(result['previous_end_exclusive'],'2026-09-08T12:00:00+09:00')

    def test_comparison_respects_scope(self):
        self.event('baseline','2026-08-20T00:00:00+09:00',uncached_input=1)
        self.event('prev','2026-08-30T00:00:00+09:00',uncached_input=1000)
        self.event('cur','2026-09-05T00:00:00+09:00',uncached_input=10)
        with self.store.connect() as c:
            self.store.event(c,'other-prev',stamp('2026-08-30T00:00:00+09:00'),'Anthropic','claude-code','claude-x',dict(uncached_input=2000))
            self.store.event(c,'other-cur',stamp('2026-09-05T00:00:00+09:00'),'Anthropic','claude-code','claude-x',dict(uncached_input=5000))
        scoped=self.store.usage(period='7d',now=self.now,scope='route:codex')['insights']['comparison']
        self.assertEqual((scoped['current'],scoped['previous']),(10,1000))
        self.assertEqual({r['route'] for r in scoped['contributions']},{'codex'})
        full=self.store.usage(period='7d',now=self.now)['insights']['comparison']
        self.assertEqual((full['current'],full['previous']),(5010,3000))

    def test_zero_insufficient_all_future_and_disappearing_model(self):
        self.event('today','2026-09-09T10:00:00+09:00',uncached_input=10)
        result=self.store.usage(period='today',now=self.now)['insights']['comparison']
        self.assertEqual(result['status'],'insufficient_history');self.assertIsNone(result['percent'])
        self.assertEqual(self.store.usage(period='all',now=self.now)['insights']['comparison']['status'],'unavailable')
        self.assertEqual(self.store.usage(period='custom',start='2026-09-10',end='2026-09-10',now=self.now)['insights']['comparison']['status'],'unavailable')
        self.event('baseline','2026-09-07T00:00:00+09:00',uncached_input=1)
        result=self.store.usage(period='today',now=self.now)['insights']['comparison']
        self.assertEqual(result['status'],'observed');self.assertIsNone(result['percent'])
        self.event('old-model','2026-09-08T10:00:00+09:00',model='gpt-old',uncached_input=30)
        result=self.store.usage(period='today',now=self.now)['insights']['comparison']
        self.assertEqual(result['contributions'][0]['delta'],-30)

    def test_cache_denominator_zero_missing_bucket_and_unknown_scope(self):
        self.event('inputs','2026-09-09T10:00:00+09:00',model='unknown',uncached_input=10,cached_input=30,cache_creation=20,output=99,reasoning=90)
        self.event('output-only','2026-09-09T11:00:00+09:00',output=1)
        result=self.store.usage(period='today',granularity='hour',now=self.now)['insights']
        self.assertEqual(result['cache']['rate'],50)
        self.assertIsNone(next(r for r in result['cache']['rows'] if r['model']=='gpt-test')['rate'])
        self.assertEqual(result['quality']['unknown_model_tokens'],159)
        self.assertAlmostEqual(result['quality']['unknown_model_percent'],159/160*100)
        self.assertEqual(len(result['cache']['series']),2)  # No invented measurements for empty hours.

    def test_history_dedup_retention_original_reset_and_failure_gap(self):
        with patch('llm_usage.store.time.time',return_value=self.now):
            with self.store.connect() as c:
                for checked,value in [(self.now-31*86400,99),(self.now-600,80),(self.now-300,75),(self.now,70)]:
                    self.store.limit(c,'codex','weekly',value,self.now+3600,checked,'codex')
                self.store.limit(c,'codex','weekly',70,self.now+3600,self.now,'codex')
            result=self.store.limits(self.now)['limits'][0]
            self.assertEqual(len(result['history']),3)
            self.assertEqual(result['pace_per_hour'],60)
            # Ten observed minutes is below the forecast floor, so the pace is
            # reported for display but no depletion forecast is produced.
            self.assertEqual(result['pace_minutes'],10)
            self.assertIsNone(result['forecast'])
            with self.store.connect() as c:self.store.source(c,'codex','error','failure',self.now-100)
            result=self.store.limits(self.now)['limits'][0]
            self.assertIsNone(result['pace_per_hour']);self.assertTrue(result['history'][-1]['break_before'])
            with self.store.connect() as c:self.store.source(c,'codex','ok','',self.now)
            self.assertIsNone(self.store.limits(self.now)['limits'][0]['pace_per_hour'])
            with patch('llm_usage.store.time.time',return_value=self.now+300),self.store.connect() as c:
                self.store.limit(c,'codex','weekly',100,self.now+7200,self.now+300,'codex')
            result=self.store.limits(self.now+300)['limits'][0]
            self.assertTrue(result['history'][-1]['break_before']);self.assertIsNone(result['pace_per_hour'])
            self.assertEqual(self.store.limits(self.now+8000)['limits'][0]['remaining'],100)
            self.assertEqual(self.store.limits(self.now+8000)['limits'][0]['status'],'stale')


if __name__=='__main__':unittest.main()

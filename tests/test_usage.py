from contextlib import closing
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from llm_usage.backup import backup_now
from llm_usage.collect import collect_file, collect_opencode, collect_status
from llm_usage.config import settings
from llm_usage.limits import codex_limits, status_limits, go_limits
from llm_usage.pricing import rate_for
from llm_usage.store import Store, stamp
from llm_usage.statusline import sanitize
from llm_usage.webapp import create_app


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'usage.db')

    def write(self,path,records,mode='w'):
        with path.open(mode) as f:
            for d in records:f.write(json.dumps(d)+'\n')

    def codex(self,usage,ts='2026-09-07T15:00:00Z'):
        return {'type':'event_msg','timestamp':ts,'payload':{'type':'token_count','info':{'total_token_usage':usage,'last_token_usage':usage}}}

    def test_codex_incremental_copies_repeated_notifications_partial_line(self):
        path=self.root/'session.jsonl'
        meta={'type':'session_meta','payload':{'id':'remote-pc-session','model_provider':'openai'}}
        model={'type':'turn_context','payload':{'model':'gpt-test'}}
        first={'input_tokens':100,'cached_input_tokens':70,'output_tokens':20,'reasoning_output_tokens':5}
        second={'input_tokens':250,'cached_input_tokens':170,'output_tokens':50,'reasoning_output_tokens':15}
        self.write(path,[meta,model,self.codex(first),self.codex(first,'2026-09-07T15:00:01Z')])
        collect_file(self.store,path,'codex')
        self.write(path,[self.codex(second)],'a');collect_file(self.store,path,'codex')
        copy=self.root/'windows-copy.jsonl';shutil.copyfile(path,copy)
        collect_file(self.store,copy,'codex');collect_file(self.store,path,'codex')
        totals=self.store.usage(period='all')['totals']
        self.assertEqual(totals,dict(uncached_input=80,cached_input=170,output=50,cache_creation=0,reasoning=15,requests=2))
        with path.open('a') as f:f.write('{"type":')
        collect_file(self.store,path,'codex')
        self.assertEqual(self.store.usage(period='all')['totals'],totals)
        with self.store.connect() as c:self.assertEqual(c.execute('select count(*) from events').fetchone()[0],2)

    def test_partial_copy_then_full_history_converges(self):
        meta={'type':'session_meta','payload':{'id':'remote-pc-session'}}
        model={'type':'turn_context','payload':{'model':'gpt-test'}}
        a=self.codex(dict(input_tokens=100,cached_input_tokens=50,output_tokens=10),'2026-09-07T10:00:00Z')
        b=self.codex(dict(input_tokens=300,cached_input_tokens=150,output_tokens=30),'2026-09-07T11:00:00Z')
        b['payload']['info']['last_token_usage']=dict(input_tokens=200,cached_input_tokens=100,output_tokens=20)
        partial=self.root/'partial.jsonl';self.write(partial,[meta,model,b]);collect_file(self.store,partial,'codex')
        self.assertEqual(self.store.usage(period='all')['totals']['output'],20)
        full=self.root/'full.jsonl';self.write(full,[meta,model,a,b]);collect_file(self.store,full,'codex')
        self.assertEqual(self.store.usage(period='all')['totals']['output'],30)
        self.assertEqual(self.store.usage(period='all')['totals']['uncached_input'],150)
        collect_file(self.store,partial,'codex')
        self.assertEqual(self.store.usage(period='all')['totals']['output'],30)

    def test_fork_embedded_parent_metadata_does_not_merge_counters(self):
        parent=self.root/'parent.jsonl';child=self.root/'child.jsonl'
        meta={'type':'session_meta','payload':{'id':'parent','timestamp':'2026-09-07T01:00:00Z'}}
        child_meta={'type':'session_meta','payload':{'id':'child','session_id':'parent','forked_from_id':'parent','timestamp':'2026-09-07T05:00:00Z'}}
        model={'type':'turn_context','payload':{'model':'gpt-test'}}
        old=self.codex(dict(input_tokens=1000,output_tokens=100),'2026-09-07T04:00:00Z')
        new=self.codex(dict(input_tokens=1100,output_tokens=110),'2026-09-07T06:00:00Z')
        own=self.codex(dict(input_tokens=50,output_tokens=5),'2026-09-07T05:10:00Z')
        self.write(parent,[meta,model,old,new]);self.write(child,[child_meta,meta,model,old,own])
        collect_file(self.store,parent,'codex');collect_file(self.store,child,'codex')
        self.assertEqual(self.store.usage(period='all')['totals']['output'],115)
        self.assertEqual(self.store.usage(period='all')['totals']['uncached_input'],1150)

    def test_fork_rewritten_replay_is_removed_and_existing_checkpoints_repaired(self):
        child=self.root/'child.jsonl'
        meta={'type':'session_meta','payload':{'id':'child','forked_from_id':'parent','timestamp':'2026-09-07T05:00:00Z'}}
        replay=self.codex(dict(input_tokens=1000,cached_input_tokens=700,output_tokens=100),'2026-09-07T05:00:00.100Z')
        own=self.codex(dict(input_tokens=50,cached_input_tokens=20,output_tokens=5),'2026-09-07T05:10:00Z')
        model={'type':'turn_context','timestamp':'2026-09-07T05:00:01Z','payload':{'model':'gpt-child'}}
        self.write(child,[meta,replay,model,own])
        # Simulate the already imported legacy bug and its unchanged-file checkpoint.
        with self.store.connect() as c:
            sid=self.store.codex_point(c,'child',stamp(replay['timestamp']),'Unknown','codex','unknown',replay['payload']['info']['total_token_usage'],replay['payload']['info']['last_token_usage'])
            self.store.rebuild_codex(c,sid)
            stat=child.stat()
            self.store.save_state(c,'file:'+str(child),dict(offset=stat.st_size,size=stat.st_size,mtime=stat.st_mtime_ns,inode=stat.st_ino,
                parser={'session':'child','provider':'openai','model':'gpt-child','fork_started':stamp(meta['payload']['timestamp'])}))
        collect_file(self.store,child,'codex')
        data=self.store.usage(period='all')
        self.assertEqual(data['totals']['output'],5)
        self.assertEqual(data['totals']['uncached_input'],30)
        self.assertEqual([row['model'] for row in data['rows']],['gpt-child'])
        collect_file(self.store,child,'codex')
        self.assertEqual(self.store.usage(period='all')['totals'],data['totals'])

    def test_genuine_unknown_model_is_not_guessed_or_discarded(self):
        path=self.root/'root.jsonl'
        self.write(path,[{'type':'session_meta','payload':{'id':'root','model_provider':'openai'}},
                         self.codex(dict(input_tokens=100,output_tokens=10))])
        collect_file(self.store,path,'codex')
        row=self.store.usage(period='all')['rows'][0]
        self.assertEqual(row['model'],'unknown')
        self.assertEqual(row['provider'],'OpenAI')
        self.assertEqual(row['output'],10)

    def test_counter_reset_uses_last_request_and_preserves_model_split(self):
        path=self.root/'reset.jsonl'
        meta={'type':'session_meta','payload':{'id':'session'}}
        model=lambda name:{'type':'turn_context','payload':{'model':name}}
        def point(i,inp,out,last_inp,last_out):
            row=self.codex(dict(input_tokens=inp,output_tokens=out),f'2026-09-07T0{i}:00:00Z')
            row['payload']['info']['last_token_usage']=dict(input_tokens=last_inp,output_tokens=last_out)
            return row
        self.write(path,[meta,model('gpt-a'),point(1,100,10,100,10),point(2,150,20,50,10),model('gpt-b'),point(3,5,3,5,3),point(4,10,7,5,4)])
        collect_file(self.store,path,'codex')
        data=self.store.usage(period='all')
        self.assertEqual(data['totals']['uncached_input'],160)
        self.assertEqual(data['totals']['output'],27)
        self.assertEqual({r['model']:r['output'] for r in data['rows']},{'gpt-a':20,'gpt-b':7})

    def test_claude_streaming_maximum_global_request_identity(self):
        path=self.root/'claude.jsonl'
        def row(output):return {'type':'assistant','timestamp':'2026-09-08T10:00:00Z','requestId':'request','sessionId':'phone-or-pc',
                 'message':{'id':'message','model':'claude-test','content':'PRIVATE BODY','usage':{'input_tokens':10,'cache_read_input_tokens':40,'cache_creation_input_tokens':20,'output_tokens':output}}}
        self.write(path,[row(1),row(100),row(4)])
        collect_file(self.store,path,'claude-code')
        copy=self.root/'mobile-copy.jsonl';shutil.copyfile(path,copy);collect_file(self.store,copy,'claude-code')
        self.assertEqual(self.store.usage(period='all')['totals'],dict(uncached_input=10,cached_input=40,output=100,cache_creation=20,reasoning=0,requests=1))
        self.assertNotIn(b'PRIVATE BODY',(self.root/'usage.db').read_bytes())

    def test_kst_boundary_cumulative_zero_buckets_and_routes(self):
        with self.store.connect() as c:
            for key,ts,route in [('a','2026-09-07T14:59:59Z','claude-code'),('b','2026-09-07T15:00:00Z','antigravity'),('c','2026-09-08T14:59:59Z','antigravity'),('d','2026-09-08T15:00:00Z','claude-code')]:
                self.store.event(c,key,stamp(ts),'Anthropic',route,'claude-test',dict(uncached_input=10))
        data=self.store.usage(period='custom',start='2026-09-08',end='2026-09-08',granularity='hour',group='route',cumulative=True)
        self.assertEqual(data['totals']['uncached_input'],20)
        self.assertEqual(data['lifetime']['uncached_input'],40)
        self.assertEqual(len(data['series']),24)
        self.assertEqual(data['series'][-1]['values']['antigravity']['uncached_input'],20)
        self.assertEqual(data['series'][5]['values']['antigravity']['uncached_input'],10)
        self.assertEqual(data['labels'],['antigravity'])
        with self.assertRaises(ValueError):self.store.usage(period='custom',start='2026-10-01',end='2026-09-01')

    def test_limits_original_resets_shared_groups_stale_and_errors(self):
        now=stamp('2026-09-08T10:00:00Z')
        with self.store.connect() as c:
            codex_limits(self.store,c,{'rateLimits':{'primary':{'usedPercent':99}},'rateLimitsByLimitId':{'codex':{'primary':{'usedPercent':25,'windowDurationMins':300,'resetsAt':now+10}},'model-family':{'secondary':{'usedPercent':10,'windowDurationMins':10080}}}},now)
            status_limits(self.store,c,'antigravity',{'quota':{'gemini-weekly':{'remaining_fraction':.7,'reset_time':'2026-09-09T10:00:00Z'},'claude-gpt-weekly':{'remaining_fraction':.2,'reset_in_seconds':100}}},now)
            status_limits(self.store,c,'claude-code',{'rate_limits':{'five_hour':{'used_percentage':42,'resets_at':now+60}}},now)
            go_limits(self.store,c,{'usage':{'rolling':{'percent':37,'resetsAt':now+100},'weekly':{'percent':10,'resetsAt':now+1000},'monthly':{'percent':3,'resetsAt':now+2000}}},now)
        rows=self.store.limits(now+11)['limits']
        agy=[r for r in rows if r['route']=='antigravity'];self.assertEqual(len(agy),2);self.assertNotEqual(agy[0]['remaining'],agy[1]['remaining'])
        codex=[r for r in rows if r['route']=='codex' and r['bucket'].startswith('codex')][0]
        self.assertEqual(codex['remaining'],75);self.assertEqual(codex['status'],'stale');self.assertEqual(codex['seconds_to_reset'],0)
        self.assertIn('+09:00',codex['reset_kst'])
        with self.store.connect() as c:
            self.store.source(c,'codex','error','failed',now+12)
            codex_limits(self.store,c,{'primary':{'usedPercent':1,'windowDurationMins':300}},now-10)
        self.assertEqual(next(r for r in self.store.limits(now+13)['limits'] if r['route']=='codex' and r['bucket'].startswith('codex'))['remaining'],75)
        self.assertEqual(next(r for r in self.store.limits(now+13)['limits'] if r['route']=='codex')['status'],'error')

    def test_uncollected_service_is_reported_without_fake_zero_series(self):
        with self.store.connect() as c:
            self.store.source(c,'antigravity-records','unavailable','binary history')
        data=self.store.usage(group='route')
        self.assertEqual(data['unavailable_routes'],[dict(route='antigravity',detail='binary history')])
        self.assertNotIn('antigravity',data['labels'])
        self.assertEqual(data['totals']['output'],0)

    def test_status_allowlist_event_ingestion(self):
        safe=sanitize('antigravity',{'quota':{'gemini-weekly':{'remaining_fraction':.5,'reset_time':'2026-10-01T00:00:00Z','secret':'PRIVATE'}},'content':'BODY','token':'SECRET'})
        self.assertNotIn('PRIVATE',json.dumps(safe));self.assertNotIn('BODY',json.dumps(safe))
        self.write(self.root/'antigravity.json',[dict(checked=1234,data=safe)])
        collect_status(self.store,{'status_inboxes':[str(self.root)]})
        rows=self.store.limits(1235)['limits'];self.assertEqual(next(r for r in rows if r['route']=='antigravity')['remaining'],50)

    def test_malformed_status_file_does_not_crash_collector(self):
        # A null bucket row is skipped while sibling buckets still ingest.
        data={'rate_limits':{'five_hour':None,'seven_day':{'used_percentage':30}}}
        self.write(self.root/'claude-code.json',[dict(checked=1234,data=data)])
        collect_status(self.store,{'status_inboxes':[str(self.root)]})
        rows=self.store.limits(1235)['limits']
        row=next(r for r in rows if r['route']=='claude-code')
        self.assertEqual((row['bucket'],row['remaining']),('seven_day',70))
        # A non-object payload raises inside status_limits; the collector must
        # record an error instead of letting the exception kill the run loop.
        self.write(self.root/'antigravity.json',[dict(checked=1234,data='broken')])
        collect_status(self.store,{'status_inboxes':[str(self.root)]})
        src=next(s for s in self.store.limits(1235)['sources'] if s['name']=='antigravity')
        self.assertEqual(src['status'],'error')

    def test_older_status_source_cannot_override_new_confirmation(self):
        with self.store.connect() as c:
            self.store.source(c,'antigravity','ok','new',200)
            self.store.source(c,'antigravity','error','old Windows event',100)
            row=c.execute("SELECT * FROM sources WHERE name='antigravity'").fetchone()
            self.assertEqual(row['status'],'ok')
            self.assertEqual(row['checked'],200)

    def test_status_wrapper_preserves_bytes_and_exit(self):
        from llm_usage import statusline
        (self.root/'claude-code-original.json').write_text(json.dumps({'command':f'{sys.executable} -c "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.exit(7)"'}))
        raw=b'{"rate_limits":{"five_hour":{"used_percentage":25}},"content":"PRIVATE BODY"}\n'
        result=subprocess.run([sys.executable,statusline.__file__,'--route','claude-code','--directory',str(self.root)],input=raw,capture_output=True)
        self.assertEqual(result.returncode,7);self.assertEqual(result.stdout,raw)
        self.assertNotIn('PRIVATE BODY',(self.root/'claude-code.json').read_text())

    def test_status_wrapper_appends_a_fresh_opt_in_summary_only(self):
        from llm_usage import statusline
        (self.root/'claude-code-original.json').write_text(json.dumps({'command':f'{sys.executable} -c "print(1)"'}))
        run=lambda:subprocess.run([sys.executable,statusline.__file__,'--route','claude-code','--directory',str(self.root)],input=b'{}',capture_output=True).stdout
        self.assertEqual(run(),b'1\n')
        (self.root/'summary.txt').write_text('Codex 주간 44%\n')
        self.assertEqual(run().decode(),'1\nCodex 주간 44%\n')
        os.utime(self.root/'summary.txt',(time.time()-1000,time.time()-1000))
        self.assertEqual(run(),b'1\n')

    def test_brief_line_lists_current_shared_windows(self):
        from llm_usage.notify import brief_line
        rows=[dict(route='codex',bucket='codex · 10080분',status='fresh',remaining=44.2,display_name='Codex 공통 · 주간'),
              dict(route='codex',bucket='codex_bengalfox · 300분',status='fresh',remaining=10),
              dict(route='claude-code',bucket='five_hour',status='fresh',remaining=60,blocked_by={'bucket':'seven_day'}),
              dict(route='devin',bucket='weekly',status='stale',remaining=5)]
        self.assertEqual(brief_line({'limits':rows}),'Codex 공통 · 주간 44% · Claude 5시간 사용 불가')
        self.assertEqual(brief_line({'limits':rows[3:]}),'한도 최신값 없음')

    def test_status_wrapper_without_original_command_file_stays_silent(self):
        from llm_usage import statusline
        raw=b'{"rate_limits":{"five_hour":{"used_percentage":25}}}'
        for broken in (None,'{not json'):
            if broken is not None:(self.root/'claude-code-original.json').write_text(broken)
            result=subprocess.run([sys.executable,statusline.__file__,'--route','claude-code','--directory',str(self.root)],input=raw,capture_output=True)
            self.assertEqual((result.returncode,result.stdout,result.stderr),(0,b'',b''))
        self.assertIn('five_hour',json.loads((self.root/'claude-code.json').read_text())['data']['rate_limits'])

    def test_session_project_agent_attribution_and_groups(self):
        from llm_usage.store import project_label
        self.assertEqual(project_label('/home/x/projects/app'),'app')
        self.assertEqual(project_label('/mnt/e/projects'),'projects')
        self.assertIsNone(project_label(None))
        path=self.root/'sub.jsonl'
        meta={'type':'session_meta','payload':{'id':'sess-1','cwd':'/home/x/projects/app','thread_source':'subagent','source':{'subagent':{}}}}
        self.write(path,[meta,{'type':'turn_context','payload':{'model':'gpt-test'}},self.codex(dict(input_tokens=100,output_tokens=10))])
        collect_file(self.store,path,'codex')
        with self.store.connect() as c:
            row=c.execute('SELECT session,project,agent_kind FROM events').fetchone()
        self.assertEqual((row['session'],row['project'],row['agent_kind']),('sess-1','app','sub'))
        usage=self.store.usage(period='all')
        self.assertEqual(usage['sessions'][0]['session'],'sess-1')
        self.assertEqual(usage['sessions'][0]['kind'],'sub')
        self.assertEqual(self.store.usage(period='all',group='project')['labels'],['app'])
        self.assertEqual(self.store.usage(period='all',group='agent')['labels'],['sub'])

    def test_existing_db_gains_attribution_columns(self):
        import sqlite3
        old=self.root/'old.db'
        with closing(sqlite3.connect(old)) as c, c:
            c.execute('CREATE TABLE events (id TEXT PRIMARY KEY,ts REAL NOT NULL,provider TEXT NOT NULL,route TEXT NOT NULL,'
                      'model TEXT NOT NULL,uncached_input INTEGER NOT NULL,cached_input INTEGER NOT NULL,'
                      'output INTEGER NOT NULL,cache_creation INTEGER NOT NULL,reasoning INTEGER NOT NULL)')
            c.execute("INSERT INTO events VALUES ('k',1,'P','r','m',1,0,1,0,0)")
        store=Store(old)
        with store.connect() as c:
            cols={r['name'] for r in c.execute('PRAGMA table_info(events)')}
            self.assertTrue({'session','project','agent_kind'}<=cols)
            self.assertIsNone(c.execute('SELECT session FROM events').fetchone()[0])

    def test_compare_overlay_series(self):
        path=self.root/'a.jsonl'
        meta={'type':'session_meta','payload':{'id':'s1'}}
        self.write(path,[meta,{'type':'turn_context','payload':{'model':'m'}},self.codex(dict(input_tokens=100,output_tokens=10))])
        collect_file(self.store,path,'codex')
        base=self.store.usage(period='all')
        cmp=self.store.usage(period='all',compare='week')
        self.assertIsNone(base['compare'])
        self.assertEqual(cmp['compare']['mode'],'week')
        self.assertEqual(len(cmp['compare']['series']),len(cmp['series']))
        prev=self.store.usage(period='7d',compare='previous')['compare']
        self.assertEqual(prev['label'],'직전 동일 기간')

    def test_backfill_jsonl_attributes_existing_events(self):
        from llm_usage.collect import backfill_jsonl
        path=self.root/'old.jsonl'
        meta={'type':'session_meta','payload':{'id':'sess-b','cwd':'/mnt/e/projects/legacy','thread_source':'subagent'}}
        self.write(path,[meta,{'type':'turn_context','payload':{'model':'m'}},self.codex(dict(input_tokens=50,output_tokens=5))])
        collect_file(self.store,path,'codex')
        with self.store.connect() as c:
            c.execute('UPDATE events SET session=NULL,project=NULL,agent_kind=NULL')
        backfill_jsonl(self.store,path,'codex')
        with self.store.connect() as c:
            row=c.execute('SELECT session,project,agent_kind FROM events').fetchone()
        self.assertEqual((row['session'],row['project'],row['agent_kind']),('sess-b','legacy','sub'))
        claude=self.root/'c.jsonl'
        self.write(claude,[{'type':'assistant','sessionId':'cs','cwd':'/home/x/p','isSidechain':False,
                            'message':{'id':'m1','model':'claude-x','usage':{'input_tokens':5,'output_tokens':3}},'timestamp':'2026-09-07T15:00:00Z'}])
        collect_file(self.store,claude,'claude-code')
        with self.store.connect() as c:c.execute("UPDATE events SET session=NULL WHERE route='claude-code'")
        backfill_jsonl(self.store,claude,'claude-code')
        with self.store.connect() as c:
            row=c.execute("SELECT session,project,agent_kind FROM events WHERE route='claude-code'").fetchone()
        self.assertEqual((row['session'],row['project'],row['agent_kind']),('cs','p','main'))

    def test_opencode_maker_backfill_preserves_tokens(self):
        from llm_usage.collect import model_provider, repair_provider_labels
        self.assertEqual(model_provider('muse-spark-1.3-contributor'),'Meta')
        self.assertEqual(model_provider('deepseek-v4-flash'),'DeepSeek')
        path=self.root/'opencode.db'
        data=dict(role='assistant',providerID='opencode-go',modelID='muse-spark-1.3-contributor',tokens=dict(input=10,output=20))
        with closing(sqlite3.connect(path)) as c, c:
            c.execute('CREATE TABLE message(id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT)')
            c.execute('INSERT INTO message VALUES (?,?,?,?)',('request',1788850000000,100,json.dumps(data)))
        collect_opencode(self.store,path)
        before=self.store.usage(period='all')['totals']
        with self.store.connect() as c:
            c.execute("UPDATE events SET provider='Other'")
            self.store.save_state(c,'opencode:'+str(path),200)
        # This old request may have disappeared from the tool DB; only the stored model is needed.
        repair_provider_labels(self.store)
        after=self.store.usage(period='all')
        self.assertEqual(after['totals'],before)
        self.assertEqual(after['rows'][0]['provider'],'Meta')

    def test_opencode_incremental_reasoning_and_copy(self):
        path=self.root/'opencode.db'
        data=dict(role='assistant',providerID='opencode-go',modelID='gemini-test',tokens=dict(input=10,output=20,reasoning=5,cache=dict(read=30,write=4)))
        with closing(sqlite3.connect(path)) as c, c:
            c.execute('CREATE TABLE message(id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT)')
            c.execute('INSERT INTO message VALUES (?,?,?,?)',('request',1788850000000,100,json.dumps(data)))
        collect_opencode(self.store,path);collect_opencode(self.store,path)
        copy=self.root/'copy.db';shutil.copyfile(path,copy);collect_opencode(self.store,copy)
        self.assertEqual(self.store.usage(period='all')['totals'],dict(uncached_input=10,cached_input=30,output=25,reasoning=5,cache_creation=4,requests=1))


    def test_scope_filters_all_sections(self):
        now=stamp('2026-09-08T06:00:00Z')
        with self.store.connect() as c:
            self.store.event(c,'a',stamp('2026-09-07T15:00:00Z'),'Anthropic','claude-code','claude-a',dict(uncached_input=10,output=5),session='s1',project='alpha')
            self.store.event(c,'b',stamp('2026-09-07T15:10:00Z'),'OpenAI','codex','gpt-b',dict(uncached_input=20,output=10),session='s2',project='beta')
            self.store.event(c,'c',stamp('2026-09-07T15:20:00Z'),'Anthropic','claude-code','claude-c',dict(uncached_input=30,output=15),session='s3')
        full=self.store.usage(period='all',now=now)
        self.assertEqual(full['totals']['uncached_input'],60)
        self.assertIsNone(full['scope'])
        self.assertIn('claude-code',full['scope_options']['route'])
        self.assertIn('미분류',full['scope_options']['project'])

        scoped=self.store.usage(period='all',now=now,scope='route:claude-code',group='route')
        self.assertEqual(scoped['scope'],'route:claude-code')
        self.assertEqual(scoped['totals']['uncached_input'],40)
        self.assertEqual(scoped['totals']['requests'],2)
        self.assertEqual(scoped['lifetime']['uncached_input'],40)
        self.assertEqual({r['model'] for r in scoped['rows']},{'claude-a','claude-c'})
        self.assertEqual(set(scoped['labels']),{'claude-code'})
        self.assertEqual({s['session'] for s in scoped['sessions']},{'s1','s3'})
        self.assertEqual(scoped['sessionless_requests'],0)
        self.assertEqual({p['project'] for p in scoped['projects']},{'alpha','미분류'})
        self.assertEqual(sum(day['tokens'] for day in scoped['calendar']),60)
        self.assertEqual(sum(h['tokens'] for h in scoped['heatmap']),60)

        by_model=self.store.usage(period='all',now=now,scope='model:gpt-b')
        self.assertEqual(by_model['totals']['uncached_input'],20)
        self.assertEqual({s['session'] for s in by_model['sessions']},{'s2'})
        by_project=self.store.usage(period='all',now=now,scope='project:미분류')
        self.assertEqual(by_project['totals']['uncached_input'],30)
        by_provider=self.store.usage(period='all',now=now,scope='provider:Anthropic')
        self.assertEqual(by_provider['totals']['uncached_input'],40)
        cmp=self.store.usage(period='all',now=now,scope='route:claude-code',compare='week')
        self.assertEqual(len(cmp['compare']['series']),len(cmp['series']))

        # Each scope value selects a strict subset; union over routes restores the total.
        routes=self.store.usage(period='all',now=now)['scope_options']['route']
        parts=[self.store.usage(period='all',now=now,scope='route:'+r)['totals']['uncached_input'] for r in routes]
        self.assertEqual(sum(parts),full['totals']['uncached_input'])
        for bad in ('agent:x','route','route:',':x'):
            with self.assertRaises(ValueError):self.store.usage(period='all',scope=bad)
        # A hostile value is bound as a literal parameter, not interpolated into SQL.
        hostile=self.store.usage(period='all',scope="route:x' OR '1'='1")
        self.assertEqual(hostile['totals']['requests'],0)

    def test_scope_isolates_period_free_memo(self):
        now=stamp('2026-09-08T06:00:00Z')
        with self.store.connect() as c:
            self.store.event(c,'a',stamp('2026-09-07T15:00:00Z'),'Anthropic','claude-code','m',dict(uncached_input=10))
            self.store.event(c,'b',stamp('2026-09-07T15:10:00Z'),'OpenAI','codex','m',dict(uncached_input=20))
        full=self.store.usage(period='all',now=now)
        scoped=self.store.usage(period='all',now=now,scope='route:claude-code')
        self.assertEqual(sum(d['tokens'] for d in scoped['calendar']),10)
        # The unscoped memo must not be reused under a scope.
        self.assertEqual(sum(d['tokens'] for d in self.store.usage(period='all',now=now)['calendar']),30)
        self.assertEqual(full['lifetime']['uncached_input'],30)


    def test_sum_preserving_relabel_recomputes_period_free_costs(self):
        # A relabel moves no token sums, so only data_version can expire the memo.
        now=stamp('2026-09-08T06:00:00Z')
        with self.store.connect() as c:
            self.store.event(c,'a',stamp('2026-09-07T15:00:00Z'),'OpenAI','codex','m1',dict(uncached_input=1_000_000))
        pricing={'m1':{'input':1,'output':0},'m2':{'input':10,'output':0}}
        before=self.store.usage(period='all',now=now,pricing=pricing)
        self.assertAlmostEqual(before['cost']['lifetime'],1.0)
        with self.store.connect() as c:
            c.execute("UPDATE events SET model='m2'")
            self.store.bump_data_version(c)
        after=self.store.usage(period='all',now=now,pricing=pricing)
        self.assertAlmostEqual(after['cost']['lifetime'],10.0)
        self.assertAlmostEqual(after['insights']['month']['total'],10.0)
        self.assertAlmostEqual(sum(d['cost'] or 0 for d in after['calendar']),10.0)


    def test_heatmap_and_calendar_carry_metric_fields(self):
        now=stamp('2026-09-08T06:00:00Z')
        with self.store.connect() as c:
            self.store.event(c,'a',stamp('2026-09-07T15:00:00Z'),'Anthropic','claude-code','claude-a',dict(uncached_input=10,output=5))
            self.store.event(c,'b',stamp('2026-09-07T15:10:00Z'),'OpenAI','codex','gpt-b',dict(uncached_input=20,output=10))
        pricing={'claude-a':{'input':1,'output':2}}
        data=self.store.usage(period='all',now=now,pricing=pricing)
        total_tokens=data['totals']['uncached_input']+data['totals']['cached_input']+data['totals']['output']+data['totals']['cache_creation']
        self.assertEqual(sum(h['tokens'] for h in data['heatmap']),total_tokens)
        self.assertEqual(sum(h['requests'] for h in data['heatmap']),data['totals']['requests'])
        self.assertEqual(sum(d['tokens'] for d in data['calendar']),total_tokens)
        self.assertEqual(sum(d['requests'] for d in data['calendar']),data['totals']['requests'])
        # Only claude-a is priced: 10*$1/M input + 5*$2/M output.
        self.assertAlmostEqual(sum(h['cost'] or 0 for h in data['heatmap']),0.00002,places=4)
        self.assertAlmostEqual(sum(d['cost'] or 0 for d in data['calendar']),0.00002,places=4)
        unpriced=self.store.usage(period='all',now=now)
        self.assertTrue(all(h['cost'] is None for h in unpriced['heatmap']))
        self.assertTrue(all(d['cost'] is None for d in unpriced['calendar']))


    def test_month_value_projection_scope_and_alert(self):
        # 2026-09-09 12:00 KST: day 9 of a 30-day month.
        now=stamp('2026-09-09T03:00:00Z')
        with self.store.connect() as c:
            self.store.event(c,'a',stamp('2026-09-01T00:00:00Z'),'OpenAI','codex','m1',dict(uncached_input=1_000_000))
            self.store.event(c,'b',stamp('2026-09-05T00:00:00Z'),'Anthropic','claude-code','m2',dict(uncached_input=2_000_000))
            self.store.event(c,'prev',stamp('2026-08-20T00:00:00Z'),'OpenAI','codex','m1',dict(uncached_input=9_000_000))
        pricing={'m1':{'input':10,'output':0},'m2':{'input':5,'output':0}}
        data=self.store.usage(period='all',now=now,pricing=pricing,value_alert_usd=50)
        month=data['insights']['month']
        self.assertEqual(month['days_elapsed'],9)
        self.assertEqual(month['days_in_month'],30)
        # September only: codex 1M*$10 = $10, claude 2M*$5 = $10; the August event is excluded.
        self.assertEqual(month['cost_by_route'],{'claude-code':10.0,'codex':10.0})
        self.assertAlmostEqual(month['total'],20.0)
        self.assertAlmostEqual(month['projected'],20/9*30,places=4)
        self.assertEqual(month['alert_usd'],50)
        scoped=self.store.usage(period='all',now=now,pricing=pricing,scope='route:codex')['insights']['month']
        self.assertAlmostEqual(scoped['total'],10.0)
        self.assertIsNone(self.store.usage(period='all',now=now)['insights']['month'])


class SessionTableTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'usage.db')
        with self.store.connect() as c:
            for i in range(105):
                self.store.event(c,f'e{i}',1757268000+i,'OpenAI','codex','gpt-x',
                    dict(uncached_input=1,output=1),session=f'session-{i:03d}',project='p',agent_kind='main')

    def test_sessions_capped_with_total(self):
        data=self.store.usage(period='all')
        self.assertLessEqual(len(data['sessions']),100)
        self.assertEqual(data['sessions_total'],105)
        self.assertEqual(len(data['sessions']),100)
        core=self.store.usage(period='all',sections='core')
        self.assertNotIn('sessions',core);self.assertNotIn('sessions_total',core)
        insights=self.store.usage(period='all',sections='insights')
        self.assertEqual(insights['sessions_total'],105)


class InsightsSectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'usage.db')
        self.now=stamp('2026-09-08T06:00:00Z')
        with self.store.connect() as c:
            self.store.event(c,'a',stamp('2026-09-07T15:00:00Z'),'Anthropic','claude-code','claude-a',
                dict(uncached_input=10,output=5),session='s1',project='alpha')
            self.store.event(c,'b',stamp('2026-09-07T16:00:00Z'),'OpenAI','codex','gpt-b',
                dict(uncached_input=20,output=10),session='s2')
        self.kwargs=dict(period='all',now=self.now,compare='week',
                         pricing={'claude-a':{'input':1,'output':2},'gpt-b':{'input':3,'output':4}})

    def test_insights_sections_matches_filtered_all(self):
        full=self.store.usage(**self.kwargs)
        ins=self.store.usage(sections='insights',**self.kwargs)
        insight_keys={'insights','sessions','sessions_total','sessionless_requests','projects','calendar'}
        shared={'start','end_exclusive','timezone','granularity','labels','series',
                'subscriptions','sources','scope','collected_at'}
        # The insights response is exactly the kept subset of the full response.
        self.assertEqual(ins,{k:v for k,v in full.items() if k in insight_keys|shared})
        self.assertTrue(full['heatmap'])
        self.assertIsNotNone(full['compare'])
        self.assertIn('claude-code',full['scope_options']['route'])

    def test_insights_sections_skips_chart_aggregates(self):
        captured={}
        original=self.store._usage_body
        def spy(*args,**kw):
            body=original(*args,**kw);captured.update(body);return body
        self.store._usage_body=spy
        try:self.store.usage(sections='insights',**self.kwargs)
        finally:self.store._usage_body=original
        # The skipped scans stay empty in the body rather than doing wasted work.
        self.assertEqual(captured['heatmap'],[])
        self.assertIsNone(captured['compare'])
        self.assertEqual(captured['scope_options'],{})


class UsageCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'usage.db')
        with self.store.connect() as c:
            self.store.event(c,'e1',1757268000,'Anthropic','claude-code','claude-x',
                dict(uncached_input=10,cached_input=5,output=3,cache_creation=0,reasoning=0))

    def calls(self):
        calls=[]
        original=self.store._usage_body
        def spy(*args,**kw):calls.append(1);return original(*args,**kw)
        return spy,calls

    def test_repeat_query_hits_cache(self):
        spy,calls=self.calls();self.store._usage_body=spy
        first=self.store.usage(period='7d')
        second=self.store.usage(period='7d')
        self.assertEqual(first,second)
        self.assertEqual(len(calls),1)
        # A different shape (scope/sections/grouping) is a different entry.
        self.store.usage(period='7d',sections='core')
        self.store.usage(period='7d',scope='route:claude-code')
        self.assertEqual(len(calls),3)
        # Live fields are not frozen into the cached body.
        with self.store.connect() as c:self.store.save_state(c,'collector',{'checked':1757268000,'status':'ok'})
        sources={s['name']:s for s in self.store.usage(period='7d')['sources']}
        self.assertEqual(sources['수집기']['status'],'stale')
        self.assertEqual(len(calls),3)

    def test_writes_invalidate(self):
        spy,calls=self.calls();self.store._usage_body=spy
        self.store.usage(period='7d')
        with self.store.connect() as c:
            self.store.event(c,'e2',1757268100,'Anthropic','claude-code','claude-x',
                dict(uncached_input=1,cached_input=0,output=1,cache_creation=0,reasoning=0))
        self.store.usage(period='7d');self.assertEqual(len(calls),2)
        with self.store.connect() as c:self.store.bump_data_version(c)
        self.store.usage(period='7d');self.assertEqual(len(calls),3)
        with self.store.connect() as c:self.store.save_state(c,'last_local',1757269000)
        self.store.usage(period='7d');self.assertEqual(len(calls),4)

    def test_lru_evicts_oldest(self):
        for i in range(15):self.store.usage(period='7d',scope=f'model:m{i}')
        self.assertLessEqual(len(self.store._usage_cache),12)


class BackupTests(unittest.TestCase):
    def test_backup_roundtrip_and_sidecars(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);db=root/'usage.db'
            store=Store(str(db))
            with store.connect() as c:
                store.event(c,'k1',time.time(),'OpenAI','codex','gpt-6-sol',dict(uncached_input=1,output=2))
            (root/'pricing.json').write_text('{"m":{"input":1,"output":2}}')
            (root/'local.json').write_text('{"refresh_seconds":120}')
            result=backup_now(str(db))
            self.assertTrue(result['ok'],result)
            copy=root/'backups'/result['file']
            self.assertTrue(copy.exists())
            import gzip,sqlite3
            plain=root/'restored.db'
            plain.write_bytes(gzip.decompress(copy.read_bytes()))
            with closing(sqlite3.connect(plain)) as c, c:
                self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0],'ok')
                self.assertEqual(c.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)
            self.assertTrue((root/'backups'/('pricing.json-'+time.strftime('%Y%m%d'))).exists())

    def test_failed_compression_leaves_no_partial_gz(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);db=root/'usage.db'
            store=Store(str(db))
            with store.connect() as c:
                store.event(c,'k1',time.time(),'OpenAI','codex','gpt-6-sol',dict(uncached_input=1))
            with patch('llm_usage.backup.gzip.open',side_effect=OSError('disk full')):
                result=backup_now(str(db))
            self.assertFalse(result['ok'])
            self.assertEqual(list((root/'backups').glob('*gz*')),[])
            self.assertEqual(list((root/'backups').glob('*.tmp')),[])

    def test_prune_keeps_recent_and_mondays(self):
        import gzip
        from llm_usage.backup import _prune
        with tempfile.TemporaryDirectory() as td:
            backups=Path(td)
            for age in (1,10,40,80):
                day=time.strftime('%Y%m%d',time.localtime(time.time()-age*86400))
                (backups/f'usage-{day}.db.gz').write_bytes(b'x')
            _prune(backups)
            kept=[p.name for p in backups.glob('usage-*.db.gz')]
            self.assertTrue(any(n.endswith(time.strftime('%Y%m%d',time.localtime(time.time()-86400))+'.db.gz') for n in kept))
            # 10일 전만 주간 보존 구간(28일) 안 — 월요일이면 유지.
            self.assertEqual(len(kept),1+(time.localtime(time.time()-10*86400).tm_wday==0))


class WebTests(unittest.TestCase):
    def setUp(self):
        UsageTests.setUp(self)
        self.origin='https://pc.example.ts.net:9444'
        self.app=create_app(dict(database=str(self.root/'usage.db'),origin=self.origin,secret_key='test-secret',allowed_logins=['owner']))
        self.client=self.app.test_client()
        self.env={'REMOTE_ADDR':'','gunicorn.socket':SimpleNamespace(family=socket.AF_UNIX)}
        self.headers={'Tailscale-User-Login':'owner'}

    def get(self,path,**kwargs):return self.client.get(path,base_url=self.origin,headers=kwargs.get('headers',self.headers),environ_overrides=kwargs.get('env',self.env))

    def test_manifest_is_installable(self):
        with self.get('/manifest.json') as r:
            self.assertEqual(r.status_code,200)
            data=r.get_json()
        self.assertEqual(data['display'],'standalone')
        self.assertTrue(data['start_url'].startswith('/'))
        self.assertTrue(any(i['sizes']=='192x192' for i in data['icons']))
        self.assertTrue(any(i['sizes']=='512x512' for i in data['icons']))
        for asset in ('/assets/icon-192.png','/assets/icon-512.png'):
            with self.get(asset) as r:self.assertEqual(r.status_code,200)
        with self.get('/') as r:self.assertIn(b'rel="manifest"',r.data)

    def test_new_sol_api_cost_uses_its_own_cache_rate(self):
        self.app=create_app(dict(database=str(self.root/'usage.db'),origin=self.origin,
            secret_key='test-secret',allowed_logins=['owner'],pricing_file=str(self.root/'pricing.json')))
        self.client=self.app.test_client()
        with self.store.connect() as c:
            for model in ('gpt-6.1-sol','gpt-6.1-sol-2026-09-29'):
                self.store.event(c,model,stamp('2026-10-03T00:00:00Z'),'OpenAI','codex',model,
                    dict(uncached_input=1_000_000,cached_input=1_000_000,output=1_000_000,
                         cache_creation=1_000_000,reasoning=500_000))
            self.store.event(c,'old-sol',stamp('2026-10-03T00:00:00Z'),'OpenAI','codex','gpt-6-sol',dict(cached_input=1_000_000))
            self.store.event(c,'future-sol',stamp('2026-10-03T00:00:00Z'),'OpenAI','codex','gpt-6.2-sol',dict(uncached_input=1_000_000))
        response=self.get('/api/usage?period=custom&start=2026-10-03&end=2026-10-03&group=model')
        self.assertEqual(response.status_code,200)
        data=response.get_json();rows={r['model']:r for r in data['rows']}
        # $2 input + $0.10 cached + $10 output + $2.50 writes; reasoning is included in output.
        self.assertEqual(rows['gpt-6.1-sol']['est_cost'],14.6)
        self.assertEqual(rows['gpt-6.1-sol-2026-09-29']['est_cost'],14.6)
        self.assertEqual(rows['gpt-6-sol']['est_cost'],0.2)
        self.assertIsNone(rows['gpt-6.2-sol']['est_cost'])
        self.assertAlmostEqual(data['cost']['period'],29.4)
        self.assertEqual(data['cost']['coverage'],90)
        config=self.get('/api/config').get_json()
        self.assertIn('gpt-6.1-sol',config['pricing_builtin'])
        self.assertEqual(config['pricing_origins']['gpt-6.1-sol'],'builtin')

    def test_auth_csrf_and_coalesced_refresh(self):
        self.assertEqual(self.get('/api/usage',headers={}).status_code,403)
        self.assertEqual(self.get('/api/usage',headers={'Tailscale-User-Login':'other'}).status_code,403)
        self.assertEqual(self.get('/api/usage',env={'REMOTE_ADDR':'127.0.0.1','gunicorn.socket':SimpleNamespace(family=socket.AF_INET)}).status_code,403)
        self.assertEqual(self.get('/api/bootstrap').json['refresh_seconds'],300)
        token=self.get('/api/bootstrap').json['csrf']
        headers={**self.headers,'Origin':self.origin,'X-CSRF-Token':token}
        def post(h):return self.client.post('/api/refresh',json={},base_url=self.origin,headers=h,environ_overrides=self.env)
        self.assertEqual(post(self.headers).status_code,403)
        r=post(headers);self.assertEqual(r.status_code,202)
        self.assertEqual(post(headers).json['requested'],r.json['requested'])
        self.assertEqual(post(headers).json['request_id'],r.json['request_id'])
        self.assertEqual(self.get('/api/collection').json['completed_id'],0)
        with self.store.connect() as c:
            self.store.save_state(c,'refresh_completed_id',r.json['request_id'])
            self.store.save_state(c,'refresh_requested',0)
        self.assertEqual(post(headers).json['request_id'],r.json['request_id']+1)
        self.assertEqual(self.get('/api/usage').status_code,200)
        self.assertIn('quality',self.get('/api/usage').json['insights'])
        for grain in ('week','month'):
            result=self.get('/api/usage?granularity='+grain)
            self.assertEqual(result.status_code,200);self.assertEqual(result.json['granularity'],grain)
        self.assertEqual(self.get('/api/usage?period=custom&start=9999-12-01&end=9999-12-31&granularity=month').status_code,400)
        self.assertEqual(self.get('/api/usage?period=custom&start=no').status_code,400)
        self.assertEqual(self.get('/api/usage?group=sql').status_code,400)
        self.assertEqual(self.get('/api/usage?scope=agent:x').status_code,400)
        self.assertEqual(self.get('/api/usage?scope=route').status_code,400)
        self.assertEqual(self.get('/api/usage?scope=route:claude-code').status_code,200)
        self.assertIn('scope_options',self.get('/api/usage').json)
        core=self.get('/api/usage?sections=core').json
        self.assertIn('totals',core)
        for key in ('insights','sessions','sessionless_requests','projects','calendar'):
            self.assertNotIn(key,core)
        ins=self.get('/api/usage?sections=insights').json
        for key in ('insights','sessions','sessionless_requests','projects','calendar'):
            self.assertIn(key,ins)
        self.assertNotIn('totals',ins)
        self.assertEqual(set(core)|set(ins),set(self.get('/api/usage').json))
        self.assertEqual(self.get('/api/usage?sections=bogus').status_code,400)
        self.assertNotIn('test-secret',self.get('/api/limits').text)
        self.assertIn('llm_usage_session',self.get('/api/bootstrap').headers.get('Set-Cookie','') or str(self.client._cookies))

    def test_config_endpoints(self):
        token=self.get('/api/bootstrap').json['csrf']
        headers={**self.headers,'Origin':self.origin,'X-CSRF-Token':token}
        def post(path,body,h=headers):return self.client.post(path,json=body,base_url=self.origin,headers=h,environ_overrides=self.env)
        self.assertEqual(self.get('/api/config').status_code,200)
        self.assertEqual(post('/api/config/subscription-prices',{}).status_code,400)
        self.assertEqual(post('/api/config/subscription-prices',{'prices':{'codex':-1}}).status_code,400)
        self.assertEqual(post('/api/config/subscription-prices',{'prices':{'Bad Route':10}}).status_code,400)
        r=post('/api/config/subscription-prices',{'prices':{'codex':20,'antigravity':0}})
        self.assertEqual(r.status_code,200)
        self.assertEqual(self.get('/api/usage').json['subscriptions'][0]['route'],'antigravity')
        self.assertEqual(post('/api/config/pricing',{'model':'x/y?'}).status_code,400)
        r=post('/api/config/pricing',{'model':'swe-2 (max)','input':0.6,'output':2.5})
        self.assertEqual(r.status_code,200)
        self.assertEqual(post('/api/config/pricing',{'model':'swe-2 (max)','delete':True}).status_code,200)
        self.assertEqual(post('/api/config/pricing',{'model':'test-model','input':-1}).status_code,400)
        self.assertEqual(post('/api/config/pricing',{'model':'test-model'}).status_code,400)
        r=post('/api/config/pricing',{'model':'test-model','input':1.5,'output':7})
        self.assertEqual(r.status_code,200)
        cfg=self.get('/api/config').json
        self.assertEqual(cfg['pricing']['test-model']['input'],1.5)
        self.assertEqual(post('/api/config/pricing',{'model':'test-model','delete':True}).status_code,200)
        self.assertNotIn('test-model',self.get('/api/config').json['pricing'])
        self.assertEqual(post('/api/config/thresholds',{'stale_seconds':30}).status_code,400)
        r=post('/api/config/thresholds',{'stale_seconds':900,'low_percent':20})
        self.assertEqual(r.status_code,200)
        self.assertEqual(self.get('/api/config').json['thresholds']['stale_seconds'],900)
        self.assertEqual(self.get('/api/limits').json['low_percent'],20)
        self.assertEqual(post('/api/config/thresholds',{'quota_hide_days':0}).status_code,400)
        self.assertEqual(post('/api/config/thresholds',{'quota_hide_days':14}).status_code,200)
        self.assertEqual(self.get('/api/limits').json['quota_hide_days'],14)
        self.assertEqual(post('/api/config/thresholds',{'stale_seconds':600,'low_percent':15,'quota_hide_days':7}).status_code,200)
        self.assertEqual(post('/api/config/refresh',{'refresh_seconds':10}).status_code,400)
        self.assertEqual(post('/api/config/refresh',{'refresh_seconds':120}).status_code,200)
        self.assertEqual(self.get('/api/config').json['refresh_seconds'],120)
        self.assertEqual(post('/api/config/value-alert',{'value_alert_usd':-1}).status_code,400)
        self.assertEqual(post('/api/config/value-alert',{'value_alert_usd':75}).status_code,200)
        self.assertEqual(self.get('/api/config').json['value_alert_usd'],75)
        self.assertEqual(post('/api/config/value-alert',{'value_alert_usd':None}).status_code,200)
        self.assertIsNone(self.get('/api/config').json['value_alert_usd'])

    def test_config_persists_to_writable_data_dir(self):
        # ProtectHome=read-only sandbox: writes must land next to the database,
        # falling back to the config dir only for reads.
        config_dir=self.root/'conf';config_dir.mkdir()
        legacy=config_dir/'pricing.json';legacy.write_text(json.dumps({'legacy-model':{'input':1,'output':2},'gone':{'input':1,'output':1}}))
        app=create_app(dict(database=str(self.root/'data'/'usage.db'),origin=self.origin,secret_key='s',allowed_logins=['owner'],config_file=str(config_dir/'config.json')))
        client=app.test_client()
        get=lambda p:client.get(p,base_url=self.origin,headers=self.headers,environ_overrides=self.env)
        token=get('/api/bootstrap').json['csrf']
        h={**self.headers,'Origin':self.origin,'X-CSRF-Token':token}
        post=lambda p,b:client.post(p,json=b,base_url=self.origin,headers=h,environ_overrides=self.env)
        self.assertEqual(get('/api/config').json['pricing']['legacy-model']['input'],1)
        self.assertEqual(post('/api/config/pricing',{'model':'swe-2 (max)','input':0.6,'output':2.5}).status_code,200)
        self.assertEqual(post('/api/config/pricing',{'model':'gone','delete':True}).status_code,200)
        self.assertEqual(post('/api/config/subscription-prices',{'prices':{'codex':20}}).status_code,200)
        written=json.loads((self.root/'data'/'pricing.json').read_text())
        self.assertEqual(written['swe-2 (max)']['output'],2.5)
        self.assertIsNone(written['gone'])
        cfg=get('/api/config').json
        self.assertEqual(cfg['pricing']['swe-2 (max)']['input'],0.6)
        self.assertEqual(cfg['pricing']['legacy-model']['input'],1)
        self.assertNotIn('gone',cfg['pricing'])
        local=json.loads((self.root/'data'/'local.json').read_text())
        self.assertEqual(local['subscription_prices'],{'codex':20})
        with patch.dict(os.environ,{'LLM_USAGE_CONFIG':str(config_dir/'config.json')}):
            (config_dir/'config.json').write_text(json.dumps({'database':str(self.root/'data'/'usage.db'),'subscription_prices':{'codex':5}}))
            self.assertEqual(settings()['subscription_prices'],{'codex':20})


    def pricing_client(self,saved):
        config_dir=self.root/'conf';config_dir.mkdir()
        data=self.root/'data';data.mkdir()
        (data/'pricing.json').write_text(json.dumps(saved))
        app=create_app(dict(database=str(data/'usage.db'),origin=self.origin,secret_key='s',allowed_logins=['owner'],
                            config_file=str(config_dir/'config.json')))
        client=app.test_client()
        token=client.get('/api/bootstrap',base_url=self.origin,headers=self.headers,environ_overrides=self.env).json['csrf']
        headers={**self.headers,'Origin':self.origin,'X-CSRF-Token':token}
        post=lambda body:client.post('/api/config/pricing',json=body,base_url=self.origin,headers=headers,environ_overrides=self.env)
        return post,data/'pricing.json'

    def test_pricing_edits_after_delete_or_on_dated_series_are_saved(self):
        post,written=self.pricing_client({'gone':None,'dated':[{'since':'2026-01-01','input':1,'output':2}]})
        self.assertEqual(post({'model':'gone','input':3,'output':4}).status_code,200)
        self.assertEqual(post({'model':'dated','input':1.5,'output':2}).status_code,200)
        # A built-in dated series keeps its history instead of being flattened.
        self.assertEqual(post({'model':'gpt-5.6-sol','input':4,'cached':0.4,'output':20,'cache_write':5}).status_code,200)
        saved=json.loads(written.read_text())
        self.assertEqual(saved['gone'],{'input':3,'output':4})
        self.assertEqual(saved['dated'][0],{'since':'2026-01-01','input':1,'output':2})
        self.assertEqual(saved['dated'][-1]['input'],1.5)
        self.assertEqual([e['since'] for e in saved['gpt-5.6-sol']][:2],['2026-06-26','2026-08-22'])

    def test_pricing_edit_on_inherited_name_merges_with_parent_entry(self):
        post,written=self.pricing_client({'parent-x':{'input':1,'cached':0.1,'output':5,'cache_write':1.25}})
        # 'gpt-5.6-sol (high)' resolves to the dated builtin; the edit must copy
        # that history under the new key and take effect from today only.
        self.assertEqual(post({'model':'gpt-5.6-sol (high)','input':4,'cached':0.4,'output':20,'cache_write':5.5}).status_code,200)
        # A flat parent entry contributes fields the partial edit did not send.
        self.assertEqual(post({'model':'parent-x (high)','output':9}).status_code,200)
        saved=json.loads(written.read_text())
        inherited=saved['gpt-5.6-sol (high)']
        self.assertEqual([e['since'] for e in inherited][:2],['2026-06-26','2026-08-22'])
        self.assertEqual(inherited[0]['output'],30)
        self.assertEqual(inherited[-1]['output'],20)
        self.assertGreater(inherited[-1]['since'],'2026-08-22')
        self.assertEqual(rate_for('gpt-5.6-sol (high)',saved,'2026-07-01')['output'],30)
        self.assertEqual(saved['parent-x (high)'],{'input':1,'cached':0.1,'output':9,'cache_write':1.25})

    def test_pricing_write_holds_lock(self):
        import llm_usage.webapp as webapp
        post,_=self.pricing_client({})
        held=[]
        real=webapp.atomic_json
        def spy(path,data):
            held.append(webapp.pricing_lock.locked())
            return real(path,data)
        with patch('llm_usage.webapp.atomic_json',side_effect=spy):
            self.assertEqual(post({'model':'m','input':1,'output':2}).status_code,200)
        self.assertEqual(held,[True])

    def test_pricing_delete_restores_builtin_and_hide_suppresses(self):
        config_dir=self.root/'conf';config_dir.mkdir()
        data=self.root/'data';data.mkdir()
        (config_dir/'pricing.json').write_text(json.dumps({'gpt-5':{'input':9,'output':9},'legacy-model':{'input':1,'output':2}}))
        app=create_app(dict(database=str(data/'usage.db'),origin=self.origin,secret_key='s',allowed_logins=['owner'],
                            config_file=str(config_dir/'config.json')))
        client=app.test_client()
        get=lambda p:client.get(p,base_url=self.origin,headers=self.headers,environ_overrides=self.env)
        token=get('/api/bootstrap').json['csrf']
        h={**self.headers,'Origin':self.origin,'X-CSRF-Token':token}
        post=lambda b:client.post('/api/config/pricing',json=b,base_url=self.origin,headers=h,environ_overrides=self.env)
        # A read-only config-dir override wins over builtin and shows as 'user'.
        cfg=get('/api/config').json
        self.assertEqual(cfg['pricing']['gpt-5']['input'],9)
        self.assertEqual(cfg['pricing_origins']['gpt-5'],'user')
        self.assertIn('gpt-5',cfg['pricing_builtin'])
        self.assertRegex(cfg['today'],r'^\d{4}-\d{2}-\d{2}$')
        # Deleting a user value restores the builtin rate instead of unpricing.
        self.assertEqual(post({'model':'gpt-5','delete':True}).status_code,200)
        self.assertEqual(json.loads((data/'pricing.json').read_text())['gpt-5'],'builtin')
        cfg=get('/api/config').json
        self.assertEqual(cfg['pricing']['gpt-5']['input'],1.25)
        self.assertEqual(cfg['pricing_origins']['gpt-5'],'builtin')
        # Hiding suppresses builtin and lower layers alike; unhiding restores.
        self.assertEqual(post({'model':'gpt-5','hide':True}).status_code,200)
        self.assertIsNone(json.loads((data/'pricing.json').read_text())['gpt-5'])
        cfg=get('/api/config').json
        self.assertNotIn('gpt-5',cfg['pricing'])
        self.assertEqual(cfg['pricing_origins']['gpt-5'],'hidden')
        self.assertEqual(post({'model':'gpt-5','delete':True}).status_code,200)
        self.assertEqual(get('/api/config').json['pricing']['gpt-5']['input'],1.25)
        # A non-builtin lower-layer model becomes unpriced, not deleted-by-pop.
        self.assertEqual(post({'model':'legacy-model','delete':True}).status_code,200)
        cfg=get('/api/config').json
        self.assertNotIn('legacy-model',cfg['pricing'])
        self.assertEqual(cfg['pricing_origins']['legacy-model'],'hidden')
        # A dated builtin keeps its history after a user edit and restores it.
        self.assertEqual(post({'model':'gpt-5.6-sol','input':4,'cached':0.4,'output':20,'cache_write':5}).status_code,200)
        saved=json.loads((data/'pricing.json').read_text())
        self.assertEqual([e['since'] for e in saved['gpt-5.6-sol']][:2],['2026-06-26','2026-08-22'])
        self.assertEqual(post({'model':'gpt-5.6-sol','delete':True}).status_code,200)
        cfg=get('/api/config').json
        self.assertIsInstance(cfg['pricing']['gpt-5.6-sol'],list)
        self.assertEqual(cfg['pricing_origins']['gpt-5.6-sol'],'builtin')

if __name__=='__main__':unittest.main()

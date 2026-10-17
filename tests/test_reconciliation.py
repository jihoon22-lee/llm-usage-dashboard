import json
from pathlib import Path
import tempfile
import unittest

from llm_usage.store import Store,stamp,identity
from llm_usage.reconciliation import snapshot,link,total
from llm_usage.cloud import _ingest,IncompletePages
from llm_usage.collect import model_provider,parse_line
from llm_usage.telemetry import ingest,resolve


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'usage.db')
        self.now=stamp('2026-10-12T12:00:00Z')

    def event(self,key='a',output=20):
        with self.store.connect() as c:
            self.store.event(c,key,stamp('2026-10-12T09:30:00Z'),'Anthropic','claude-code','claude-test',dict(output=output),session='s',project='p')
            link(c,'claude:s',key,'local')

    def evidence(self,tokens=100,start='2026-10-12T09:00:00Z',end='2026-10-12T10:00:00Z'):
        with self.store.connect() as c:
            snapshot(self.store,c,source='claude-cloud',scope_id='claude:s',route='claude-code',provider='Anthropic',
                     model='claude-test',session='s',project='p',start=stamp(start),end=stamp(end),tokens=tokens,
                     parts=dict(uncached_input=0,cached_input=0,cache_creation=0,output=tokens))

    def usage(self,**kw):return self.store.usage(now=self.now,**kw)

    def test_late_records_shrink_supplement_and_do_not_mutate_cached_body(self):
        self.event();self.evidence()
        a=self.usage(period='today');b=self.usage(period='today')
        self.assertEqual(a,b)
        self.assertEqual(total(a['totals']),100)
        self.assertEqual(a['reconciliation']['supplemental_tokens'],80)
        self.event('b',40)
        u=self.usage(period='today')
        self.assertEqual(total(u['totals']),100)
        self.assertEqual(u['reconciliation']['supplemental_tokens'],40)
        self.assertEqual(u['totals']['requests'],2)
        self.assertTrue(self.store.rollup_matches())

    def test_supplemented_subscription_usage_is_not_no_usage(self):
        # Codex used only through its account total (no local records) was still used;
        # an account day crossing the selected period leaves 'no usage' unknown.
        with self.store.connect() as c:self.store.account_tokens(c,'codex',[dict(startDate='2026-10-11',tokens=1000)],self.now)
        subs=lambda **kw:{s['route']:s for s in self.usage(subscriptions={'codex':20,'devin':10},**kw)['subscriptions']}
        inside=subs(period='custom',start='2026-10-11',end='2026-10-12')
        self.assertEqual((inside['codex']['period_tokens'],inside['devin']['period_tokens']),(1000,0))
        self.assertIsNone(subs(period='today')['codex']['period_tokens'])
        # Claude cloud supplements count toward the Claude subscription too.
        self.event();self.evidence()
        self.assertEqual(self.usage(period='today',subscriptions={'claude-code':20})['subscriptions'][0]['period_tokens'],100)

    def test_codex_account_residual_is_one_item_apart_from_unknown_and_unassigned(self):
        # The Codex account total beyond this PC's records has no model or project. Under
        # model and project grouping it is one residual item rather than 'unknown' or
        # '미분류'; totals, series and the comparison include it in every grouping.
        from llm_usage.reconciliation import RESIDUAL
        with self.store.connect() as c:
            self.store.event(c,'x',stamp('2026-10-07T03:00:00Z'),'OpenAI','codex','gpt-test',dict(uncached_input=100,output=50),session='t')
            self.store.account_tokens(c,'codex',[dict(startDate='2026-10-07',tokens=1000),dict(startDate='2026-09-30',tokens=400)],self.now)
        span=dict(period='custom',start='2026-10-05',end='2026-10-11',granularity='week',compare='previous')
        views={group:self.usage(group=group,**span) for group in ('provider','route','model','project')}
        sums=lambda series:[sum(total(v) for v in p['values'].values()) for p in series]
        for group,body in views.items():
            with self.subTest(group=group):
                self.assertEqual((total(body['totals']),body['totals']['residual_tokens'],body['reconciliation']['residual_tokens']),(1000,850,850))
                self.assertEqual((sums(body['series']),sums(body['compare']['series'])),([1000],[400]))
        model,project=views['model'],views['project']
        self.assertEqual(set(model['series'][0]['values']),{'gpt-test',RESIDUAL})
        self.assertEqual({k for k,v in model['compare']['series'][0]['values'].items() if total(v)},{RESIDUAL})
        self.assertEqual({r['model']:total(r) for r in model['rows']},{'gpt-test':150,RESIDUAL:850})
        # A local record without a project stays '미분류', apart from the residual.
        self.assertEqual(set(project['series'][0]['values']),{'미분류',RESIDUAL})
        self.assertEqual({p['project']:total(p) for p in project['projects']},{'미분류':150,RESIDUAL:850})
        # Neither is a model or a project to filter by.
        self.assertNotIn(RESIDUAL,model['scope_options']['model']+model['scope_options']['project'])
        self.assertNotIn('unknown',model['scope_options']['model'])
        with self.store.connect() as c:month=self.store.project_month(c,self.now)
        self.assertEqual((month['residual_tokens'],{k:v['tokens'] for k,v in month['projects'].items()}),(850,{'미분류':150}))
        # Narrowing to '미분류' or 'unknown' leaves it out; its service and provider keep it.
        scoped=lambda scope:self.usage(scope=scope,group='model',**span)
        unassigned=scoped('project:미분류')
        self.assertEqual((total(unassigned['totals']),unassigned['totals'].get('residual_tokens',0)),(150,0))
        self.assertEqual([r['model'] for r in unassigned['rows']],['gpt-test'])
        self.assertEqual(total(scoped('model:unknown')['totals']),0)
        self.assertEqual((total(scoped('route:codex')['totals']),total(scoped('provider:OpenAI')['totals'])),(1000,1000))
        self.assertEqual(sum(m['tokens'] for m in self.store.project_detail('미분류',now=self.now)['models']),150)

    def test_cumulative_results_are_differenced_not_summed(self):
        self.event();self.evidence()
        self.evidence(160,end='2026-10-12T11:00:00Z')
        u=self.usage(period='today',granularity='hour',group='route')
        self.assertEqual(total(u['totals']),160)
        self.assertEqual(sum(total(v) for p in u['series'] for v in p['values'].values()),160)
        self.assertEqual(self.usage(period='today',scope='project:other')['totals']['output'],0)

    def test_account_intervals_remain_unallocated_at_kst_midnight(self):
        with self.store.connect() as c:self.store.account_tokens(c,'codex',[dict(startDate='2026-10-11',tokens=1000)],self.now)
        today=self.usage(period='today')
        self.assertEqual(total(today['totals']),0)
        self.assertEqual(today['reconciliation']['overlapping_intervals'][0]['start'],'2026-10-11T09:00:00+09:00')
        u=self.usage(period='custom',start='2026-10-11',end='2026-10-12')
        self.assertEqual(total(u['totals']),1000)
        self.assertEqual(u['reconciliation']['unallocated_tokens'],1000)
        self.assertEqual(u['totals']['unclassified_tokens'],1000)
        self.assertEqual(u['totals']['requests'],0)
        self.assertIsNone(u['cost']['period'])
        week=self.usage(period='all',granularity='month')
        self.assertEqual(total(week['totals']),1000)
        self.assertEqual(week['reconciliation']['unallocated_tokens'],0)

    def test_counter_decrease_and_records_exceeding_total_are_visible(self):
        self.event(output=120);self.evidence(100)
        u=self.usage(period='today')
        self.assertEqual(total(u['totals']),120)
        self.assertEqual(u['reconciliation']['conflicts'][0]['reason'],'records_exceed_aggregate')
        self.evidence(50,end='2026-10-12T11:00:00Z')
        self.assertIn('counter_decreased',[x['reason'] for x in self.usage(period='today')['reconciliation']['conflicts']])

    def test_running_account_bucket_is_bounded_by_receipt_not_fabricated_time(self):
        with self.store.connect() as c:self.store.account_tokens(c,'codex',[dict(startDate='2026-10-12',tokens=1000)],self.now)
        u=self.usage(period='today')
        self.assertEqual(total(u['totals']),1000)
        self.assertEqual(u['reconciliation']['overlapping_intervals'],[])
        hour=self.usage(period='1h')
        self.assertEqual(total(hour['totals']),0)
        self.assertEqual(len(hour['reconciliation']['overlapping_intervals']),1)

    def test_session_project_and_report_use_reconciled_totals(self):
        self.event();self.evidence()
        d=self.store.session_detail('s')
        self.assertEqual(total(d['totals']),100)
        self.assertEqual(sum(total(m) for m in d['models']),100)
        p=self.store.project_detail('p',now=self.now)
        self.assertEqual(sum(m['tokens'] for m in p['models']),100)
        self.assertEqual(sum(d['tokens'] for d in p['daily']),100)
        u=self.usage(period='today')
        self.assertEqual(total(u['sessions'][0]),100)
        self.assertEqual(total(u['projects'][0]),100)

    def test_receiver_auth_size_and_content_type(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        from llm_usage.telemetry import handler
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.store,'fixture-token'))
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            for headers,status in [({},401),({'Authorization':'Bearer fixture-token','Content-Type':'application/x-protobuf'},415),
                                   ({'Authorization':'Bearer fixture-token','Content-Type':'application/json'},200)]:
                client=http.client.HTTPConnection(*server.server_address,timeout=3)
                client.request('POST','/v1/logs',body='{}',headers=headers)
                response=client.getresponse();self.assertEqual(response.status,status);response.read();client.close()
        finally:server.shutdown();server.server_close();worker.join(timeout=3)

    def test_real_cloud_shape_parent_tool_and_model_usage(self):
        item=dict(id='remote',created_at='2026-10-12T09:00:00Z')
        message={'payload':dict(type='assistant',session_id='s',timestamp='2026-10-12T09:30:00Z',parent_tool_use_id='tool1',
            message=dict(id='m1',model='claude-test',usage=dict(input_tokens=2,output_tokens=1,cache_read_input_tokens=30,cache_creation_input_tokens=4),content='NEVER STORE'))}
        result={'created_at':'2026-10-12T10:00:00Z','payload':dict(type='result',session_id='s',modelUsage={'claude-test':dict(inputTokens=2,outputTokens=20,cacheReadInputTokens=30,cacheCreationInputTokens=4)})}
        fetch=lambda *args:dict(data=[message,result])
        with self.store.connect() as c:_ingest(self.store,c,fetch,{},item,model_provider,'events')
        u=self.usage(period='today')
        self.assertEqual(total(u['totals']),56)
        self.assertEqual(u['reconciliation']['supplemental_tokens'],19)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT agent_kind FROM events').fetchone()[0],'sub')
            self.assertNotIn('NEVER STORE','\n'.join(c.iterdump()))

    def test_repeated_pagination_is_not_success(self):
        fetch=lambda *a:dict(data=[],next_cursor='same')
        with self.assertRaises(IncompletePages),self.store.connect() as c:
            _ingest(self.store,c,fetch,{},dict(id='s'),model_provider)

    def test_large_event_history_returns_a_durable_resume_cursor(self):
        from llm_usage.cloud import _batch
        calls=[]
        def fetch(url,headers):
            from urllib.parse import urlparse,parse_qs
            cursor=int(parse_qs(urlparse(url).query).get('cursor',['0'])[0]);calls.append(cursor)
            return dict(data=[],next_cursor=str(cursor+1) if cursor<22 else None)
        _,cursor=_batch(fetch,{},dict(id='s'))
        self.assertEqual(cursor,'20')
        _,cursor=_batch(fetch,{},dict(id='s'),cursor)
        self.assertIsNone(cursor)
        self.assertEqual(calls,list(range(23)))

    def test_reasoning_remains_part_of_output_and_is_not_added_twice(self):
        self.event(output=100)
        with self.store.connect() as c:
            snapshot(self.store,c,source='claude-cloud',scope_id='claude:s',route='claude-code',provider='Anthropic',
                     model='claude-test',start=stamp('2026-10-12T09:00:00Z'),end=stamp('2026-10-12T10:00:00Z'),tokens=100,
                     parts=dict(uncached_input=0,cached_input=0,cache_creation=0,output=100,reasoning=60))
        u=self.usage(period='today')
        self.assertEqual(total(u['totals']),100)
        self.assertEqual(u['totals']['reasoning'],60)

    def test_telemetry_waits_for_request_alias_and_discards_content(self):
        values={'event.name':'claude_code.api_request','request_id':'req1','session.id':'s','model':'claude-test',
                'input_tokens':2,'output_tokens':30,'cache_read_tokens':3,'cache_creation_tokens':4,'prompt':'PRIVATE'}
        record=dict(timeUnixNano=str(int((self.now-10)*1e9)),attributes=[dict(key=k,value={'stringValue':v} if isinstance(v,str) else {'intValue':str(v)}) for k,v in values.items()])
        data=dict(resourceLogs=[dict(scopeLogs=[dict(logRecords=[record])])])
        result=ingest(self.store,data,self.now)
        self.assertEqual(result['pending'],1)
        self.assertEqual(total(self.usage(period='today')['totals']),0)
        with self.store.connect() as c:
            parse_line(self.store,c,'claude-code',dict(type='assistant',timestamp=self.now-10,requestId='req1',sessionId='s',
                message=dict(id='m1',model='claude-test',usage=dict(input_tokens=2,output_tokens=1,cache_read_input_tokens=3,cache_creation_input_tokens=4))),{})
            self.assertEqual(resolve(self.store,c),1)
            receipt=self.store.state(c,'telemetry:received')
            self.assertEqual((receipt['pending'],receipt['checked'],receipt['status']),(0,self.now,'ok'))
            source=c.execute("SELECT status,checked FROM sources WHERE name='telemetry'").fetchone()
            self.assertEqual(tuple(source),('ok',self.now))
            self.assertNotIn('PRIVATE','\n'.join(c.iterdump()))
        ingest(self.store,data,self.now)
        self.assertEqual(total(self.usage(period='today')['totals']),39)
        self.assertEqual(self.usage(period='today')['totals']['requests'],1)


    def test_reports_follow_late_records_links_and_pricing_without_reannouncing(self):
        from llm_usage import reports
        from unittest.mock import patch
        start=stamp('2026-10-06T09:00:00Z');price={'claude-test':dict(input=1,cached=1,cache_write=1,output=1)}
        with self.store.connect() as c:
            self.store.event(c,'first',start+1,'Anthropic','claude-code','claude-test',dict(output=20_000_000),session='s')
            link(c,'claude:s','first','local')
            snapshot(self.store,c,source='claude-cloud',scope_id='claude:s',route='claude-code',provider='Anthropic',model='claude-test',
                     start=start,end=start+3600,tokens=100_000_000,parts=dict(uncached_input=0,cached_input=0,cache_creation=0,output=100_000_000))
        reports.ensure_report(self.store,self.now,price)
        self.assertEqual(reports.recent(self.store)[0]['totals']['supplemental_tokens'],80_000_000)
        with self.store.connect() as c:
            self.store.event(c,'late',start+2,'Anthropic','claude-code','claude-test',dict(output=30_000_000),session='s')
            link(c,'claude:s','late','local')
        self.assertIsNone(reports.ensure_report(self.store,self.now+1,price))
        totals=reports.recent(self.store)[0]['totals']
        self.assertEqual((totals['tokens'],totals['requests'],totals['supplemental_tokens'],totals['cost']),(100_000_000,2,50_000_000,50))
        with self.store.connect() as c:self.store.event(c,'unlinked',start+3,'Anthropic','claude-code','claude-test',dict(output=15_000_000),session='s')
        reports.ensure_report(self.store,self.now+2,price)
        self.assertEqual(reports.recent(self.store)[0]['totals']['tokens'],115_000_000)
        with self.store.connect() as c:link(c,'claude:s','unlinked','local')
        reports.ensure_report(self.store,self.now+3,price)
        self.assertEqual(reports.recent(self.store)[0]['totals']['tokens'],100_000_000)
        price['claude-test']['output']=2
        reports.ensure_report(self.store,self.now+4,price)
        self.assertEqual(reports.recent(self.store)[0]['totals']['cost'],130)
        with self.store.connect() as c:self.store.event(c,'current-week',self.now-5,'Anthropic','claude-code','claude-test',dict(output=10))
        with patch('llm_usage.reports.build',wraps=reports.build) as build:
            self.assertIsNone(reports.ensure_report(self.store,self.now+5,price));build.assert_not_called()

    def test_reports_include_evidence_when_there_are_no_local_requests(self):
        from llm_usage import reports
        self.evidence(100,start='2026-10-06T09:00:00Z',end='2026-10-06T10:00:00Z')
        reports.ensure_report(self.store,self.now)
        report=reports.recent(self.store)[0]
        self.assertEqual((report['totals']['tokens'],report['totals']['requests']),(100,0))

    def test_project_ranks_sessions_after_supplements_including_evidence_only(self):
        begin=stamp('2026-10-12T09:00:00Z')
        with self.store.connect() as c:
            for i in range(11):
                sid=f's-{i}'
                self.store.event(c,sid,begin+10,'Anthropic','claude-code','claude-test',dict(output=11-i),session=sid,project='p')
                link(c,'claude:'+sid,sid,'local')
            for sid,tokens in [('s-10',100),('only-evidence',200)]:
                snapshot(self.store,c,source='claude-cloud',scope_id='claude:'+sid,route='claude-code',provider='Anthropic',model='claude-test',
                         session=sid,project='p',start=begin,end=begin+3600,tokens=tokens,
                         parts=dict(uncached_input=0,cached_input=0,cache_creation=0,output=tokens))
        sessions=self.store.project_detail('p',now=self.now)['sessions']
        self.assertEqual(len(sessions),10)
        self.assertEqual([(r['session'],r['tokens'],r['requests']) for r in sessions[:2]],[('only-evidence',200,0),('s-10',100,1)])
        self.assertEqual([r['tokens'] for r in sessions],sorted([r['tokens'] for r in sessions],reverse=True))

    def test_calendar_uses_the_same_history_boundary_for_evidence_and_requests(self):
        from datetime import datetime,timedelta
        from llm_usage.store import KST
        cutoff=datetime.fromtimestamp(self.now,KST).replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=181)
        self.evidence(100,start=(cutoff-timedelta(hours=23)).isoformat(),end=(cutoff-timedelta(hours=22)).isoformat())
        old=self.usage(period='all')
        self.assertEqual(total(old['totals']),100);self.assertEqual(old['calendar'],[])
        with self.store.connect() as c:
            snapshot(self.store,c,source='claude-cloud',scope_id='claude:boundary',route='claude-code',provider='Anthropic',model='claude-test',
                     start=(cutoff+timedelta(hours=1)).timestamp(),end=(cutoff+timedelta(hours=2)).timestamp(),tokens=40,
                     parts=dict(uncached_input=0,cached_input=0,cache_creation=0,output=40))
        days=self.usage(period='all')['calendar']
        self.assertEqual([(d['day'],d['tokens']) for d in days],[(cutoff.date().isoformat(),40)])


if __name__=='__main__':unittest.main()

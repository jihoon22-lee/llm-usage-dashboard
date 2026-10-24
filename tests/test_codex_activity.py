import copy
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from llm_usage import codex_activity as activity
from llm_usage import codex_activity_collect as collector
from llm_usage.store import Store, identity, stamp
from llm_usage.webapp import create_app

NOW=stamp('2026-10-24T03:00:00Z')
SID='11111111-2222-4333-8444-555555555555'
CHILD='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
SECRET='PRIVATE-CONVERSATION-MUST-NOT-BE-PERSISTED'


def task(number=1,status='completed',created=None):
    return dict(id='task_'+str(number),title=SECRET,created_at=created or NOW-3600,updated_at=NOW-100,
                task_status_display={'initial_intent':'cr','latest_turn_status_display':{'turn_id':'turn_'+str(number),'turn_status':status}},
                pull_requests=[{'pull_request':{'url':'https://github.com/example/repo/pull/12','title':SECRET,'body':SECRET}}])


def detail(item,sid=SID,status='completed'):
    turn=dict(id='turn_'+item['id'],model_version='gpt-test',turn_status=status,created_at=NOW-3500,
              output_items=[{'text':SECRET}],logs=[SECRET],worklog={'messages':[{'content':SECRET}]},
              thread_events={'events':[{'method':'thread/started','params':{'thread':{'id':sid,'preview':SECRET}}},
                                       {'method':'item/completed','params':{'threadId':sid,'item':{'text':SECRET}}}]})
    return dict(task=item,current_assistant_turn=turn,current_diff_task_turn=copy.deepcopy(turn))


def usage(sid=SID,status='partial',weekly=1.25,as_of=NOW-60):
    row=dict(thread_id=sid,data_status=status,usage_source='warehouse',weekly_limit_percent=weekly,
             five_hour_limit_percent=None,balance_usage_credits='0E-10',groups=[dict(model='gpt-test',reasoning_effort='high',
               speed='standard',weekly_limit_percent=weekly,balance_usage_credits='0E-10')])
    if status=='unavailable':row.update(weekly_limit_percent=None,balance_usage_credits=None,groups=[])
    return dict(data_as_of=as_of,comparison_plan='pro',threads=[row])


def daily():
    return dict(units='percent',group_by='day',data=[dict(date='2026-10-24',
        attribution=[dict(thread_source='codex_web_code_review',turn_trigger='user',model='gpt-test',surface='github_code_review',value=2)],
        models=[dict(model='gpt-test',speed='standard',credits=2)],product_surface_usage_values={'github_code_review':2})])


class Api:
    def __init__(self):self.calls=[];self.pages={};self.details={};self.error=None
    def __call__(self,path,body=None):
        self.calls.append((path,body))
        if self.error:raise self.error
        if path.startswith('tasks/list?'):
            q=parse_qs(urlsplit(path).query);key=(q['task_filter'][0],q.get('cursor',[''])[0])
            return copy.deepcopy(self.pages.get(key,dict(items=[],cursor=None)))
        if path.startswith('tasks/'):
            return copy.deepcopy(self.details[path.split('/')[-1]])
        if path.startswith('usage/daily-token-usage-breakdown'):return daily()
        if path.startswith('usage/plan_limit_history'):
            return dict(data_as_of=NOW-60,coverage_start=NOW-86400,coverage_complete=False,approximate=True,
                        periods=[dict(starts_at=NOW-86400,ends_at=NOW+86400,plan_type='pro',window_minutes=10080,accounting_complete=False)])
        if path=='usage/thread_usage/query_v2':
            return dict(data_as_of=NOW-60,threads=[usage(t['thread_id'])['threads'][0] for t in body['threads']])
        raise AssertionError(path)


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'usage.db');self.account='fixture-account'
        with self.store.connect() as c:
            self.store.save_state(c,'codex_activity:account',dict(key=self.account,status='active'))
            self.store.event(c,'local-request',NOW-50,'OpenAI','codex','gpt-test',dict(uncached_input=5,cached_input=2,output=3))

    def add(self,item=None,payload=None):
        item=item or task()
        with self.store.connect() as c:
            key=activity.record_task(c,self.account,item,NOW)
            activity.record_detail(c,key,detail(item),NOW)
            if payload:activity.record_consumption(c,self.account,payload,[SID],NOW)
        return key

    def test_activity_does_not_change_any_token_ledger_or_totals(self):
        with self.store.connect() as c:
            before={name:[tuple(r) for r in c.execute('SELECT * FROM '+name)] for name in ('events','usage_evidence','account_token_buckets','usage_hourly')}
        key=self.add(payload=usage())
        with self.store.connect() as c:
            activity.record_daily(c,self.account,daily(),NOW)
            after={name:[tuple(r) for r in c.execute('SELECT * FROM '+name)] for name in before}
            dump='\n'.join(c.iterdump())
        self.assertEqual(before,after);self.assertNotIn(SECRET,dump)
        result=activity.task_detail(self.store,key,now=NOW)['task']
        self.assertEqual(result['run_count'],1);self.assertEqual(result['thread_count'],1)
        self.assertEqual(result['consumption']['payload']['weekly_limit_percent'],'1.25')
        self.assertEqual(result['consumption']['payload']['balance_usage_credits'],'0.0000000000')

    def test_same_pr_multiple_reviews_and_repeated_reads_are_distinct(self):
        first=self.add();self.add();self.add(task(2))
        result=activity.activity(self.store,now=NOW)
        self.assertEqual((result['total'],result['pr_count']),(2,1))
        self.assertEqual(activity.task_detail(self.store,first,now=NOW)['task']['run_count'],1)

    def test_provider_composite_turn_identity_with_tilde_is_retained_once(self):
        item=task();raw=detail(item)
        for slot in ('current_assistant_turn','current_diff_task_turn'):raw[slot]['id']='turn_fixture~attempt_fixture'
        with self.store.connect() as c:
            key=activity.record_task(c,self.account,item,NOW)
            activity.record_detail(c,key,raw,NOW)
        result=activity.task_detail(self.store,key,now=NOW)['task']
        self.assertEqual((result['run_count'],result['thread_count']),(1,1))
        self.assertEqual(result['runs'][0]['turn_id'],'turn_fixture~attempt_fixture')

    def test_updated_tasks_reopen_details_and_sparse_or_stale_details_do_not_replace_them(self):
        key=self.add();item=task();item['updated_at']=NOW+10
        with self.store.connect() as c:
            activity.record_task(c,self.account,item,NOW+11)
            self.assertEqual(c.execute('SELECT next_detail FROM codex_activity_tasks WHERE id=?',(key,)).fetchone()[0],0)
        with self.assertRaises(ValueError),self.store.connect() as c:activity.record_detail(c,key,{},NOW+12)
        with self.assertRaises(ValueError),self.store.connect() as c:activity.record_detail(c,key,detail(task()),NOW+12)

    def test_transport_failures_keep_resume_cursor(self):
        api=Api();api.pages[('code_review','')]=dict(items=[task()],cursor='next')
        collector._scan(self.store,self.account,api,'code_review',1,NOW)
        api.error=HTTPError('https://chatgpt.com/redacted',500,'failure',{},None)
        collector._lists_and_details(self.store,self.account,api,1,NOW+301,1,0)
        with self.store.connect() as c:state=self.store.state(c,'codex_activity:scan:'+self.account+':code_review')
        self.assertEqual(state['cursor'],'next');self.assertEqual(state['error'],'HTTP 500')

    def test_permanent_detail_failure_cannot_starve_unread_tasks(self):
        newest=task(1);older=task(2);older['created_at']-=100
        with self.store.connect() as c:
            first=activity.record_task(c,self.account,newest,NOW)
            second=activity.record_task(c,self.account,older,NOW)
        def fetch(path,body=None):
            if path.endswith('/task_1'):raise HTTPError('https://chatgpt.com/redacted',404,'gone',{},None)
            return detail(older)
        collector._details(self.store,self.account,fetch,1,NOW)
        collector._details(self.store,self.account,fetch,1,NOW+301)
        self.assertEqual(activity.task_detail(self.store,first,now=NOW+301)['task']['detail_status'],'error')
        self.assertEqual(activity.task_detail(self.store,second,now=NOW+301)['task']['detail_status'],'ok')

    def test_parent_child_threads_are_linked_without_summing_percentages(self):
        item=task();raw=detail(item);raw['current_assistant_turn']['child_turns']=[dict(id='child-turn',model_version='other-model',turn_status='completed',
            thread_events={'events':[{'method':'thread/started','params':{'thread':{'id':CHILD,'parentThreadId':SID}}}]})]
        raw['current_diff_task_turn']['child_turns']=raw['current_assistant_turn'].pop('child_turns')
        with self.store.connect() as c:
            key=activity.record_task(c,self.account,item,NOW)
            activity.record_detail(c,key,raw,NOW)
            u=usage();u['threads'].append(usage(CHILD,weekly=2)['threads'][0]);activity.record_consumption(c,self.account,u,[SID,CHILD],NOW)
        r=activity.task_detail(self.store,key,now=NOW)['task']
        self.assertEqual((r['run_count'],r['thread_count']),(2,2));self.assertIsNone(r['consumption'])
        self.assertIn(SID,[x['parent_thread'] for x in r['runs']])

    def test_failed_review_keeps_positive_consumption_and_cancelled_has_no_fake_zero(self):
        key=self.add(task(status='failed'),usage())
        r=activity.task_detail(self.store,key,now=NOW)['task']
        self.assertEqual(r['status'],'failed');self.assertEqual(r['consumption']['status'],'partial')
        item=task(2,status='cancelled')
        with self.store.connect() as c:
            key=activity.record_task(c,self.account,item,NOW)
            activity.record_detail(c,key,dict(task=item,current_assistant_turn=None,current_diff_task_turn=None),NOW)
        r=activity.task_detail(self.store,key,now=NOW)['task']
        self.assertEqual(r['consumption_status'],'unlinked');self.assertIsNone(r['consumption'])

    def test_unavailable_and_older_responses_preserve_previous_observation(self):
        key=self.add(payload=usage())
        with self.store.connect() as c:
            activity.record_consumption(c,self.account,usage(weekly=99,as_of=NOW-1000),[SID],NOW+10)
        self.assertEqual(activity.task_detail(self.store,key,now=NOW)['task']['consumption']['payload']['weekly_limit_percent'],'1.25')
        with self.store.connect() as c:activity.record_consumption(c,self.account,usage(status='unavailable',as_of=None),[SID],NOW+20)
        r=activity.task_detail(self.store,key,now=NOW+21)['task']['consumption']
        self.assertEqual(r['status'],'unavailable');self.assertIsNone(r['payload']['weekly_limit_percent'])
        self.assertEqual(r['previous']['weekly_limit_percent'],'1.25')

    def test_missing_null_zero_and_invalid_fields_remain_distinguishable(self):
        key=self.add();u=usage(weekly=0);del u['threads'][0]['balance_usage_credits']
        u['threads'][0]['five_hour_limit_percent']='bad';u['threads'][0]['data_status']='complete'
        with self.store.connect() as c:activity.record_consumption(c,self.account,u,[SID],NOW)
        r=activity.task_detail(self.store,key,now=NOW)['task']['consumption']
        self.assertEqual(r['status'],'partial');self.assertEqual(r['payload']['weekly_limit_percent'],'0')
        self.assertIn('balance_usage_credits',r['payload']['missing_fields'])
        self.assertIn('five_hour_limit_percent',r['payload']['invalid_fields'])
        for value in (True,-1,float('nan'),'NaN','Infinity','0E+1000000000','1E-1000000000'):
            self.assertIsNone(activity.decimal(value))

    def test_daily_units_mismatch_and_recollection_are_not_token_or_additive(self):
        raw=daily();raw['data'][0]['attribution'][0]['value']=1
        with self.store.connect() as c:
            self.assertEqual(activity.record_daily(c,self.account,raw,NOW),(1,1,0))
            activity.record_daily(c,self.account,raw,NOW+10)
        result=activity.consumption(self.store,now=NOW+10)
        self.assertEqual(len(result['days']),1);self.assertEqual(result['units'],'percent')
        self.assertEqual(result['days'][0]['totals'],dict(attribution='1',models='2',surfaces='2'))
        self.assertTrue(result['days'][0]['mismatch']);self.assertEqual(result['missing_days'],6)
        raw['units']='tokens'
        with self.assertRaises(ValueError),self.store.connect() as c:activity.record_daily(c,self.account,raw,NOW+20)
        self.assertEqual(activity.consumption(self.store,now=NOW+20)['days'][0]['checked'],NOW+10)

    def test_unknown_dates_search_and_kst_bounds(self):
        self.add();item=task(2);item['created_at']=None
        with self.store.connect() as c:activity.record_task(c,self.account,item,NOW)
        self.assertEqual(activity.activity(self.store,period='unknown',now=NOW)['total'],1)
        self.assertEqual(activity.activity(self.store,search='example/repo',now=NOW)['total'],1)
        self.assertEqual(activity.activity(self.store,search='%',now=NOW)['total'],0)
        r=activity.activity(self.store,period='custom',start='2026-10-24',end='2026-10-24',now=NOW)
        self.assertEqual(r['total'],1);self.assertEqual(r['end_exclusive'],'2026-10-25')
        self.assertEqual(activity.activity(self.store,period='custom',start='2026-10-23',end='2026-10-23',now=NOW)['total'],0)

    def test_account_change_hides_previous_accounts_and_rejects_old_detail(self):
        key=self.add(payload=usage())
        with self.store.connect() as c:
            activity.record_daily(c,self.account,daily(),NOW)
            self.store.save_state(c,'codex_activity:account',dict(key='different',status='active'))
        self.assertEqual(activity.activity(self.store,now=NOW)['total'],0)
        self.assertEqual(activity.consumption(self.store,now=NOW)['days'],[])
        with self.assertRaises(KeyError):activity.task_detail(self.store,key,now=NOW)

    def test_only_safe_links_and_metadata_are_retained(self):
        item=task();item['pull_requests'] += [{'url':'javascript:alert(1)'},{'url':'https://github.com.evil.test/u/r/pull/1'}]
        item['url']='https://chatgpt.com.evil.test/codex/tasks/task_1'
        key=self.add(item)
        r=activity.task_detail(self.store,key,now=NOW)['task']
        self.assertIsNone(r['url']);self.assertEqual(len(r['prs']),1)
        self.assertNotIn(SECRET,json.dumps(r))

    def test_collection_resumes_and_auth_errors_back_off_without_erasing_rows(self):
        api=Api();api.pages[('code_review','')]=dict(items=[task()],cursor='next')
        api.pages[('code_review','next')]=dict(items=[task(2)],cursor=None)
        api.details={t['id']:detail(t) for t in (task(),task(2))}
        config=dict(codex_binary='fixture')
        with patch.object(collector,'authentication',return_value=(self.account,{'Authorization':'PRIVATE'},7)):
            collector.collect(self.store,config,fetch=api,now=NOW,page_limit=1)
            self.assertEqual(activity.activity(self.store,now=NOW)['total'],1)
            collector.collect(self.store,config,fetch=api,now=NOW+301,page_limit=2)
            self.assertEqual(activity.activity(self.store,now=NOW+301)['total'],2)
            self.assertIn('cursor=next',' '.join(p for p,_ in api.calls))
            r=activity.activity(self.store,now=NOW+301)
            self.assertTrue(r['coverage']['code_review']['complete'])
            self.assertEqual(next(x for x in r['items'] if x['task_id']=='task_1')['consumption']['payload']['weekly_limit_percent'],'1.25')
            api.error=HTTPError('https://chatgpt.com/redacted',403,'forbidden',{},None)
            collector.collect(self.store,config,fetch=api,now=NOW+4000)
            count=len(api.calls);collector.collect(self.store,config,fetch=api,now=NOW+8000)
            self.assertEqual(len(api.calls),count)
        self.assertEqual(activity.activity(self.store,now=NOW+8000)['total'],2)
        with self.store.connect() as c:self.assertNotIn('PRIVATE','\n'.join(c.iterdump()))

    def test_repeated_cursor_and_transaction_failure_preserve_existing_data(self):
        api=Api();api.pages[('code_review','')]=dict(items=[task()],cursor='same')
        api.pages[('code_review','same')]=dict(items=[task(2)],cursor='same')
        with self.assertRaises(ValueError):collector._scan(self.store,self.account,api,'code_review',3,NOW)
        self.assertEqual(activity.activity(self.store,now=NOW)['total'],1)
        with self.store.connect() as c:state=self.store.state(c,'codex_activity:scan:'+self.account+':code_review')
        self.assertFalse(state['complete']);self.assertEqual(state['cursor'],'same')

    def test_http_200_with_missing_thread_rows_does_not_refresh_receipt(self):
        key=self.add(payload=usage())
        with self.store.connect() as c:
            self.assertEqual(activity.record_consumption(c,self.account,dict(threads=[]),[SID],NOW+10),(0,1))
        self.assertEqual(activity.task_detail(self.store,key,now=NOW+10)['task']['consumption']['checked'],NOW)

    def test_rate_limit_survives_credential_refresh_and_auth_block_can_recover(self):
        error=HTTPError('https://chatgpt.com/redacted',429,'limited',{'Retry-After':'1800'},None)
        collector._failed(self.store,'fixture-poll',1,error,NOW)
        self.assertFalse(collector._allowed(self.store,'fixture-poll',2,NOW+301))
        self.assertTrue(collector._allowed(self.store,'fixture-poll',2,NOW+1801))
        collector._failed(self.store,'fixture-auth',1,HTTPError('https://chatgpt.com/redacted',403,'forbidden',{},None),NOW)
        self.assertFalse(collector._allowed(self.store,'fixture-auth',1,NOW+9000))
        self.assertTrue(collector._allowed(self.store,'fixture-auth',2,NOW+1))

    def test_optional_plan_history_respects_retry_after_while_daily_values_continue(self):
        api=Api();plan_calls=[]
        def fetch(path,body=None):
            if path.startswith('usage/plan_limit_history'):
                plan_calls.append(path)
                raise HTTPError('https://chatgpt.com/redacted',429,'limited',{'Retry-After':'7200'},None)
            return api(path,body)
        with patch.object(collector,'authentication',return_value=(self.account,{},1)):
            collector.collect(self.store,dict(codex_binary='fixture'),fetch=fetch,now=NOW)
            collector.collect(self.store,dict(codex_binary='fixture'),fetch=fetch,now=NOW+3601)
        self.assertEqual(len(plan_calls),1)
        result=activity.consumption(self.store,now=NOW+3601)
        self.assertEqual(result['days'][0]['checked'],NOW+3601)
        self.assertEqual(result['plan']['error'],'HTTP 429')
        with patch.object(collector,'authentication',return_value=(self.account,{},2)):
            collector.collect(self.store,dict(codex_binary='fixture'),fetch=fetch,now=NOW+3602)
            self.assertEqual(len(plan_calls),1)
            collector.collect(self.store,dict(codex_binary='fixture'),fetch=fetch,now=NOW+7203)
        self.assertEqual(len(plan_calls),2)

    def test_pending_detail_and_missing_native_thread_are_not_the_same_state(self):
        with self.store.connect() as c:key=activity.record_task(c,self.account,task(),NOW)
        self.assertEqual(activity.task_detail(self.store,key,now=NOW)['task']['consumption_status'],'pending')
        with self.store.connect() as c:c.execute("UPDATE codex_activity_tasks SET detail_status='error' WHERE id=?",(key,))
        self.assertEqual(activity.task_detail(self.store,key,now=NOW)['task']['consumption_status'],'detail_error')

    def test_client_rejects_external_destinations_and_disables_redirects(self):
        from llm_usage.limits import NoRedirect
        with patch('urllib.request.build_opener') as build:
            client=collector.Client({'Authorization':'fixture-secret'})
            self.assertIs(build.call_args.args[0],NoRedirect)
            for path in ('https://external.test/','/tasks/task_1','usage/https://external.test/'):
                with self.assertRaises(ValueError):client(path)
            build.return_value.open.assert_not_called()

    def test_data_age_and_plan_uncertainty_are_visible(self):
        key=self.add(payload=usage(as_of=NOW-90000))
        self.assertTrue(activity.task_detail(self.store,key,now=NOW)['task']['consumption']['stale'])
        plan=activity.plan_metadata(dict(data_as_of=NOW-86400,coverage_complete=False,approximate=True,periods=[dict(accounting_complete=False)]),NOW)
        self.assertFalse(plan['coverage_complete']);self.assertTrue(plan['approximate'])
        self.assertFalse(plan['periods'][0]['accounting_complete'])

    def test_api_auth_validation_and_reads_never_call_provider(self):
        key=self.add(payload=usage())
        app=create_app(dict(database=self.store.path,origin='https://dashboard.test',secret_key='fixture',allowed_logins=['owner']))
        client=app.test_client();kwargs=dict(headers={'Tailscale-User-Login':'owner'},environ_overrides={'gunicorn.socket':SimpleNamespace(family=socket.AF_UNIX)})
        with patch.object(collector.Client,'__call__',side_effect=AssertionError('network on GET')):
            for url in ('/api/codex/activity','/api/codex/activity/'+key,'/api/codex/consumption'):
                self.assertEqual(client.get(url,**kwargs).status_code,200)
                self.assertEqual(client.get(url).status_code,403)
        for url in ('/api/codex/consumption?days=8','/api/codex/activity?period=custom&start=bad','/api/codex/activity?page=-1','/api/codex/activity/bad'):
            self.assertEqual(client.get(url,**kwargs).status_code,400)


if __name__=='__main__':unittest.main()

"""Regressions found in the 2026-10-04 code review: report double counting, held and
retried notifications, elapsed-time comparison, tier pricing, report backfill,
unpriced chart buckets and the shared usage cache."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from llm_usage import notify, reports
from llm_usage.pricing import BUILTIN, entry_key
from llm_usage.store import Store, stamp

NOW=stamp('2026-10-03T09:00:00Z')  # Saturday 18:00 KST
NTFY={'ntfy_url':'https://ntfy.example/topic'}
PRICING={'gpt-test':dict(input=1,cached=0.1,output=10,cache_write=1)}


class Response:
    def __enter__(self):return self
    def __exit__(self,*exc):return False
    def read(self,n):return b''


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'usage.db')


class ReportExhaustionTests(Base):
    def test_two_streams_of_one_bucket_count_once(self):
        with patch('llm_usage.store.time.time',return_value=NOW),self.store.connect() as c:
            self.store.event(c,'w',stamp('2026-09-29T01:00:00Z'),'OpenAI','codex','gpt-test',dict(output=1))
            start=stamp('2026-09-30T00:00:00Z')
            for source,offset in (('codex',0),('codex-local',90)):
                for i,remaining in enumerate((20,0,100,0)):
                    self.store.limit(c,'codex','codex · 10080분',remaining,NOW+86400,start+i*600+offset,source)
            # A bucket seen only by a local stream still counts.
            for i,remaining in enumerate((5,0)):
                self.store.limit(c,'claude-code','five_hour',remaining,NOW+3600,start+i*600,'claude-code')
        with self.store.connect() as c:
            found=reports.exhaustions(c,stamp('2026-09-28T00:00:00+09:00'),stamp('2026-10-05T00:00:00+09:00'))
        self.assertEqual({(e['route'],e['bucket']):e['count'] for e in found},
                         {('codex','codex · 10080분'):2,('claude-code','five_hour'):1})

    def test_stored_reports_are_recounted_once_within_retained_history(self):
        with patch('llm_usage.store.time.time',return_value=NOW),self.store.connect() as c:
            start=stamp('2026-09-30T00:00:00Z')
            for source,offset in (('codex',0),('codex-local',90)):
                for i,remaining in enumerate((20,0)):
                    self.store.limit(c,'codex','codex · 10080분',remaining,NOW+86400,start+i*600+offset,source)
            self.store.save_state(c,'report:2026-09-28',dict(start='2026-09-28',exhausted=[dict(route='codex',bucket='codex · 10080분',count=2)]))
            self.store.save_state(c,'report:2026-08-03',dict(start='2026-08-03',exhausted=[dict(route='x',bucket='y',count=9)]))
        reports.recount_exhaustions(self.store,NOW)
        with self.store.connect() as c:
            self.assertEqual(self.store.state(c,'report:2026-09-28')['exhausted'][0]['count'],1)
            self.assertEqual(self.store.state(c,'report:2026-08-03')['exhausted'][0]['count'],9)  # history no longer kept

    def test_missed_weeks_are_backfilled_and_only_the_newest_is_returned(self):
        with self.store.connect() as c:
            for day in ('2026-09-08','2026-09-15','2026-09-22','2026-09-29'):
                self.store.event(c,day,stamp(day+'T01:00:00Z'),'OpenAI','codex','gpt-test',dict(output=10))
        newest=reports.ensure_report(self.store,stamp('2026-10-05T01:00:00Z'),PRICING)
        self.assertEqual(newest['start'],'2026-09-28')
        self.assertEqual([r['start'] for r in reports.recent(self.store)],['2026-09-28','2026-09-21','2026-09-14','2026-09-07'])
        # Nothing before the first recorded week, and nothing new to announce next time.
        self.assertIsNone(reports.ensure_report(self.store,stamp('2026-10-05T02:00:00Z'),PRICING))
        # A missed older week alone is filled in quietly.
        with self.store.connect() as c:c.execute("DELETE FROM state WHERE key='report:2026-09-14'")
        self.assertIsNone(reports.ensure_report(self.store,stamp('2026-10-05T03:00:00Z'),PRICING))
        self.assertEqual(len(reports.recent(self.store)),4)


class OutboxTests(Base):
    def deliver(self,messages,config,now,fail=False):
        def opener(request,timeout):
            if fail:raise OSError('down')
            payload=json.loads(request.data);self.sent.append(payload['title'])
            self.bodies=getattr(self,'bodies',[])+[payload['message']];return Response()
        with patch('llm_usage.notify.urllib.request.urlopen',side_effect=opener):
            return notify.deliver(self.store,config,messages,now)

    def test_quiet_hours_hold_then_send_after(self):
        self.sent=[]
        quiet={**NTFY,'quiet':[17,19]}  # NOW is 18:00 KST
        self.assertEqual(self.deliver([('report','주간 리포트','본문')],quiet,NOW),[])
        self.assertEqual(self.sent,[])
        self.assertEqual(self.deliver([],quiet,NOW+1800),[])
        sent=self.deliver([],quiet,NOW+3600+60)  # 19:01 KST
        self.assertEqual([m[0] for m in sent],['report']);self.assertEqual(self.sent,['주간 리포트'])
        # The delivered text says when it happened; the stored message stays unchanged.
        self.assertEqual(self.bodies[-1],'본문 · 10-03 18:00 KST 발생');self.assertEqual(sent[0][2],'본문')
        self.assertEqual(self.deliver([],quiet,NOW+7200),[])
        statuses=[e['status'] for e in notify.recent_log(self.store)]
        self.assertEqual(statuses,['resent','held'])

    def test_many_held_messages_become_one_digest(self):
        self.sent=[]
        quiet={**NTFY,'quiet':[17,19]}
        self.deliver([(k,f'제목 {k}','본문') for k in ('low','exhausted','spike','budget','source')],quiet,NOW)
        sent=self.deliver([],quiet,NOW+3700)
        self.assertEqual(len(sent),5);self.assertEqual(self.sent,['방해 금지 동안 알림 5건'])

    def test_failed_send_is_retried_and_expires(self):
        self.sent=[]
        self.assertEqual(self.deliver([('low','잔여 적음','본문')],NTFY,NOW,fail=True),[])
        with self.store.connect() as c:
            self.assertEqual(self.store.state(c,'notify:outbox')[0]['attempts'],1)
            self.assertEqual(dict(c.execute("SELECT status FROM sources WHERE name='외부 알림'").fetchone())['status'],'error')
        self.assertEqual([m[0] for m in self.deliver([],NTFY,NOW+300)],['low'])
        self.assertEqual(self.sent,['잔여 적음'])
        self.assertEqual(self.bodies[-1].count('발생'),1)  # retries do not stack the note
        # A message that cannot be sent within a day is dropped and recorded as expired.
        self.deliver([('spike','급증','본문')],NTFY,NOW+600,fail=True)
        self.assertEqual(self.deliver([],NTFY,NOW+600+2*86400),[])
        self.assertEqual(notify.recent_log(self.store)[0]['status'],'expired')

    def test_disabled_kind_is_not_held(self):
        self.sent=[]
        config={**NTFY,'quiet':[17,19],'events':{'low':False}}
        self.deliver([('low','잔여 적음','본문')],config,NOW)
        with self.store.connect() as c:self.assertEqual(self.store.state(c,'notify:outbox'),[])


class CompareTests(Base):
    def test_card_comparison_stops_at_the_same_elapsed_time(self):
        now=stamp('2026-10-03T03:00:00Z')  # 12:00 KST
        with self.store.connect() as c:
            # Previous day: 100 before noon, 900 after; today: 150 before noon.
            self.store.event(c,'y-am',stamp('2026-10-02T00:00:00Z'),'OpenAI','codex','gpt-test',dict(output=100))
            self.store.event(c,'y-pm',stamp('2026-10-02T08:00:00Z'),'OpenAI','codex','gpt-test',dict(output=900))
            self.store.event(c,'t-am',stamp('2026-10-03T00:00:00Z'),'OpenAI','codex','gpt-test',dict(output=150))
        data=self.store.usage(period='today',compare='previous',now=now,sections='core')
        self.assertEqual(data['compare']['elapsed']['output'],100)
        self.assertEqual(data['compare']['elapsed']['end_exclusive'],'2026-10-02T12:00:00+09:00')
        self.assertEqual(sum(v.get('output',0) for p in data['compare']['series'] for v in p['values'].values()),1000)


class PricingTierTests(unittest.TestCase):
    def test_tier_suffixes_do_not_inherit_a_price(self):
        table={**BUILTIN,'gemini-3.1-pro':dict(input=1,output=1)}
        for model in ('gpt-5-nano','gpt-5-pro','gpt-5-mini-high','gemini-2.5-flash-lite'):
            self.assertNotEqual(entry_key(model,table),'gpt-5' if model.startswith('gpt') else 'gemini-2.5-flash',model)
        self.assertEqual(entry_key('gpt-5-mini-2025-08-07',table),'gpt-5-mini')
        for model,key in (('gpt-5-codex','gpt-5'),('gemini-2.5-pro-preview-06-05','gemini-2.5-pro'),
                          ('claude-opus-5-5-medium','claude-opus-5-5'),('gemini-3.1-pro-low','gemini-3.1-pro'),
                          ('claude-opus-4-20250514','claude-opus-4')):
            self.assertEqual(entry_key(model,table),key,model)


class UnpricedSeriesTests(Base):
    def test_chart_buckets_carry_unpriced_tokens(self):
        now=stamp('2026-10-03T03:00:00Z')
        with self.store.connect() as c:
            self.store.event(c,'a',now-60,'OpenAI','codex','gpt-test',dict(output=1000))
            self.store.event(c,'b',now-30,'OpenAI','codex','gpt-noprice',dict(output=7))
        data=self.store.usage(period='today',group='route',now=now,pricing=PRICING,sections='core')
        value=data['series'][0]['values']['codex']
        self.assertEqual(value['unpriced'],7);self.assertAlmostEqual(value['cost'],0.01)


class CacheThreadTests(Base):
    def test_concurrent_cache_use_does_not_raise(self):
        errors=[]
        def work(n):
            try:
                for i in range(400):
                    self.store._usage_put((n,i%20),{});self.store._usage_get((n,(i+7)%20))
            except Exception as exc:errors.append(exc)
        threads=[threading.Thread(target=work,args=(n,)) for n in range(4)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(errors,[])
        self.assertLessEqual(len(self.store._usage_cache),12)


class CollectionCostTests(Base):
    def test_only_changed_or_failing_files_are_opened(self):
        from llm_usage.collect import changed_files, collect_file
        root=Path(self.tmp.name)/'records';root.mkdir()
        files=[root/f'{i}.jsonl' for i in range(3)]
        for f in files:f.write_text('{}\n')
        for f in files:collect_file(self.store,f,'claude-code')
        self.assertEqual(list(changed_files(self.store,root,files)),[])
        files[1].write_text('{}\n{}\n')
        with self.store.connect() as c:self.store.import_error(c,'file:'+str(files[2]),0,'ValueError')
        self.assertEqual(list(changed_files(self.store,root,files)),files[1:])

    def test_unchanged_devin_database_is_not_copied_again(self):
        import sqlite3
        from llm_usage import devin
        db=Path(self.tmp.name)/'sessions.db'
        src=sqlite3.connect(db)
        src.execute('CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY, session_id TEXT, node_id TEXT, created_at REAL, chat_message TEXT)')
        src.commit();src.close()
        devin.collect_database(self.store,db,lambda m:'Cognition')
        with patch('llm_usage.devin.open_db',side_effect=AssertionError('reopened')):
            self.assertEqual(devin.collect_database(self.store,db,lambda m:'Cognition'),0)

    def test_repeated_source_state_is_not_rewritten(self):
        with self.store.connect() as c:
            self.store.source_changed(c,'x','unavailable','대기')
            first=c.execute("SELECT checked FROM sources WHERE name='x'").fetchone()[0]
        with patch('llm_usage.store.time.time',return_value=first+100),self.store.connect() as c:
            self.store.source_changed(c,'x','unavailable','대기')
            self.assertEqual(c.execute("SELECT checked FROM sources WHERE name='x'").fetchone()[0],first)
            self.store.source_changed(c,'x','error','실패')
            self.assertEqual(c.execute("SELECT status FROM sources WHERE name='x'").fetchone()[0],'error')


class FeatureTests(Base):
    def test_cache_savings_use_the_list_rate_difference(self):
        now=stamp('2026-10-03T03:00:00Z')
        with self.store.connect() as c:
            self.store.event(c,'a',now-60,'OpenAI','codex','gpt-test',dict(cached_input=2_000_000,output=0))
            self.store.event(c,'b',now-30,'OpenAI','codex','gpt-noprice',dict(cached_input=9_000_000))
        cost=self.store.usage(period='today',now=now,pricing=PRICING,sections='core')['cost']
        self.assertAlmostEqual(cost['cache_savings'],2*(1-0.1))

    def test_project_detail_models_days_sessions_and_month_projection(self):
        now=stamp('2026-10-10T03:00:00Z')  # 10th of the month, KST noon
        with self.store.connect() as c:
            self.store.event(c,'p1',stamp('2026-10-02T01:00:00Z'),'OpenAI','codex','gpt-test',dict(output=1_000_000),session='s1',project='proj')
            self.store.event(c,'p2',stamp('2026-10-09T01:00:00Z'),'OpenAI','codex','gpt-test',dict(output=1_000_000),session='s2',project='proj')
            self.store.event(c,'p3',stamp('2026-09-20T01:00:00Z'),'OpenAI','codex','gpt-test',dict(output=500_000),session='s3',project='proj')
            self.store.event(c,'x',stamp('2026-10-09T01:00:00Z'),'OpenAI','codex','gpt-test',dict(output=7),project='other')
        d=self.store.project_detail('proj',now,PRICING,budget={'usd':40})
        self.assertEqual([m['tokens'] for m in d['models']],[2_500_000])
        self.assertEqual([x['day'] for x in d['daily']],['2026-09-20','2026-10-02','2026-10-09'])
        self.assertEqual([x['session'] for x in d['sessions']],['s1','s2','s3'])
        month=d['month']
        self.assertEqual((month['tokens'],month['cost']),(2_000_000,20.0))
        self.assertAlmostEqual(month['projected_cost'],20/10*31)
        self.assertAlmostEqual(month['budget_ratio'],0.5);self.assertAlmostEqual(month['projected_budget_ratio'],62/40)

    def test_capacity_history_per_finished_window(self):
        now=stamp('2026-10-03T03:00:00Z');span=18000
        with patch('llm_usage.store.time.time',return_value=now),self.store.connect() as c:
            for w,(reset,lowest,tokens) in enumerate(((now-3*span,60,4_000_000),(now-span,80,1_000_000))):
                for i,remaining in enumerate((100,90,lowest)):
                    self.store.limit(c,'claude-code','five_hour',remaining,reset+(1 if i==1 else 0),reset-span+600+i*600,'claude-oauth')
                self.store.event(c,f'w{w}',reset-1000,'Anthropic','claude-code','claude-x',dict(output=tokens))
            self.store.limit(c,'claude-code','five_hour',97,now+span-60,now-60,'claude-oauth')
            self.store.source(c,'claude-oauth','ok','',now-60)
        row=next(r for r in self.store.limits(now)['limits'] if r['bucket']=='five_hour')
        self.assertEqual([(h['used_points'],h['tokens_per_point']) for h in row['capacity_history']],[(40,100_000),(20,50_000)])

    def test_notification_history_endpoint_has_no_secrets(self):
        import socket
        from types import SimpleNamespace
        from llm_usage.webapp import create_app
        app=create_app(dict(database=self.store.path,origin='https://pc.example',secret_key='t',allowed_logins=['owner']))
        with patch('llm_usage.notify.urllib.request.urlopen',side_effect=lambda r,timeout:Response()):
            notify.deliver(self.store,{'ntfy_url':'https://ntfy.example/SECRET-TOPIC'},[('low','잔여 적음','본문')],NOW)
        r=app.test_client().get('/api/notify/log',base_url='https://pc.example',headers={'Tailscale-User-Login':'owner'},
                                environ_overrides={'gunicorn.socket':SimpleNamespace(family=socket.AF_UNIX)})
        self.assertEqual(r.get_json()['log'][0]['status'],'sent');self.assertNotIn('SECRET',r.get_data(as_text=True))


class AssetCacheTests(unittest.TestCase):
    def test_versioned_assets_cache_and_data_does_not(self):
        import socket
        from types import SimpleNamespace
        from llm_usage.webapp import asset_version, create_app
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        app=create_app(dict(database=str(Path(tmp.name)/'usage.db'),origin='https://pc.example',secret_key='t',allowed_logins=['owner']))
        client=app.test_client()
        env={'gunicorn.socket':SimpleNamespace(family=socket.AF_UNIX)}
        def get(path):
            response=client.get(path,base_url='https://pc.example',headers={'Tailscale-User-Login':'owner'},environ_overrides=env)
            response.get_data();response.close();return response
        version=asset_version(app.static_folder)
        page=get('/').get_data(as_text=True)
        self.assertIn(f'/assets/app.js?v={version}',page);self.assertNotIn('{{',page)
        self.assertIn('immutable',get(f'/assets/app.js?v={version}').headers['Cache-Control'])
        self.assertEqual(get('/assets/app.js?v=old').headers['Cache-Control'],'no-store')
        self.assertEqual(get('/api/limits').headers['Cache-Control'],'no-store')
        self.assertEqual(get('/').headers['Cache-Control'],'no-store')


if __name__=='__main__':unittest.main()

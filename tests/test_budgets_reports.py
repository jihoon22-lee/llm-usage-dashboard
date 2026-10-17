"""Project budgets (month-to-date, alerts) and weekly reports."""
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from llm_usage import notify, reports
from llm_usage.store import Store, stamp
from llm_usage.webapp import create_app

NOW=stamp('2026-10-03T09:00:00Z')  # Saturday 18:00 KST
PRICING={'gpt-test':dict(input=1,cached=0.1,output=10,cache_write=1)}


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'usage.db')

    def test_month_to_date_per_project_starts_at_kst_month(self):
        with self.store.connect() as c:
            self.store.event(c,'sep',stamp('2026-09-30T14:59:00Z'),'OpenAI','codex','gpt-test',dict(uncached_input=1_000_000),project='a')
            self.store.event(c,'oct',stamp('2026-09-30T15:00:00Z'),'OpenAI','codex','gpt-test',dict(uncached_input=2_000_000),project='a')
            self.store.event(c,'free',NOW-60,'OpenAI','codex','unpriced',dict(output=500),project='b')
            self.store.event(c,'none',NOW-60,'OpenAI','codex','gpt-test',dict(output=100))
            month=self.store.project_month(c,NOW,PRICING)
        self.assertEqual(month['month'],'2026-10')
        self.assertEqual(month['projects']['a'],dict(tokens=2_000_000,cost=2.0,priced_share=100.0))
        self.assertEqual(month['projects']['b'],dict(tokens=500,cost=None,priced_share=0.0))
        self.assertIn('미분류',month['projects'])

    def test_budget_level_and_crossings_once_per_month(self):
        self.assertEqual(notify.budget_level(dict(tokens=850,cost=1.0),dict(tokens=1000)),(80,0.85))
        self.assertEqual(notify.budget_level(dict(tokens=10,cost=12.0),dict(tokens=1000,usd=10))[0],100)
        self.assertEqual(notify.budget_level(dict(tokens=10,cost=None),dict(usd=10)),(0,0))
        month=lambda m,t:dict(month=m,projects={'a':dict(tokens=t,cost=None)})
        budgets={'a':dict(tokens=1000)}
        sent,state=notify.budget_messages(month('2026-10',500),budgets,{})
        self.assertEqual(sent,[])
        sent,state=notify.budget_messages(month('2026-10',820),budgets,state)
        self.assertEqual([m[1] for m in sent],['a 월 예산 80% 도달'])
        sent,state=notify.budget_messages(month('2026-10',900),budgets,state);self.assertEqual(sent,[])
        sent,state=notify.budget_messages(month('2026-10',1001),budgets,state)
        self.assertEqual([m[1] for m in sent],['a 월 예산 초과'])
        sent,state=notify.budget_messages(month('2026-11',850),budgets,state)
        self.assertEqual([m[1] for m in sent],['a 월 예산 80% 도달'])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'usage.db')
        with patch('llm_usage.store.time.time',return_value=NOW),self.store.connect() as c:
            # Week of 09-21 (previous) and 09-28 (reported); 10-03 is the running week.
            self.store.event(c,'p1',stamp('2026-09-22T01:00:00Z'),'OpenAI','codex','gpt-test',dict(uncached_input=1000))
            self.store.event(c,'w1',stamp('2026-09-29T01:00:00Z'),'OpenAI','codex','gpt-test',dict(uncached_input=1500,output=500))
            self.store.event(c,'w2',stamp('2026-10-01T01:00:00Z'),'Anthropic','claude-code','claude-x',dict(output=300))
            self.store.event(c,'now',NOW-60,'OpenAI','codex','gpt-test',dict(uncached_input=9_999))
            # 20 → 0 (exhausted) → 0 → 100 (new window) → 0 (exhausted again)
            for i,remaining in enumerate((20,0,0,100,0)):
                self.store.limit(c,'codex','codex · 10080분',remaining,NOW+86400,stamp('2026-09-30T00:00:00Z')+i*600,'codex')

    def test_last_finished_week_is_monday_to_sunday_kst(self):
        # On Saturday 10-03 the running week is not finished; the last finished one is 09-21~27.
        monday,next_monday=reports.last_week(NOW)
        self.assertEqual((monday.isoformat(),next_monday.isoformat()),('2026-09-21T00:00:00+09:00','2026-09-28T00:00:00+09:00'))
        monday,_=reports.last_week(stamp('2026-10-04T15:30:00Z'))  # Monday 00:30 KST
        self.assertEqual(monday.isoformat(),'2026-09-28T00:00:00+09:00')

    def test_report_totals_change_models_and_exhaustions(self):
        report=reports.build(self.store,stamp('2026-10-05T01:00:00Z'),PRICING)
        self.assertEqual((report['start'],report['end']),('2026-09-28','2026-10-04'))
        self.assertEqual(report['totals']['tokens'],1500+500+300+9_999)
        self.assertEqual(report['previous']['tokens'],1000)
        routes={r['route']:r for r in report['routes']}
        self.assertEqual(routes['codex']['tokens'],1500+500+9_999)
        self.assertAlmostEqual(routes['codex']['change_pct'],(11_999-1000)/1000*100)
        self.assertIsNone(routes['claude-code']['change_pct'])
        self.assertEqual(report['models'][0]['model'],'gpt-test')
        self.assertEqual(report['exhausted'],[dict(route='codex',bucket='codex · 10080분',count=2,display_name=None)])
        self.assertIn('Codex codex · 10080분 2회',reports.summary(report))
        self.assertIn('전주 대비',reports.summary(report))

    def test_quota_use_counts_observed_and_zero_time(self):
        report=reports.build(self.store,stamp('2026-10-05T01:00:00Z'),PRICING)
        codex=next(q for q in report['quota_use'] if q['route']=='codex')
        # Five readings 10 minutes apart cover 40 minutes, 10 of them at 0% (0 → 0); the rise to
        # 100 without a new reset time is not a window reset, so no before-reset value is recorded.
        self.assertEqual((codex['observed_hours'],codex['zero_hours'],codex['resets_observed'],codex['before_reset']),(0.67,0.17,0,None))
        self.assertEqual((codex['exhausted'],codex['streak']),(2,1))
        self.assertIn('0%로 머문 시간 Codex codex · 10080분 10분',reports.summary(report))

    def test_before_reset_needs_observation_across_the_reset(self):
        start=stamp('2026-09-29T00:00:00Z')
        with patch('llm_usage.store.time.time',return_value=NOW),self.store.connect() as c:
            # Observed every 5 minutes up to and across the reset: 35% was left unused.
            for i,remaining in enumerate((60,50,40,35)):
                self.store.limit(c,'claude-code','five_hour',remaining,start+3600,start+2640+i*300,'claude-oauth')
            self.store.limit(c,'claude-code','five_hour',100,start+21600,start+3840,'claude-oauth')
            # The next window resets while nothing is observed: no value is guessed for it.
            self.store.limit(c,'claude-code','five_hour',80,start+39600,start+28800,'claude-oauth')
            # A window that ends with no new one started reads without a reset time; the reset
            # was still seen (20% left).
            for i,(remaining,resets) in enumerate(((20,start+39600),(20,start+39600),(100,None))):
                self.store.limit(c,'claude-code','five_hour',remaining,resets,start+39000+i*300,'claude-oauth')
        report=reports.build(self.store,stamp('2026-10-05T01:00:00Z'),PRICING)
        claude=next(q for q in report['quota_use'] if q['route']=='claude-code')
        self.assertEqual((claude['resets_observed'],claude['before_reset']),(2,dict(avg=27.5,min=20,max=35)))
        self.assertEqual((claude['observed_hours'],claude['zero_hours'],claude['exhausted']),(0.5,0,0))

    def test_moving_reset_time_of_an_idle_window_is_not_a_reset(self):
        start=stamp('2026-09-29T00:00:00Z')
        with patch('llm_usage.store.time.time',return_value=NOW),self.store.connect() as c:
            # An unused 5-hour window reports a reset five hours after every reading; then a
            # reading of the stale reset time after it passed is followed by the new window.
            for i in range(6):
                self.store.limit(c,'antigravity','gemini-5h',100,start+i*300+18000,start+i*300,'antigravity-app')
            for i,(remaining,resets) in enumerate(((0,start+3000),(0,start+3000),(0,start+3000),(100,start+3000+18000))):
                self.store.limit(c,'antigravity','3p-5h',remaining,resets,start+2700+i*300,'antigravity-app')
        report=reports.build(self.store,stamp('2026-10-05T01:00:00Z'),PRICING)
        use={q['bucket']:q for q in report['quota_use'] if q['route']=='antigravity'}
        self.assertEqual((use['gemini-5h']['observed_hours'],use['gemini-5h']['resets_observed']),(0.42,0))
        # The window reset at +3000 once, with 0% left; the stale reading after it is not another reset.
        self.assertEqual((use['3p-5h']['resets_observed'],use['3p-5h']['before_reset']),(1,dict(avg=0,min=0,max=0)))

    def test_week_edges_join_readings_just_outside(self):
        start,end=stamp('2026-09-27T15:00:00Z'),stamp('2026-10-04T15:00:00Z')
        with patch('llm_usage.store.time.time',return_value=end+3600),self.store.connect() as c:
            # A reset in the last minutes of the week, confirmed by a poll just after it.
            self.store.limit(c,'claude-code','five_hour',30,end-100,end-400,'claude-oauth')
            self.store.limit(c,'claude-code','five_hour',100,end-100+18000,end+150,'claude-oauth')
            # 0% across the start of the week counts only from the start.
            self.store.limit(c,'claude-code','seven_day',0,end+86400,start-200,'claude-oauth')
            self.store.limit(c,'claude-code','seven_day',0,end+86400,start+100,'claude-oauth')
            # An exhaustion counted from a reading long before the week, with no interval inside it.
            self.store.limit(c,'codex','codex · 300분',50,end+86400,start-3600,'codex')
            self.store.limit(c,'codex','codex · 300분',0,end+86400,start+100,'codex')
        report=reports.build(self.store,stamp('2026-10-05T01:00:00Z'),PRICING)
        use={(q['route'],q['bucket']):q for q in report['quota_use']}
        five=use[('claude-code','five_hour')]
        self.assertEqual((five['resets_observed'],five['before_reset'],five['observed_hours']),(1,dict(avg=30,min=30,max=30),0.11))
        self.assertEqual((use[('claude-code','seven_day')]['zero_hours'],use[('claude-code','seven_day')]['observed_hours']),(0.03,0.03))
        kept=use[('codex','codex · 300분')]
        self.assertEqual((kept['exhausted'],kept['observed_hours'],kept['before_reset']),(1,0,None))
        # The next week does not count the same reset again.
        with self.store.connect() as c:later=reports.quota_use(c,end,end+7*86400)
        self.assertFalse(any(q['resets_observed'] for q in later))

    def test_longest_observed_stream_is_used(self):
        start=stamp('2026-09-29T00:00:00Z')
        with patch('llm_usage.store.time.time',return_value=NOW),self.store.connect() as c:
            # The account poll stops after two readings; the status line goes on to exhaust the window.
            for i,remaining in enumerate((80,78)):
                self.store.limit(c,'claude-code','seven_day',remaining,start+6*86400,start+i*300,'claude-oauth')
            for i,remaining in enumerate((40,20,5,0,0)):
                self.store.limit(c,'claude-code','seven_day',remaining,start+6*86400,start+86400+i*300,'claude-code')
        report=reports.build(self.store,stamp('2026-10-05T01:00:00Z'),PRICING)
        use=next(q for q in report['quota_use'] if (q['route'],q['bucket'])==('claude-code','seven_day'))
        self.assertEqual((use['exhausted'],use['observed_hours'],use['zero_hours']),(1,0.33,0.08))

    def test_fetch_failure_between_readings_is_not_observed(self):
        start=stamp('2026-09-29T00:00:00Z')
        with patch('llm_usage.store.time.time',return_value=NOW),self.store.connect() as c:
            for i in range(3):self.store.limit(c,'claude-code','five_hour',0,start+18000,start+i*300,'claude-oauth')
            # The same failure also falls between the readings around a window reset.
            for i,(remaining,resets) in enumerate(((0,start+400),(0,start+400),(100,start+400+604800))):
                self.store.limit(c,'claude-code','seven_day',remaining,resets,start+i*300,'claude-oauth')
            # The poll between the last two readings failed: that interval is not observed 0% time.
            self.store.source(c,'claude-oauth','error','timeout',start+450)
        report=reports.build(self.store,stamp('2026-10-05T01:00:00Z'),PRICING)
        use={q['bucket']:q for q in report['quota_use'] if q['route']=='claude-code'}
        self.assertEqual((use['five_hour']['observed_hours'],use['five_hour']['zero_hours']),(0.08,0.08))
        self.assertEqual((use['seven_day']['observed_hours'],use['seven_day']['zero_hours']),(0.08,0.08))
        # The reset itself was still seen across the two readings.
        self.assertEqual(use['seven_day']['before_reset'],dict(avg=0,min=0,max=0))

    def test_consecutive_exhaustion_weeks(self):
        with patch('llm_usage.store.time.time',return_value=NOW),self.store.connect() as c:
            for i,remaining in enumerate((5,0)):
                self.store.limit(c,'codex','codex · 10080분',remaining,NOW+86400,stamp('2026-09-23T00:00:00Z')+i*600,'codex')
        reports.ensure_report(self.store,stamp('2026-10-05T01:00:00Z'),PRICING)
        with self.store.connect() as c:report=self.store.state(c,'report:2026-09-28')
        self.assertEqual(next(q for q in report['quota_use'] if q['route']=='codex')['streak'],2)
        self.assertIn('Codex codex · 10080분 2회 (2주 연속)',reports.summary(report))

    def test_rebuilt_old_week_keeps_quota_figures(self):
        later=stamp('2026-10-05T01:00:00Z')
        reports.ensure_report(self.store,later,PRICING)
        with self.store.connect() as c:before=self.store.state(c,'report:2026-09-28')
        self.assertEqual(before['exhausted'][0]['count'],2)
        # Weeks later the quota history of that week is pruned; a pricing change rebuilds every report.
        month=later+40*86400
        with patch('llm_usage.store.time.time',return_value=month),self.store.connect() as c:self.store.prune_history(c)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM limit_history').fetchone()[0],0)
        reports.ensure_report(self.store,month,{**PRICING,'claude-x':dict(input=1,output=1)})
        with self.store.connect() as c:
            after=self.store.state(c,'report:2026-09-28');unseen=self.store.state(c,'report:2026-10-05')
        self.assertNotEqual(after['built'],before['built'])
        self.assertEqual((after['exhausted'],after['quota_use']),(before['exhausted'],before['quota_use']))
        # A week first built after its history was pruned says so instead of reporting zeros.
        self.assertEqual((unseen['exhausted'],unseen['quota_use']),(None,None))
        self.assertNotIn('한도 소진',reports.summary(unseen))

    def test_longer_retention_after_pruning_keeps_quota_figures(self):
        later=stamp('2026-10-05T01:00:00Z')
        reports.ensure_report(self.store,later,PRICING)
        with self.store.connect() as c:before=self.store.state(c,'report:2026-09-28')
        month=later+40*86400
        with patch('llm_usage.store.time.time',return_value=month),self.store.connect() as c:self.store.prune_history(c)
        # Retention is raised after the prune: the deleted history of that week does not come back.
        wider=Store(Path(self.tmp.name)/'usage.db',thresholds={'retention_days':90})
        reports.ensure_report(wider,month,{**PRICING,'claude-x':dict(input=1,output=1)})
        with wider.connect() as c:after=wider.state(c,'report:2026-09-28')
        self.assertNotEqual(after['built'],before['built'])
        self.assertEqual((after['exhausted'],after['quota_use']),(before['exhausted'],before['quota_use']))

    def test_report_is_built_once_and_old_ones_are_pruned(self):
        later=stamp('2026-10-05T01:00:00Z')
        self.assertIsNotNone(reports.ensure_report(self.store,later,PRICING))
        self.assertIsNone(reports.ensure_report(self.store,later,PRICING))
        with self.store.connect() as c:
            for week in range(20):self.store.save_state(c,f'report:2025-01-{week+1:02d}',dict(start=str(week)))
        reports.ensure_report(self.store,stamp('2026-10-12T01:00:00Z'),PRICING)
        kept=reports.recent(self.store,limit=100)
        self.assertEqual(len(kept),reports.KEEP)
        self.assertEqual(kept[0]['start'],'2026-10-05')


class BudgetWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'config.json').write_text('{}')
        self.app=create_app(dict(database=str(self.root/'usage.db'),origin='https://pc.example',secret_key='t',allowed_logins=['owner'],
                                 config_file=str(self.root/'config.json')))
        self.client=self.app.test_client()
        self.env={'REMOTE_ADDR':'','gunicorn.socket':SimpleNamespace(family=socket.AF_UNIX)}
        self.headers={'Tailscale-User-Login':'owner'}

    def get(self,path):return self.client.get(path,base_url='https://pc.example',headers=self.headers,environ_overrides=self.env)

    def post(self,path,body):
        token=self.get('/api/bootstrap').json['csrf']
        return self.client.post(path,json=body,base_url='https://pc.example',environ_overrides=self.env,
                                headers={**self.headers,'Origin':'https://pc.example','X-CSRF-Token':token})

    def test_budget_validation_storage_and_usage_payload(self):
        r=self.post('/api/config/project-budgets',{'budgets':{'a':{'tokens':2_000_000,'usd':None},'b':{'tokens':None,'usd':5},'c':{'tokens':None,'usd':None}}})
        self.assertEqual(r.get_json()['project_budgets'],{'a':{'tokens':2_000_000,'usd':None},'b':{'tokens':None,'usd':5}})
        self.assertEqual(json.loads((self.root/'local.json').read_text())['project_budgets']['b']['usd'],5)
        for bad in ({'a':{'tokens':-1}},{'a':{'tokens':1.5}},{'a':{'usd':0}},{'':{'tokens':1}},[]):
            self.assertEqual(self.post('/api/config/project-budgets',{'budgets':bad}).status_code,400,bad)
        usage=self.get('/api/usage?period=7d').get_json()
        self.assertEqual(usage['project_budgets']['a']['tokens'],2_000_000)
        self.assertIn('projects',usage['project_month'])
        self.assertEqual(self.get('/api/reports').get_json(),{'reports':[]})


if __name__=='__main__':unittest.main()

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

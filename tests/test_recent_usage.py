"""Rolling-hour boundaries use original records, including across KST midnight."""
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_usage.insights import token_total
from llm_usage.store import Store, stamp
from llm_usage.webapp import create_app


class RecentUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'usage.db';self.store=Store(self.path)
        self.now=stamp('2026-10-11T00:20:00+09:00')
        with self.store.connect() as c:
            # Excluded events deliberately share hourly rollups with included ones.
            for key,offset,value,route,project,session in (
                ('before',-3601,10000,'codex','outside','old'),
                ('start',-3600,10,'codex','alpha','one'),
                ('middle',-1200,20,'claude-code','beta','two'),
                ('last',-1,30,'antigravity',None,None),
                ('end',0,10000,'codex','outside','new'),
                ('future',60,10000,'codex','outside','future')):
                self.store.event(c,key,self.now+offset,'fixture',route,'m',
                                 dict(uncached_input=value,cached_input=value,output=value,
                                      cache_creation=value,reasoning=value),project=project,session=session)
            self.store.save_state(c,'last_local',self.now-1)

    def test_exact_hour_totals_models_projects_and_series_agree(self):
        data=self.store.usage(period='1h',granularity='hour',now=self.now)
        self.assertEqual(stamp(data['start']),self.now-3600)
        self.assertEqual(stamp(data['end_exclusive']),self.now)
        self.assertEqual(data['totals']['requests'],3)
        self.assertEqual(token_total(data['totals']),240)  # reasoning is already in output
        self.assertEqual(sum(token_total(r) for r in data['rows']),240)
        self.assertEqual(sum(token_total(r) for r in data['projects']),240)
        self.assertEqual(sum(token_total(v) for p in data['series'] for v in p['values'].values()),240)
        self.assertEqual(len(data['series']),2)
        self.assertTrue(all(p['partial'] for p in data['series']))
        self.assertEqual(data['sessions_total'],2)
        self.assertEqual(data['sessionless_requests'],1)
        self.assertEqual({r['project'] for r in data['projects']},{'alpha','beta','미분류'})

    def test_scope_pricing_comparison_and_raw_store_equivalence(self):
        args=dict(period='1h',granularity='hour',group='project',now=self.now,
                  scope='route:codex',pricing={'m':dict(input=1,cached=1,output=1,cache_write=1)},
                  subscriptions={'codex':20},compare='previous')
        result=self.store.usage(**args)
        self.assertEqual(token_total(result['totals']),40)
        self.assertAlmostEqual(result['cost']['period'],40/1e6)
        self.assertEqual(token_total(result['compare']['elapsed']),40000)
        self.assertEqual(result['subscriptions'],[])  # no monthly extrapolation of one hour
        raw=Store(self.path,rollup=False).usage(**args)
        for key in ('totals','rows','series','projects','sessions','compare','cost'):
            self.assertEqual(result[key],raw[key],key)

    def test_clock_expiry_and_pinned_lazy_details(self):
        core=self.store.usage(period='1h',now=self.now,sections='core')
        detail=self.store.usage(period='1h',now=self.now+20,as_of=self.now,sections='insights')
        self.assertEqual(detail['start'],core['start'])
        self.assertEqual(detail['end_exclusive'],core['end_exclusive'])
        self.assertEqual(sum(token_total(r) for r in detail['projects']),token_total(core['totals']))
        later=self.store.usage(period='1h',now=self.now+7200)
        self.assertEqual(later['totals']['requests'],0)
        self.assertEqual(later['projects'],[])

    def test_invalid_anchor_is_rejected(self):
        for anchor in (True,float('nan'),float('inf'),-1,0,self.now+1,'bad'):
            with self.subTest(anchor=anchor),self.assertRaises(ValueError):
                self.store.usage(period='1h',now=self.now,as_of=anchor)
        with self.assertRaises(ValueError):
            self.store.usage(period='today',now=self.now,as_of=self.now)

    def test_observed_zero_baseline_keeps_absolute_change(self):
        with self.store.connect() as c:
            self.store.event(c,'earlier-observation',self.now-10800,'fixture','antigravity','m',dict(output=1))
        comparison=self.store.usage(period='1h',scope='route:antigravity',now=self.now)['insights']['comparison']
        self.assertEqual(comparison['status'],'observed')
        self.assertEqual((comparison['current'],comparison['previous'],comparison['delta']),(120,0,120))
        self.assertIsNone(comparison['percent'])

    def test_http_core_and_details_use_the_same_range(self):
        app=create_app(dict(database=str(self.path),origin='https://fixture.test',
                            secret_key='fixture-key',allowed_logins=['fixture']))
        client=app.test_client()
        def get(query):
            return client.get('/api/usage?'+query,headers={'Host':'fixture.test','Tailscale-User-Login':'fixture'},
                              environ_base={'gunicorn.socket':type('Sock',(),{'family':socket.AF_UNIX})()})
        with patch('llm_usage.store.time.time',return_value=self.now):
            core=get('period=1h&granularity=hour&sections=core').get_json()
        with patch('llm_usage.store.time.time',return_value=self.now+20):
            detail=get('period=1h&granularity=hour&sections=insights&as_of='+str(self.now)).get_json()
            self.assertEqual(core['end_exclusive'],detail['end_exclusive'])
            self.assertEqual(sum(token_total(r) for r in detail['projects']),token_total(core['totals']))
            for query in ('period=1h&as_of=nan','period=1h&as_of=inf','period=1h&as_of=bad','period=today&as_of='+str(self.now)):
                self.assertEqual(get(query).status_code,400,query)


if __name__=='__main__':
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

from llm_usage.insights import quota_forecast, window_seconds
from llm_usage.pricing import BUILTIN, edited_rates, entry_key, estimate, load_pricing, pricing_origins, rate_for, upcoming_for
from llm_usage.store import Store, stamp


class WindowSecondsTests(unittest.TestCase):
    def test_known_buckets(self):
        self.assertEqual(window_seconds('five_hour'),18000)
        self.assertEqual(window_seconds('rolling'),18000)
        self.assertEqual(window_seconds('seven_day_opus'),604800)
        self.assertEqual(window_seconds('gemini-5h'),18000)
        self.assertEqual(window_seconds('3p-weekly'),604800)
        self.assertEqual(window_seconds('daily'),86400)
        self.assertEqual(window_seconds('weekly'),604800)
        self.assertEqual(window_seconds('monthly'),2592000)
        self.assertEqual(window_seconds('codex · 300분'),18000)
        self.assertEqual(window_seconds('codex_bengalfox · 10080분'),604800)

    def test_unknown_or_missing_names_return_none(self):
        for name in (None,'','미제공','custom-bucket','x · 날짜'):
            self.assertIsNone(window_seconds(name))


class ForecastTests(unittest.TestCase):
    def test_depleting_pace_flags_within_window(self):
        now=1000.0
        row=dict(status='fresh',pace_per_hour=5.0,pace_minutes=30,remaining=10.0,resets=now+4*3600)
        forecast=quota_forecast(row,now)
        self.assertTrue(forecast['within_window'])
        self.assertEqual(forecast['depletes_at'],now+2*3600)
        self.assertEqual(forecast['projected_remaining'],0)

    def test_spare_pace_projects_remaining(self):
        now=1000.0
        row=dict(status='fresh',pace_per_hour=1.0,pace_minutes=60,remaining=50.0,resets=now+10*3600)
        forecast=quota_forecast(row,now)
        self.assertFalse(forecast['within_window'])
        self.assertAlmostEqual(forecast['projected_remaining'],40.0)

    def test_no_forecast_without_fresh_pace_or_reset(self):
        now=1000.0
        self.assertIsNone(quota_forecast(dict(status='stale',pace_per_hour=5,pace_minutes=60,remaining=10,resets=now+3600),now))
        self.assertIsNone(quota_forecast(dict(status='fresh',pace_per_hour=None,pace_minutes=60,remaining=10,resets=now+3600),now))
        self.assertIsNone(quota_forecast(dict(status='fresh',pace_per_hour=5,pace_minutes=60,remaining=None,resets=now+3600),now))
        self.assertIsNone(quota_forecast(dict(status='fresh',pace_per_hour=5,pace_minutes=60,remaining=10,resets=None),now))
        self.assertIsNone(quota_forecast(dict(status='fresh',pace_per_hour=5,pace_minutes=60,remaining=10,resets=now-1),now))
        # A pace derived from less than fifteen minutes of observation is
        # quantization noise; do not forecast from it.
        self.assertIsNone(quota_forecast(dict(status='fresh',pace_per_hour=5,pace_minutes=14,remaining=10,resets=now+3600),now))
        self.assertIsNone(quota_forecast(dict(status='fresh',pace_per_hour=5,remaining=10,resets=now+3600),now))

    def test_zero_pace_never_depletes(self):
        forecast=quota_forecast(dict(status='fresh',pace_per_hour=0.0,pace_minutes=60,remaining=50.0,resets=4000.0),1000.0)
        self.assertFalse(forecast['within_window'])
        self.assertIsNone(forecast['depletes_at'])
        self.assertEqual(forecast['projected_remaining'],50.0)


class PricingTests(unittest.TestCase):
    def test_exact_and_snapshot_prefix_match(self):
        table={'gpt-x':dict(input=2,output=8)}
        self.assertEqual(rate_for('gpt-x',table),table['gpt-x'])
        self.assertEqual(rate_for('gpt-x-2026-01-01',table),table['gpt-x'])
        self.assertIsNone(rate_for('gpt-x2',table))
        self.assertIsNone(rate_for('unknown-model',table))
        self.assertIsNone(rate_for(None,table))

    def test_longest_prefix_wins(self):
        table={'gpt':dict(input=1,output=1),'gpt-x':dict(input=2,output=2)}
        self.assertEqual(rate_for('gpt-x-preview',table)['input'],2)

    def test_newer_model_version_is_not_priced_as_older_snapshot(self):
        table={'claude-opus-4':dict(input=15,output=75),'gpt-x':dict(input=2,output=8)}
        self.assertEqual(rate_for('claude-opus-4-20250514',table)['input'],15)
        self.assertIsNone(rate_for('claude-opus-4-5-20251101',table))
        self.assertIsNone(rate_for('claude-opus-4-6',table))
        for suffix in ('5.1','5beta','20250514beta'):
            self.assertIsNone(rate_for('claude-opus-4-'+suffix,table))
        self.assertEqual(rate_for('gpt-x-2026-01-01',table)['input'],2)
        self.assertEqual(rate_for('gpt-x-preview',table)['input'],2)
        self.assertEqual(rate_for('gpt-x-low',table)['input'],2)

    def test_estimate_uses_each_dimension_and_cache_fallback(self):
        rates=dict(input=1,cached=0.1,output=5,cache_write=1.25)
        value=estimate(dict(uncached_input=1_000_000,cached_input=1_000_000,output=1_000_000,cache_creation=1_000_000),rates)
        self.assertAlmostEqual(value,1+0.1+5+1.25)
        no_write=dict(input=1,output=5)
        self.assertAlmostEqual(estimate(dict(cache_creation=1_000_000),no_write),1.0)

    def test_edited_dated_series_takes_effect_from_today_only(self):
        series=[dict(since='2026-06-26',input=5,cached=0.5,output=30),dict(since='2026-08-22',input=4,cached=0.4,output=20)]
        edited=edited_rates(series,dict(output=18),'2026-09-26')
        self.assertEqual(rate_for('m',{'m':edited},'2026-07-01')['output'],30)
        self.assertEqual(rate_for('m',{'m':edited},'2026-09-01')['output'],20)
        self.assertEqual(rate_for('m',{'m':edited},'2026-09-26'),dict(since='2026-09-26',input=4,cached=0.4,output=18))
        self.assertEqual(edited_rates(None,dict(input=1,output=2),'2026-09-26'),dict(input=1,output=2))
        self.assertEqual(edited_rates(dict(input=1,output=2,cached=0.1),dict(output=3),'2026-09-26'),dict(input=1,output=3,cached=0.1))

    def test_load_pricing_merges_valid_file_only(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        path=Path(tmp.name)/'pricing.json'
        path.write_text(json.dumps({'my-model':dict(input=9,output=9),'broken':dict(input='x')}))
        table=load_pricing({'pricing_file':str(path)})
        self.assertEqual(table['my-model']['input'],9)
        self.assertNotIn('broken',table)
        path.write_text('{')
        self.assertIn('claude-opus-4',load_pricing({'pricing_file':str(path)}))

    def test_dated_rates_load_sorted_and_skip_invalid(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        path=Path(tmp.name)/'pricing.json'
        path.write_text(json.dumps({'m':[dict(since='2026-09-01',input=2,output=2),
                                       dict(since='2026-08-01',input=1,output=1),
                                       dict(input='x')]}))
        table=load_pricing({'pricing_file':str(path)})
        self.assertEqual([e['since'] for e in table['m']],['2026-08-01','2026-09-01'])

    def test_rate_for_day_boundaries(self):
        table={'m':[dict(since='2026-08-01',input=1,output=1),
                   dict(since='2026-09-01',input=2,output=2)]}
        self.assertEqual(rate_for('m',table)['input'],2)             # latest
        self.assertEqual(rate_for('m',table,'2026-07-01')['input'],1) # before first: earliest known
        self.assertEqual(rate_for('m',table,'2026-08-31')['input'],1)
        self.assertEqual(rate_for('m',table,'2026-09-01')['input'],2) # boundary day
        self.assertEqual(rate_for('m',table,'2026-10-01')['input'],2)
        self.assertIsNone(upcoming_for('m',table,'2026-09-02'))
        self.assertEqual(upcoming_for('m',table,'2026-08-15')['since'],'2026-09-01')
        self.assertIsNone(upcoming_for('flat',{'flat':dict(input=1,output=1)},'2026-08-15'))

    def test_origins_builtin_marker_and_entry_key(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        empty=str(Path(tmp.name)/'none.json')
        s={'pricing_file':empty,
           'pricing':{'x-model':dict(input=1,output=2),'gone':None,'gpt-5':'builtin'}}
        table=load_pricing(s)
        self.assertEqual(table['x-model']['input'],1)
        self.assertNotIn('gone',table)
        self.assertEqual(table['gpt-5'],BUILTIN['gpt-5'])
        origins=pricing_origins(s)
        self.assertEqual(origins['x-model'],'user')
        self.assertEqual(origins['gone'],'hidden')
        self.assertEqual(origins['gpt-5'],'builtin')
        self.assertEqual(origins['claude-opus-4'],'builtin')
        self.assertEqual(entry_key('gpt-5.6-sol-2026-07-01',table),'gpt-5.6-sol')
        self.assertEqual(entry_key('swe-2 (max)',{'swe-2':dict(input=1,output=1)}),'swe-2')
        self.assertIsNone(entry_key('unpriced-model',table))


class InsightStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'usage.db')

    def test_usage_requests_heatmap_and_estimated_cost(self):
        with self.store.connect() as c:
            # 2026-09-08 23:00 KST → Tuesday 14:00 UTC
            self.store.event(c,'a',stamp('2026-09-08T14:00:00Z'),'OpenAI','codex','priced-model',
                             dict(uncached_input=1_000_000,output=500_000))
            self.store.event(c,'b',stamp('2026-09-08T14:30:00Z'),'OpenAI','codex','unpriced-model',
                             dict(uncached_input=2_000_000))
        pricing={'priced-model':dict(input=1,cached=0.1,output=5,cache_write=1.25)}
        data=self.store.usage(period='all',pricing=pricing)
        self.assertEqual(data['totals']['requests'],2)
        row=next(r for r in data['rows'] if r['model']=='priced-model')
        self.assertEqual(row['requests'],1)
        self.assertAlmostEqual(row['est_cost'],1*1+0.5*5)
        self.assertAlmostEqual(data['cost']['period'],3.5)
        self.assertAlmostEqual(data['cost']['coverage'],1.5/3.5*100)
        self.assertAlmostEqual(data['cost']['lifetime'],3.5)
        self.assertEqual([c for c in data['heatmap'] if c['tokens']>0],
                         [dict(dow=2,hour=23,tokens=3_500_000,requests=2,cost=3.5)])
        without=self.store.usage(period='all')
        self.assertIsNone(without['cost']['period'])
        self.assertNotIn('est_cost',without['rows'][0])
        self.assertTrue(all(c['cost'] is None for c in without['heatmap']))

    def test_dated_pricing_splits_cost_by_event_day(self):
        with self.store.connect() as c:
            self.store.event(c,'a',stamp('2026-08-20T00:00:00Z'),'P','r','dated-model',
                             dict(uncached_input=1_000_000))
            self.store.event(c,'b',stamp('2026-08-25T00:00:00Z'),'P','r','dated-model',
                             dict(uncached_input=1_000_000))
            self.store.event(c,'sess',stamp('2026-08-25T00:00:00Z'),'P','r','dated-model',
                             dict(uncached_input=1_000_000),session='s1')
        pricing={'dated-model':[dict(since='2026-08-01',input=5,output=30),
                              dict(since='2026-08-22',input=4,output=20)]}
        data=self.store.usage(period='all',pricing=pricing)
        row=next(r for r in data['rows'] if r['model']=='dated-model')
        self.assertAlmostEqual(row['est_cost'],5+4+4)
        self.assertAlmostEqual(data['cost']['period'],13.0)
        self.assertAlmostEqual(data['cost']['lifetime'],13.0)
        sess=next(s for s in data['sessions'] if s['session']=='s1')
        self.assertAlmostEqual(sess['cost'],4.0)

    def test_period_free_totals_follow_event_changes(self):
        now=stamp('2026-09-08T03:00:00Z')
        pricing={'m':dict(input=1,output=1)}
        with self.store.connect() as c:
            self.store.event(c,'a',now-3600,'P','r','unpriced',dict(uncached_input=1_000_000))
        first=self.store.usage(period='today',pricing=pricing,now=now)
        self.assertIsNone(first['cost']['lifetime'])
        with self.store.connect() as c:
            self.store.event(c,'b',now-1800,'P','r','m',dict(uncached_input=1_000_000))
        second=self.store.usage(period='today',pricing=pricing,now=now)
        self.assertAlmostEqual(second['cost']['lifetime'],1.0)
        self.assertEqual(second['calendar'][-1]['requests'],2)
        # A relabel keeps every sum; the collection pass that makes it bumps last_local.
        with self.store.connect() as c:
            self.store.event(c,'a',now-3600,'P','r','m',dict(uncached_input=1_000_000))
            self.store.save_state(c,'last_local',now)
        self.assertAlmostEqual(self.store.usage(period='today',pricing=pricing,now=now)['cost']['lifetime'],2.0)
        self.assertAlmostEqual(self.store.usage(period='today',pricing={'m':dict(input=3,output=1)},now=now)['cost']['lifetime'],6.0)

    def test_compare_series_aligns_with_current_buckets(self):
        now=stamp('2026-09-09T03:00:00Z')
        with self.store.connect() as c:
            self.store.event(c,'cur',stamp('2026-09-05T12:00:00+09:00'),'OpenAI','codex','gpt-test',dict(uncached_input=100))
            self.store.event(c,'prev',stamp('2026-08-29T12:00:00+09:00'),'OpenAI','codex','gpt-test',dict(uncached_input=7))
            self.store.event(c,'boundary',stamp('2026-08-31T12:00:00+09:00'),'OpenAI','codex','gpt-test',dict(uncached_input=3))
        # The ghost series must always have the same bucket count as the main
        # series, including week/month grids whose spans differ after a shift.
        for period,grain,mode in [('7d','day','week'),('7d','day','previous'),
                                  ('30d','week','month'),('30d','month','month'),('30d','day','previous')]:
            result=self.store.usage(period=period,granularity=grain,group='route',compare=mode,now=now)
            self.assertEqual(len(result['compare']['series']),len(result['series']),(period,grain,mode))
        # The Aug-29 event shifts +7d into the Sep-05 bucket.
        result=self.store.usage(period='7d',granularity='day',group='route',compare='week',now=now)
        idx=[i for i,p in enumerate(result['series']) if p['time'].startswith('2026-09-05')][0]
        self.assertEqual(result['compare']['series'][idx]['values']['codex']['uncached_input'],7)
        self.assertTrue(result['compare']['series'][idx]['compared_time'].startswith('2026-08-29'))

    def test_limit_window_counts_route_events(self):
        now=stamp('2026-09-08T10:00:00Z')
        with self.store.connect() as c:
            self.store.event(c,'in',now-3600,'Anthropic','claude-code','claude-test',dict(output=100))
            self.store.event(c,'out',now-6*3600,'Anthropic','claude-code','claude-test',dict(output=200))
            self.store.limit(c,'claude-code','five_hour',42,now+3600,now,'claude-code')
        row=next(r for r in self.store.limits(now)['limits'] if r['route']=='claude-code')
        self.assertEqual(row['window']['requests'],1)
        self.assertEqual(row['window']['tokens'],100)
        self.assertTrue(row['window']['open'])
        # Codex "model · N분" buckets sum the route's events.
        with self.store.connect() as c:
            self.store.event(c,'cx',now-60,'OpenAI','codex','gpt-test',dict(output=7))
            self.store.limit(c,'codex','codex_bengalfox · 300분',50,now+60,now,'codex')
        row=next(r for r in self.store.limits(now)['limits'] if 'bengalfox' in r['bucket'])
        self.assertEqual(row['window']['tokens'],7)
        # Buckets without a known window or reset get no window.
        with self.store.connect() as c:
            self.store.limit(c,'opencode-go','미제공',None,None,now,'opencode-go')
        row=next(r for r in self.store.limits(now)['limits'] if r['route']=='opencode-go')
        self.assertIsNone(row['window'])

    def test_ended_source_stays_ended(self):
        now=stamp('2026-09-08T10:00:00Z')
        with self.store.connect() as c:
            self.store.limit(c,'opencode-go','rolling',40,now+3600,now,'opencode-go')
            self.store.source(c,'opencode-go','ended','계정에 구독이 없습니다.')
        row=next(r for r in self.store.limits(now)['limits'] if r['route']=='opencode-go')
        # An ended subscription must not fall back to fresh/stale and resurface the card.
        self.assertEqual(row['status'],'ended')
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM quota_interruptions').fetchone()[0],0)

    def test_closed_window_still_reports(self):
        now=stamp('2026-09-08T10:00:00Z')
        with self.store.connect() as c:
            self.store.event(c,'e',now-7200,'Devin','devin','swe-test',dict(output=55))
            self.store.limit(c,'devin','daily',10,now-3600,now-1800,'devin')
        row=next(r for r in self.store.limits(now)['limits'] if r['route']=='devin')
        self.assertFalse(row['window']['open'])
        self.assertEqual(row['window']['tokens'],55)


    def test_shared_model_cost_stays_on_its_own_route(self):
        with self.store.connect() as c:
            self.store.event(c,'cc',stamp('2026-09-08T01:00:00Z'),'Anthropic','claude-code','shared-model',
                             dict(uncached_input=1_000_000))
            self.store.event(c,'oc',stamp('2026-09-08T02:00:00Z'),'Anthropic','opencode','shared-model',
                             dict(uncached_input=2_000_000))
        pricing={'shared-model':dict(input=3,output=15)}
        data=self.store.usage(period='all',pricing=pricing,subscriptions={'claude-code':20,'opencode':10})
        costs={r['route']:r['est_cost'] for r in data['rows']}
        self.assertAlmostEqual(costs['claude-code'],3.0)
        self.assertAlmostEqual(costs['opencode'],6.0)
        self.assertAlmostEqual(sum(costs.values()),data['cost']['period'])
        subs={s['route']:s['period_cost'] for s in data['subscriptions']}
        self.assertAlmostEqual(subs['claude-code'],3.0)
        self.assertAlmostEqual(subs['opencode'],6.0)

    def test_subscriptions_without_pricing_report_no_estimate(self):
        with self.store.connect() as c:
            self.store.event(c,'e',stamp('2026-09-08T01:00:00Z'),'OpenAI','codex','gpt-test',dict(output=5))
        data=self.store.usage(period='all',subscriptions={'codex':20})
        self.assertEqual(data['subscriptions'],[dict(route='codex',monthly_usd=20,period_cost=None,monthly_equiv=None)])

if __name__=='__main__':unittest.main()

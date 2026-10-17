import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from llm_usage import monthly
from llm_usage.store import Store, stamp
from llm_usage.webapp import create_app

NOW=stamp('2026-10-01T00:30:00Z')  # Thursday 10-01 09:30 KST: the first pass in October
PRICING={'gpt-test':dict(input=1,cached=0.1,output=10,cache_write=1)}
PRICES={'codex':200,'claude-code':100}


class MonthlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'usage.db')
        # Quota readings prune the history older than retention_days from the clock.
        with patch('llm_usage.store.time.time',return_value=NOW),self.store.connect() as c:
            # Records begin in July (07-10); August and September are whole months.
            self.store.event(c,'jul',stamp('2026-07-10T01:00:00Z'),'OpenAI','codex','gpt-test',dict(uncached_input=500_000),project='alpha')
            self.store.event(c,'aug',stamp('2026-08-20T01:00:00Z'),'OpenAI','codex','gpt-test',dict(uncached_input=300_000),project='alpha')
            self.store.event(c,'s1',stamp('2026-09-10T01:00:00Z'),'OpenAI','codex','gpt-test',dict(uncached_input=1_000_000),project='alpha')
            self.store.event(c,'s2',stamp('2026-09-12T01:00:00Z'),'Anthropic','claude-code','claude-x',dict(output=2000),project='beta')
            self.store.event(c,'oct',NOW-600,'OpenAI','codex','gpt-test',dict(uncached_input=7_000_000),project='alpha')
            # A Codex account day with no local record: a residual of 5M tokens and no cost.
            self.store.account_tokens(c,'codex',[dict(startDate='2026-09-15',tokens=5_000_000)],NOW)
            # 20 → 0 (exhausted) → 0 → 100 inside September.
            for i,remaining in enumerate((20,0,0,100)):
                self.store.limit(c,'codex','codex · 10080분',remaining,stamp('2026-09-27T00:00:00Z'),stamp('2026-09-20T00:00:00Z')+i*600,'codex')

    def month(self,key):
        with self.store.connect() as c:return self.store.state(c,'month:'+key)

    def test_month_arithmetic_is_kst_calendar_months(self):
        start=monthly.month_start(stamp('2026-09-30T15:30:00Z'))  # 10-01 00:30 KST
        self.assertEqual(start.isoformat(),'2026-10-01T00:00:00+09:00')
        self.assertEqual(monthly.shift(start,-10).isoformat(),'2025-12-01T00:00:00+09:00')
        self.assertEqual(monthly.shift(start,3).isoformat(),'2027-01-01T00:00:00+09:00')

    def test_settlement_values_each_subscription_against_its_price(self):
        settled=monthly.build(self.store,monthly.shift(monthly.month_start(NOW),-1),NOW,PRICING,PRICES,{})
        self.assertEqual((settled['month'],settled['start'],settled['end']),('2026-09','2026-09-01','2026-09-30'))
        self.assertFalse(settled['partial'])
        subs={s['route']:s for s in settled['subscriptions']}
        # Codex tokens include the account residual; its value is the records' list price only.
        self.assertEqual((subs['codex']['tokens'],subs['codex']['value'],subs['codex']['monthly_usd']),(6_000_000,1.0,200))
        self.assertAlmostEqual(subs['codex']['ratio'],1/200)
        self.assertEqual(settled['totals']['residual_tokens'],5_000_000)
        # Used without a list price: no value and no ratio rather than 0.
        self.assertEqual((subs['claude-code']['tokens'],subs['claude-code']['value'],subs['claude-code']['ratio']),(2000,None,None))
        self.assertEqual([m['model'] for m in settled['models']],['gpt-test','claude-x'])
        self.assertEqual([p['project'] for p in settled['projects']],['alpha','beta'])
        self.assertEqual(settled['residual'],dict(tokens=5_000_000,account_tokens=5_000_000,days=1))
        # The month's quota use from its history, with the exhaustion count.
        (row,)=settled['quota_use']
        self.assertEqual((row['exhausted'],row['zero_hours'],row['observed_hours']),(1,0.17,0.5))
        # Pruned to retention_days (30) before NOW: the month's first hours are gone.
        self.assertEqual(settled['quota_from'],stamp('2026-09-01T00:30:00Z'))
        self.assertIsNone(settled['accounts'])

    def test_first_month_is_partial_and_ended_subscriptions_leave_later_months(self):
        july=monthly.build(self.store,monthly.shift(monthly.month_start(NOW),-3),NOW,PRICING,PRICES,{})
        self.assertEqual((july['partial'],july['records_from']),(True,'2026-07-10'))
        # Ended in mid-August: settled for August, not for September.
        ended={'claude-code':{'since':stamp('2026-08-15T00:00:00Z')}}
        august=monthly.build(self.store,monthly.shift(monthly.month_start(NOW),-2),NOW,PRICING,PRICES,ended)
        september=monthly.build(self.store,monthly.shift(monthly.month_start(NOW),-1),NOW,PRICING,PRICES,ended)
        self.assertEqual({s['route']:s['monthly_usd'] for s in august['subscriptions']},{'codex':200,'claude-code':100})
        self.assertEqual({s['route']:s['monthly_usd'] for s in september['subscriptions']},{'codex':200})

    def test_months_are_settled_once_and_filled_back_to_the_first_record(self):
        self.assertEqual(monthly.ensure_months(self.store,NOW,PRICING,PRICES,{}),['2026-07','2026-08','2026-09'])
        self.assertEqual([m['month'] for m in monthly.recent(self.store)],['2026-09','2026-08','2026-07'])
        # Nothing new: nothing is rebuilt.
        self.assertEqual(monthly.ensure_months(self.store,NOW+300,PRICING,PRICES,{}),[])
        # This month's records do not touch finished months.
        with self.store.connect() as c:self.store.event(c,'oct2',NOW-60,'OpenAI','codex','gpt-test',dict(output=10))
        self.assertEqual(monthly.ensure_months(self.store,NOW+600,PRICING,PRICES,{}),[])

    def test_late_records_rebuild_the_last_two_months_with_the_first_prices(self):
        monthly.ensure_months(self.store,NOW,PRICING,PRICES,{})
        july=self.month('2026-07')
        with self.store.connect() as c:
            self.store.event(c,'late',stamp('2026-09-25T01:00:00Z'),'OpenAI','codex','gpt-test',dict(uncached_input=2_000_000))
        # The price changed since: the settlement keeps the price it was first built with.
        self.assertEqual(monthly.ensure_months(self.store,NOW+300,PRICING,{'codex':20},{}),['2026-08','2026-09'])
        codex=next(s for s in self.month('2026-09')['subscriptions'] if s['route']=='codex')
        self.assertEqual((codex['value'],codex['monthly_usd']),(3.0,200))
        self.assertEqual(self.month('2026-09')['built'],NOW+300)
        self.assertEqual(self.month('2026-07'),july)
        # A rate change is a change in the same way.
        rates={'gpt-test':dict(input=2,cached=0.1,output=10,cache_write=1)}
        self.assertEqual(monthly.ensure_months(self.store,NOW+600,rates,PRICES,{}),['2026-08','2026-09'])
        self.assertEqual(next(s for s in self.month('2026-09')['subscriptions'] if s['route']=='codex')['value'],6.0)

    def test_records_attributed_later_rebuild_the_last_two_months(self):
        monthly.ensure_months(self.store,NOW,PRICING,PRICES,{})
        # A record of September assigned to another project later: every token sum is kept.
        with self.store.connect() as c:
            c.execute("UPDATE events SET project='gamma' WHERE project='beta'");self.store.bump_data_version(c)
        self.assertEqual(monthly.ensure_months(self.store,NOW+300,PRICING,PRICES,{}),['2026-08','2026-09'])
        self.assertEqual([p['project'] for p in self.month('2026-09')['projects']],['alpha','gamma'])

    def test_a_month_is_settled_once_the_readings_past_its_end_are_in(self):
        # Readings up to 10 minutes past the month's end close its last interval.
        end=stamp('2026-09-30T15:00:00Z')  # 10-01 00:00 KST
        with patch('llm_usage.store.time.time',return_value=NOW),self.store.connect() as c:
            self.store.limit(c,'devin','weekly',0,end+86400,end-300,'devin')
        self.assertEqual(monthly.ensure_months(self.store,end+120,PRICING,PRICES,{}),['2026-07','2026-08'])
        with patch('llm_usage.store.time.time',return_value=NOW),self.store.connect() as c:
            self.store.limit(c,'devin','weekly',0,end+86400,end+240,'devin')
        self.assertEqual(monthly.ensure_months(self.store,end+600,PRICING,PRICES,{}),['2026-08','2026-09'])
        devin=next(q for q in self.month('2026-09')['quota_use'] if q['route']=='devin')
        self.assertEqual((devin['observed_hours'],devin['zero_hours']),(0.08,0.08))

    def test_quota_use_comes_from_the_history_kept_and_survives_pruning(self):
        with self.store.connect() as c:self.store.save_state(c,'history_cutoff',stamp('2026-09-05T00:00:00Z'))
        monthly.ensure_months(self.store,NOW,PRICING,PRICES,{})
        september=self.month('2026-09')
        self.assertEqual((september['quota_from'],september['quota_use'][0]['exhausted']),(stamp('2026-09-05T00:00:00Z'),1))
        # August lies wholly before the cutoff: no quota figures rather than zeros.
        self.assertEqual((self.month('2026-08')['quota_use'],self.month('2026-08')['quota_from']),(None,None))
        # Later pruning removes the exhaustion; a rebuild keeps what was computed then.
        with self.store.connect() as c:
            c.execute('DELETE FROM limit_history WHERE checked<?',(stamp('2026-09-25T00:00:00Z'),))
            self.store.save_state(c,'history_cutoff',stamp('2026-09-25T00:00:00Z'))
            self.store.event(c,'late',stamp('2026-09-25T01:00:00Z'),'OpenAI','codex','gpt-test',dict(output=1))
        self.assertIn('2026-09',monthly.ensure_months(self.store,NOW+300,PRICING,PRICES,{}))
        rebuilt=self.month('2026-09')
        self.assertEqual((rebuilt['quota_from'],rebuilt['quota_use']),(september['quota_from'],september['quota_use']))
        self.assertEqual(rebuilt['built'],NOW+300)

    def test_format_change_rebuilds_every_month_and_old_months_are_dropped(self):
        monthly.ensure_months(self.store,NOW,PRICING,PRICES,{})
        with patch.object(monthly,'MONTH_FORMAT',monthly.MONTH_FORMAT+1):
            self.assertEqual(monthly.ensure_months(self.store,NOW+300,PRICING,PRICES,{}),['2026-07','2026-08','2026-09'])
        with patch.object(monthly,'KEEP',2):
            monthly.ensure_months(self.store,NOW+600,PRICING,PRICES,{})
        self.assertEqual([m['month'] for m in monthly.recent(self.store)],['2026-09','2026-08'])


class MonthlyWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.app=create_app(dict(database=str(self.root/'usage.db'),origin='https://pc.example',secret_key='t',allowed_logins=['owner'],
                                 config_file=str(self.root/'config.json')))
        self.client=self.app.test_client()

    def test_reports_api_lists_settled_months(self):
        env={'REMOTE_ADDR':'','gunicorn.socket':SimpleNamespace(family=socket.AF_UNIX)}
        get=lambda:self.client.get('/api/reports',base_url='https://pc.example',headers={'Tailscale-User-Login':'owner'},environ_overrides=env).get_json()
        self.assertEqual(get()['months'],[])
        store=Store(self.root/'usage.db')
        with store.connect() as c:store.save_state(c,'month:2026-09',dict(month='2026-09'))
        self.assertEqual(get()['months'],[dict(month='2026-09')])
        json.dumps(get())


if __name__=='__main__':unittest.main()

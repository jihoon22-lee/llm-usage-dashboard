"""A changed subscription price values each span of use against the price paid then."""
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from types import SimpleNamespace

from llm_usage import monthly
from llm_usage.store import Store, stamp
from llm_usage.subscriptions import change, clean_history, segments, weighted
from llm_usage.webapp import create_app

DAY=86400
PRICING={'gpt-test':dict(input=1,output=1)}


class PriceHistoryTests(unittest.TestCase):
    def test_change_from_a_moment_keeps_earlier_prices(self):
        self.assertEqual(change([],220,110,'now',None,1000),[{'until':1000,'usd':220}])
        # A later change keeps the first; one between them replaces what follows it.
        two=change([{'until':1000,'usd':220}],110,150,'at',2000,3000)
        self.assertEqual(two,[{'until':1000,'usd':220},{'until':2000,'usd':110}])
        self.assertEqual(change(two,150,90,'at',500,3000),[{'until':500,'usd':220}])
        # Going back to an earlier price keeps the span between; a change at the moment the
        # earlier price ended merges both spans at that price.
        self.assertEqual(change([{'until':1000,'usd':220}],110,220,'now',None,2000),[{'until':1000,'usd':220},{'until':2000,'usd':110}])
        self.assertEqual(change([{'until':1000,'usd':110}],220,110,'at',1000,2000),[])
        self.assertEqual(change([{'until':500,'usd':90},{'until':1000,'usd':220}],110,220,'at',800,2000),[{'until':500,'usd':90}])

    def test_correction_all_time_unchanged_and_cleared_prices(self):
        history=[{'until':1000,'usd':220}]
        self.assertEqual(change(history,110,100,'correct',None,2000),history)
        self.assertEqual(change(history,110,100,'all',None,2000),[])
        # An unchanged price keeps its history in every mode, so one save of all rows
        # does not touch the subscriptions the user left alone.
        for mode in ('now','at','correct','all'):self.assertEqual(change(history,110,110,mode,1500,2000),history)
        self.assertEqual(change(history,110,None,'now',None,2000),[])
        # A first price has nothing earlier to keep.
        self.assertEqual(change([],None,20,'now',None,2000),[])

    def test_segments_and_weighted_price(self):
        history=[{'until':1000,'usd':220}]
        self.assertEqual(segments(110,history,0,2000),[(0,1000,220),(1000,2000,110)])
        self.assertEqual(segments(110,history,1500,2000),[(1500,2000,110)])
        self.assertEqual(segments(110,history,0,500),[(0,500,220)])
        self.assertEqual(segments(110,[],0,500),[(0,500,110)])
        self.assertEqual(segments(110,history,500,500),[])
        self.assertAlmostEqual(weighted([(0,3000,220),(3000,4000,110)]),192.5)
        self.assertIsNone(weighted([(0,1,None)]))

    def test_broken_history_entries_are_ignored(self):
        raw={'codex':[{'until':2000,'usd':110},{'until':1000,'usd':220},{'until':'x','usd':1},{'until':5,'usd':-1},
                      {'until':float('nan'),'usd':1},{'until':True,'usd':1},'bad'],'devin':'bad',7:[]}
        self.assertEqual(clean_history(raw),{'codex':[{'until':1000.0,'usd':220},{'until':2000.0,'usd':110}]})
        self.assertEqual(clean_history(None),{})


class PriceHistoryUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'usage.db')
        self.now=stamp('2026-10-25T13:00:00Z')  # 22:00 KST
        self.change=self.now-2*DAY
        with self.store.connect() as c:
            # 6 days at the old price, then 2 days at the new one: $6 and $2 of list value.
            for day in range(8):
                ts=self.now-(7.5-day)*DAY
                self.store.event(c,f'e{day}',ts,'OpenAI','codex','gpt-test',dict(uncached_input=1_000_000))

    def subscription(self,history,**kw):
        data=self.store.usage(period=kw.pop('period','all'),now=self.now,pricing=PRICING,subscriptions={'codex':110},
                              price_history=history,**kw)
        return data['subscriptions'][0],data

    def test_value_is_set_against_the_prices_of_the_span(self):
        plain,_=self.subscription(None)
        self.assertEqual(plain['effective_usd'],110)
        self.assertIsNone(plain['price_segments'])
        sub,data=self.subscription({'codex':[{'until':self.change,'usd':220}]})
        start=stamp(data['start']);end=self.now
        expected=(220*(self.change-start)+110*(end-self.change))/(end-start)
        self.assertAlmostEqual(sub['effective_usd'],expected)
        self.assertEqual(sub['monthly_usd'],110)
        self.assertAlmostEqual(sub['period_cost'],8)
        # Same value, larger price paid: the multiple is smaller than against $110 alone.
        self.assertLess(sub['monthly_equiv']/sub['effective_usd'],sub['monthly_equiv']/110)
        parts=sub['price_segments']
        self.assertEqual([p['monthly_usd'] for p in parts],[220,110])
        self.assertAlmostEqual(sum(p['cost'] for p in parts),sub['period_cost'])
        self.assertEqual([round(p['cost']) for p in parts],[6,2])
        self.assertEqual(sum(p['tokens'] for p in parts),sub['period_tokens'])
        self.assertAlmostEqual(parts[1]['days'],2)

    def test_a_change_before_the_period_leaves_one_price(self):
        sub,_=self.subscription({'codex':[{'until':self.now-40*DAY,'usd':220}]},period='7d')
        self.assertEqual(sub['effective_usd'],110)
        self.assertIsNone(sub['price_segments'])
        sub,_=self.subscription({'codex':[{'until':self.now+DAY,'usd':220}]},period='7d')
        self.assertEqual(sub['effective_usd'],220)

    def test_history_is_part_of_the_cached_answer(self):
        first,_=self.subscription(None)
        second,_=self.subscription({'codex':[{'until':self.change,'usd':220}]})
        self.assertNotEqual(first['effective_usd'],second['effective_usd'])

    def test_monthly_settlement_prorates_and_keeps_its_prices_on_rebuild(self):
        start=monthly.month_start(stamp('2026-09-15T00:00:00Z'))
        middle=start.timestamp()+10*DAY
        with self.store.connect() as c:
            self.store.event(c,'sep',start.timestamp()+DAY,'OpenAI','codex','gpt-test',dict(uncached_input=3_000_000))
        now=stamp('2026-10-02T00:00:00Z')
        history={'codex':[{'until':middle,'usd':220}],'devin':[{'until':middle,'usd':50}]}
        settled=monthly.build(self.store,start,now,PRICING,{'codex':110},{},history=history)
        sub=settled['subscriptions'][0]
        self.assertAlmostEqual(sub['monthly_usd'],(220*10+110*20)/30)
        self.assertEqual(sub['list_usd'],110)
        self.assertAlmostEqual(sub['ratio'],3/sub['monthly_usd'])
        self.assertEqual([p['monthly_usd'] for p in sub['price_segments']],[220,110])
        self.assertEqual(settled['price_history'],{'codex':[{'until':middle,'usd':220}]})
        # A later price setting does not re-settle the month.
        again=monthly.build(self.store,start,now,PRICING,{'codex':300},{},stored=json.loads(json.dumps(settled)),history={})
        self.assertAlmostEqual(again['subscriptions'][0]['monthly_usd'],sub['monthly_usd'])
        self.assertEqual(again['price_history'],settled['price_history'])

    def test_a_month_settled_before_price_history_keeps_its_price(self):
        start=monthly.month_start(stamp('2026-09-15T00:00:00Z'))
        with self.store.connect() as c:
            self.store.event(c,'sep',start.timestamp()+DAY,'OpenAI','codex','gpt-test',dict(uncached_input=3_000_000))
        old=dict(subscriptions=[dict(route='codex',monthly_usd=220,value=3,tokens=3_000_000,ratio=3/220)])
        again=monthly.build(self.store,start,stamp('2026-10-02T00:00:00Z'),PRICING,{'codex':110},{},stored=old)
        self.assertEqual((again['subscriptions'][0]['monthly_usd'],again['subscriptions'][0]['list_usd']),(220,220))
        self.assertNotIn('price_segments',again['subscriptions'][0])


class PriceHistoryApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.origin='https://fixture.example'
        Store(self.root/'usage.db')
        (self.root/'config.json').write_text('{}')
        self.cfg=dict(database=str(self.root/'usage.db'),config_file=str(self.root/'config.json'),origin=self.origin,
                      secret_key='fixture',allowed_logins=['owner'],subscription_prices={'codex':220,'devin':20})
        self.client=create_app(self.cfg).test_client()
        self.env={'gunicorn.socket':SimpleNamespace(family=socket.AF_UNIX),'REMOTE_ADDR':''}
        self.identity={'Tailscale-User-Login':'owner'}

    def get(self,path):
        return self.client.get(path,base_url=self.origin,headers=self.identity,environ_overrides=self.env)

    def post(self,body):
        token=self.get('/api/bootstrap').json['csrf']
        return self.client.post('/api/config/subscription-prices',json=body,base_url=self.origin,environ_overrides=self.env,
                                headers={**self.identity,'Origin':self.origin,'X-CSRF-Token':token})

    def saved(self):
        return json.loads((self.root/'local.json').read_text())

    def test_a_plan_change_from_now_keeps_the_old_price_for_earlier_use(self):
        before=time.time()
        response=self.post({'prices':{'codex':110,'devin':20}})
        self.assertEqual(response.status_code,200)
        entries=self.saved()['subscription_price_history']['codex']
        self.assertEqual(entries[0]['usd'],220)
        self.assertTrue(before<=entries[0]['until']<=time.time())
        self.assertNotIn('devin',self.saved()['subscription_price_history'])
        self.assertEqual(self.get('/api/config').json['subscription_price_history'],{'codex':entries})
        self.assertEqual(response.json['subscription_price_history'],{'codex':entries})
        sub=next(s for s in self.get('/api/usage?period=7d').json['subscriptions'] if s['route']=='codex')
        self.assertGreater(sub['effective_usd'],110)
        # A correction keeps the change; a price for all time drops it.
        self.post({'prices':{'codex':100,'devin':20},'effective':'correct'})
        self.assertEqual(self.saved()['subscription_price_history']['codex'],entries)
        self.post({'prices':{'codex':90,'devin':20},'effective':'all'})
        self.assertEqual(self.saved()['subscription_price_history'],{})

    def test_a_given_moment_and_invalid_modes(self):
        at=time.time()-3*DAY
        self.assertEqual(self.post({'prices':{'codex':110},'effective':'at','effective_at':at}).status_code,200)
        self.assertEqual(self.saved()['subscription_price_history'],{'codex':[{'until':at,'usd':220}]})
        for body in ({'prices':{'codex':1},'effective':'later'},{'prices':{'codex':1},'effective':'at'},
                     {'prices':{'codex':1},'effective':'at','effective_at':time.time()+DAY},
                     {'prices':{'codex':1},'effective':'at','effective_at':'yesterday'}):
            self.assertEqual(self.post(body).status_code,400,body)
        self.assertEqual(self.saved()['subscription_prices'],{'codex':110})

    def test_names_and_ended_routes_still_save_with_the_prices(self):
        response=self.post({'prices':{'codex':110},'ended':['devin'],'residual_label':'자동 리뷰'})
        self.assertEqual(response.status_code,200)
        saved=self.saved()
        self.assertEqual((list(saved['ended_subscriptions']),saved['residual_label']),(['devin'],'자동 리뷰'))
        self.assertEqual(saved['subscription_price_history']['codex'][0]['usd'],220)


if __name__=='__main__':unittest.main()

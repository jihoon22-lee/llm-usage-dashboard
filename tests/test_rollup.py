"""The hourly rollup must answer exactly what the events table answers."""
from pathlib import Path
import random
import tempfile
import unittest

from llm_usage.store import TOKENS, Store, stamp

NOW=stamp('2026-10-03T03:30:00Z')
PRICING={'m1':dict(input=1,cached=0.1,output=10,cache_write=1),
         'm2':[dict(since='2026-09-20',input=2,output=4),dict(since='2026-09-28',input=3,output=6)]}


def fresh_rollup(c):
    return sorted((tuple(r) for r in c.execute(
        'SELECT CAST(ts/3600 AS INTEGER)*3600,provider,route,model,project,agent_kind,'
        +','.join(f'SUM({k})' for k in TOKENS)+',COUNT(*) FROM events GROUP BY 1,2,3,4,5,6')),key=repr)


class RollupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'usage.db'
        self.store=Store(self.path)
        rng=random.Random(7)
        with self.store.connect() as c:
            for i in range(1500):
                ts=NOW-rng.uniform(0,40*86400)
                route=rng.choice(['codex','claude-code','devin','antigravity'])
                self.store.event(c,f'e{i}',ts,rng.choice(['OpenAI','Anthropic']),route,rng.choice(['m1','m2','m3']),
                                 {k:rng.randint(0,5000) for k in TOKENS},
                                 session=rng.choice([None,'s1','s2','s3']),project=rng.choice([None,'p1','p2']),
                                 agent_kind=rng.choice([None,'main','sub']))
            # Streaming maxima (update), attribution backfill (update), removals (delete).
            for i in range(0,1500,7):
                self.store.event(c,f'e{i}',0,'OpenAI','codex','m1',{k:9000 for k in TOKENS})
            c.execute("UPDATE events SET project='p9',session='s9' WHERE project IS NULL AND id LIKE 'e1%'")
            c.execute("DELETE FROM events WHERE id LIKE 'e2%'")
            # Antigravity merges replace whole rows.
            c.execute("INSERT OR REPLACE INTO events (id,ts,provider,route,model,uncached_input,cached_input,output,cache_creation,reasoning) "
                      "SELECT id,ts+7200,provider,'antigravity',model,1,2,3,4,0 FROM events WHERE id LIKE 'e3%'")

    def test_triggers_keep_the_rollup_equal_to_a_fresh_aggregation(self):
        with self.store.connect() as c:
            stored=sorted((tuple(r) for r in c.execute('SELECT * FROM usage_hourly')),key=repr)
            self.assertEqual(stored,fresh_rollup(c))
        # A store reopened later neither rebuilds nor double counts.
        again=Store(self.path)
        with again.connect() as c:self.assertEqual(sorted((tuple(r) for r in c.execute('SELECT * FROM usage_hourly')),key=repr),stored)

    def test_drift_from_untriggered_writes_is_detected_and_rebuilt(self):
        self.assertTrue(self.store.rollup_matches())
        with self.store.connect() as c:
            c.execute('PRAGMA recursive_triggers=OFF')  # what an older process did
            c.execute("INSERT OR REPLACE INTO events (id,ts,provider,route,model,uncached_input,cached_input,output,cache_creation,reasoning) "
                      "SELECT id,ts,provider,route,model,1,1,1,1,0 FROM events WHERE id='e5'")
        self.assertFalse(self.store.rollup_matches())
        self.store.rebuild_rollup()
        self.assertTrue(self.store.rollup_matches())

    def test_usage_answers_match_the_events_table(self):
        direct=Store(self.path,rollup=False)
        cases=[dict(period=p,granularity=g,group=grp,cumulative=cum,compare=cmp,scope=sc)
               for p,g in (('7d','day'),('30d','hour'),('all','week'),('all','month'),('today','hour'))
               for grp,cum,cmp,sc in (('provider',False,None,None),('model',True,'previous',None),
                                      ('project',False,'week','route:codex'),('agent',False,'month','project:p1'),('route',True,None,'model:m2'))]
        cases.append(dict(period='custom',start='2026-09-01',end='2026-09-30',granularity='day',group='route'))
        for case in cases:
            for sections in ('core','insights'):
                a=self.store.usage(now=NOW,pricing=PRICING,subscriptions={'codex':20},sections=sections,**case)
                b=direct.usage(now=NOW,pricing=PRICING,subscriptions={'codex':20},sections=sections,**case)
                for data in (a,b):
                    for source in data['sources']:source.pop('checked',None)
                self.assertEqual(a,b,(case,sections))
        with self.store.connect() as c:
            self.assertEqual(self.store.project_month(c,NOW,PRICING),direct.project_month(c,NOW,PRICING))


if __name__=='__main__':unittest.main()

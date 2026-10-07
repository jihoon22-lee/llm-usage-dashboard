"""Project session counts remain distinct across daily/model cost groups."""
from pathlib import Path
import tempfile
import unittest
from llm_usage.store import Store, stamp

class ProjectSessionTests(unittest.TestCase):
    def test_sessions_are_distinct_across_models_days_and_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'usage.db')
            with store.connect() as c:
                for key,day,model,session,project in [
                    ('a','07','m1','same','app'),('b','08','m1','same','app'),
                    ('c','08','m2','same','app'),('d','08','m2',None,'app'),
                    ('e','08','m1','other','elsewhere')]:
                    store.event(c,key,stamp('2026-09-'+day+'T01:00:00Z'),'OpenAI','codex',model,
                                {'uncached_input':10},session=session,project=project)
            for scope,sessions,requests in [(None,1,4),('project:app',1,4),('model:m2',1,2),('route:codex',1,4)]:
                with self.subTest(scope=scope):
                    data=store.usage(period='all',scope=scope)
                    app=next(p for p in data['projects'] if p['project']=='app')
                    self.assertEqual(app['sessions'],sessions)
                    self.assertEqual(app['requests'],requests)
                    self.assertEqual(app['uncached_input'],10*requests)
            data=store.usage(period='custom',start='2026-09-08',end='2026-09-08',scope='project:app')
            self.assertEqual(data['projects'][0]['sessions'],1)
            self.assertEqual(data['sessionless_requests'],1)

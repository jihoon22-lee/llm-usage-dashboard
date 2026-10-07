"""KST calendar buckets preserve range totals, including partial weeks/months."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from llm_usage.store import Store,stamp,TOKENS


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'usage.db')

    def add(self,key,date,value):
        with self.store.connect() as c:
            self.store.event(c,key,stamp(date),'OpenAI','codex','gpt-test',dict(uncached_input=value,cached_input=value*2,output=value*3,cache_creation=value*4,reasoning=value))

    def test_monday_kst_boundary_and_partial_weeks(self):
        self.add('sunday','2026-09-06T23:59:59+09:00',1)
        self.add('monday','2026-09-07T00:00:00+09:00',2)
        self.add('end','2026-09-09T00:00:00+09:00',4)
        data=self.store.usage(period='custom',start='2026-09-06',end='2026-09-08',granularity='week')
        self.assertEqual([p['time'] for p in data['series']],['2026-08-31T00:00:00+09:00','2026-09-07T00:00:00+09:00'])
        self.assertEqual([p['values']['OpenAI']['uncached_input'] for p in data['series']],[1,2])
        self.assertEqual(data['totals']['uncached_input'],3)
        self.assertTrue(all(p['partial'] for p in data['series']))
        self.assertEqual(data['series'][0]['range_start'],'2026-09-06T00:00:00+09:00')
        self.assertEqual(data['series'][-1]['range_end_exclusive'],'2026-09-09T00:00:00+09:00')

    def test_months_include_leap_day_and_empty_month(self):
        self.add('feb','2024-02-29T23:59:59+09:00',1)
        self.add('mar','2024-03-01T00:00:00+09:00',2)
        self.add('excluded','2024-05-01T00:00:00+09:00',4)
        data=self.store.usage(period='custom',start='2024-02-01',end='2024-04-30',granularity='month')
        self.assertEqual([p['time'][:10] for p in data['series']],['2024-02-01','2024-03-01','2024-04-01'])
        self.assertEqual([p['values']['OpenAI']['uncached_input'] for p in data['series']],[1,2,0])
        self.assertFalse(any(p['partial'] for p in data['series']))

    def test_calendar_year_transition_and_all_grains_preserve_totals(self):
        self.add('dec','2026-12-31T23:59:59+09:00',1)
        self.add('jan','2027-01-01T00:00:00+09:00',2)
        reference=None
        for grain in ('hour','day','week','month'):
            data=self.store.usage(period='custom',start='2026-12-30',end='2027-01-02',granularity=grain)
            if reference is None:reference=data['totals']
            self.assertEqual(data['totals'],reference)
            for field in TOKENS:
                self.assertEqual(sum(p['values']['OpenAI'][field] for p in data['series']),reference[field])
            cumulative=self.store.usage(period='custom',start='2026-12-30',end='2027-01-02',granularity=grain,cumulative=True)
            self.assertEqual({k:cumulative['series'][-1]['values']['OpenAI'][k] for k in TOKENS},{k:reference[k] for k in TOKENS})
        self.assertEqual([p['time'][:7] for p in data['series']],['2026-12','2027-01'])
        self.assertTrue(all(p['partial'] for p in data['series']))


if __name__=='__main__':unittest.main()

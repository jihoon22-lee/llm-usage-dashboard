"""An explicit model tier wins over the broader family's snapshot fallback."""
import unittest
from pathlib import Path
import tempfile
from llm_usage.pricing import BUILTIN, entry_key, rate_for, estimate, load_pricing

class PricingResolutionTests(unittest.TestCase):
    def test_exact_base_precedes_parent_snapshot(self):
        self.assertEqual(entry_key('gpt-5-mini (high)',BUILTIN),'gpt-5-mini')
        self.assertAlmostEqual(estimate({'uncached_input':1_000_000,'output':1_000_000},
                                       rate_for('gpt-5-mini (high)',BUILTIN)),2.25)

    def test_exact_override_dated_snapshot_and_unknown_tiers(self):
        special=dict(input=7,output=9)
        table={**BUILTIN,'gpt-5-mini (high)':special}
        self.assertIs(rate_for('gpt-5-mini (high)',table),special)
        self.assertEqual(entry_key('gpt-5-mini-2026-09-01 (high)',BUILTIN),'gpt-5-mini')
        self.assertIsNone(rate_for('gpt-5-nano (high)',BUILTIN))
        history=[dict(since='2026-01-01',input=1,output=2),dict(since='2026-09-01',input=2,output=3)]
        self.assertEqual(rate_for('gpt-5-mini (high)',{'gpt-5':BUILTIN['gpt-5'],'gpt-5-mini':history},'2026-08-01')['input'],1)

    def test_hidden_variants_block_inheritance_but_exact_child_overrides_still_work(self):
        with tempfile.TemporaryDirectory() as folder:
            variants = ('review-model (high)', 'review-model-2026-10-01', 'review-model-preview')
            cfg = {'pricing_file': str(Path(folder)/'absent.json'),
                   'pricing': {'review-model': dict(input=1, output=2), **dict.fromkeys(variants)}}
            table = load_pricing(cfg)
            for model in variants:
                with self.subTest(model=model):
                    self.assertIsNone(rate_for(model, table))
            hidden_parent = load_pricing({**cfg, 'pricing': {'review-model': None, 'review-model (high)': dict(input=3, output=4)}})
            self.assertIsNone(rate_for('review-model-preview', hidden_parent))
            self.assertEqual(rate_for('review-model (high)', hidden_parent)['input'], 3)

    def test_hidden_variant_invalidates_usage_and_finished_reports(self):
        from llm_usage.store import Store, stamp
        from llm_usage import reports, monthly
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder)/'usage.db')
            cfg = {'database': store.path, 'pricing_file': str(Path(folder)/'absent.json'),
                   'pricing': {'review-model': dict(input=2, output=4)}}
            model = 'review-model (high)'
            now = stamp('2026-10-23T09:00:00Z')
            with store.connect() as c:
                for key, ts in [('month', '2026-09-15T01:00:00Z'), ('week', '2026-10-15T01:00:00Z')]:
                    store.event(c, key, stamp(ts), 'OpenAI', 'codex', model, {'uncached_input': 1_000_000})
            prices = {'codex': 20}
            before = load_pricing(cfg)
            self.assertEqual(store.usage(period='all', now=now, pricing=before)['cost']['period'], 4)
            reports.ensure_report(store, now, before)
            monthly.ensure_months(store, now, before, prices)
            hidden = load_pricing({**cfg, 'pricing': {**cfg['pricing'], model: None}})
            usage = store.usage(period='all', now=now, pricing=hidden)
            self.assertEqual(usage['totals']['uncached_input'], 2_000_000)
            self.assertIsNone(usage['cost']['period'])
            self.assertIsNone(usage['cost']['lifetime'])
            reports.ensure_report(store, now+60, hidden)
            monthly.ensure_months(store, now+60, hidden, prices)
            self.assertIsNone(reports.recent(store)[0]['totals']['cost'])
            self.assertIsNone(monthly.recent(store)[0]['subscriptions'][0]['value'])


class BuiltinTableTests(unittest.TestCase):
    """The builtin table against the public price lists checked on 2026-11-10."""

    def test_every_builtin_entry_is_a_usable_rate(self):
        from llm_usage.pricing import _valid
        for model,entry in BUILTIN.items():
            for rates in entry if isinstance(entry,list) else [entry]:
                with self.subTest(model=model):
                    self.assertTrue(_valid(rates))
                    self.assertLessEqual(rates.get('cached',0),rates['input'])
                    self.assertGreaterEqual(rates.get('cache_write_1h',rates.get('cache_write',0)),rates.get('cache_write',0))
            if isinstance(entry,list):
                self.assertEqual([e['since'] for e in entry],sorted(e['since'] for e in entry),model)

    def test_current_models_resolve_to_their_own_rate(self):
        expected={
            'claude-fable-5-2':(8,0.2,40,10,16),
            'claude-opus-5-5':(4,0.2,20,5,8),
            'claude-opus-5':(5,0.5,25,6.25,10),
            'claude-opus-4-7':(5,0.5,25,6.25,10),
            'claude-sonnet-5-5':(2,0.1,10,2.5,4),
            'claude-sonnet-5-5 (high)':(2,0.1,10,2.5,4),
            'claude-sonnet-5-5-high':(2,0.1,10,2.5,4),
            'claude-sonnet-5':(2,0.2,10,2.5,4),
            'claude-haiku-5-5':(0.1,0.01,0.5,0.125,0.2),
            'claude-haiku-4-5':(1,0.1,5,1.25,2),
            'claude-haiku-4-5-20251001':(1,0.1,5,1.25,2),
        }
        for model,(inp,cached,output,write,hour) in expected.items():
            with self.subTest(model=model):
                rate=rate_for(model,BUILTIN,'2026-11-10')
                self.assertEqual((rate['input'],rate['cached'],rate['output'],rate['cache_write'],rate['cache_write_1h']),
                                 (inp,cached,output,write,hour))
        for model,(inp,cached,output) in {'gpt-6-astra':(10,1,50),'gpt-6.1-sol':(2,0.1,10),'gpt-6-sol':(2,0.2,10),
                                         'gemini-4-argon-trial':(2,0.1,10),'gemini-3.1-pro-low':(2,0.2,12),
                                         'gemini-3.8-flash-n':(0.75,0.075,3.75),
                                         'muse-spark-1.3':(1.25,0.15,4.25),
                                         'muse-spark-1.3-contributor-free':(0.1,0.002,0.2),
                                         'deepseek-v4.1-flash-free':(0.15,0.003,0.6),
                                         'deepseek-v4-flash':(0.14,0.0028,0.28)}.items():
            with self.subTest(model=model):
                rate=rate_for(model,BUILTIN,'2026-11-10')
                self.assertEqual((rate['input'],rate['cached'],rate['output']),(inp,cached,output))

    def test_a_newer_version_never_inherits_an_older_rate(self):
        for model in ('claude-fable-5-3','claude-opus-5-6','claude-sonnet-5-6','claude-haiku-5-6','claude-haiku-4',
                      'claude-opus-6','gpt-6.2-sol','gpt-6-terra','gemini-4-argon-pro','muse-spark-1.4','swe-2 (max)'):
            with self.subTest(model=model):
                self.assertIsNone(rate_for(model,BUILTIN))

    def test_hour_cache_writes_use_the_hour_rate_when_the_model_has_one(self):
        from llm_usage.pricing import cache_estimated_tokens
        values={'cache_creation':3_000_000,'cache_creation_5m':1_000_000,'cache_creation_1h':2_000_000}
        rate=rate_for('claude-opus-5',BUILTIN)
        self.assertAlmostEqual(estimate(values,rate),6.25+2*10)
        self.assertEqual(cache_estimated_tokens(values,rate),0)

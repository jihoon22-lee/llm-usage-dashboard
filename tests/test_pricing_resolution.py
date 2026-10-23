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

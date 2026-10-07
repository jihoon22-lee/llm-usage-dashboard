"""An explicit model tier wins over the broader family's snapshot fallback."""
import unittest
from llm_usage.pricing import BUILTIN, entry_key, rate_for, estimate

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

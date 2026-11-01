"""개요 (/api/overview): each figure equals the usage tab's for the same span, and the facts
describe recorded use only (no forecast, expected pace or advice)."""
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest

from llm_usage import overview
from llm_usage.insights import token_total
from llm_usage.reconciliation import RESIDUAL
from llm_usage.store import Store, stamp
from llm_usage.webapp import create_app

NOW = stamp('2026-11-05T03:00:00Z')  # Thursday 11-05 12:00 KST
PRICING = {'gpt-test': dict(input=1, cached=0.1, output=10, cache_write=1),
           'claude-x': dict(input=3, cached=0.3, output=15, cache_write=3)}
SPAN = dict(group='route', pricing=PRICING, sections='core', now=NOW)


def keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from keys(v)


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name)/'usage.db')
        with self.store.connect() as c:
            for day in range(40):
                ts = NOW-day*86400-3600
                self.store.event(c, f'c{day}', ts, 'OpenAI', 'codex', 'gpt-test', dict(uncached_input=1000*(day+1), output=100), project='alpha')
                self.store.event(c, f'a{day}', ts-1800, 'Anthropic', 'claude-code', 'claude-x', dict(cached_input=5000, output=50),
                                 project='beta' if day % 2 else None)
            # One day far above the others (10-26).
            self.store.event(c, 'peak', NOW-10*86400, 'OpenAI', 'codex', 'gpt-test', dict(uncached_input=2_000_000), project='alpha')
            # A Codex account day above this PC's records: its residual counts in totals, on no model or project.
            self.store.account_tokens(c, 'codex', [dict(startDate='2026-10-30', tokens=9_000_000)], NOW)

    def build(self, **kw):
        return overview.build(self.store, NOW, pricing=PRICING, **kw)

    def test_periods_equal_the_usage_cards(self):
        data = self.build()
        today = self.store.usage(period='today', **SPAN)
        week = self.store.usage(period='custom', start='2026-11-02', end='2026-11-08', **SPAN)
        last30 = self.store.usage(period='30d', **SPAN)
        self.assertEqual(data['today']['tokens'], token_total(today['totals']))
        self.assertEqual(data['today']['prior']['tokens'], token_total(today['prior']))
        self.assertEqual(data['week']['tokens'], token_total(week['totals']))
        self.assertEqual(data['week']['prior']['tokens'], token_total(week['prior']))
        self.assertEqual(data['week']['prior']['current_tokens'], week['prior']['current_tokens'])
        self.assertEqual(data['recent']['tokens'], token_total(last30['totals']))
        self.assertGreater(last30['reconciliation']['residual_tokens'], 0)
        # The bars and the part no single day holds make up the 30-day total.
        recent = data['recent']
        self.assertEqual(sum(s['tokens'] for s in recent['services'])+recent['unallocated_tokens'], recent['tokens'])
        self.assertEqual((len(recent['days']), recent['days'][-1], recent['active_days']), (30, '2026-11-05', 30))
        # Hours and days not reached yet are absent, not zero: 13:00–23:00 KST and Friday to Sunday.
        self.assertEqual((len(data['today']['points']), data['today']['points'].count(None)), (24, 11))
        self.assertEqual(data['week']['points'][4:], [None, None, None])
        self.assertTrue(all(v is not None for v in data['week']['points'][:4]))

    def test_ranked_rows_equal_their_drill_scope(self):
        data = self.build()
        self.assertTrue(data['models'] and data['projects'])
        for row in data['models']:
            scoped = self.store.usage(period='30d', scope='model:'+row['name'], **SPAN)
            self.assertEqual(row['tokens'], token_total(scoped['totals']), row)
        for row in data['projects']:
            scoped = self.store.usage(period='30d', scope='project:'+row['name'], **SPAN)
            self.assertEqual(row['tokens'], token_total(scoped['totals']), row)
        names = [r['name'] for r in data['models']+data['projects']]
        self.assertNotIn(RESIDUAL, names)
        # Records without a project are listed as the usage tab names them.
        self.assertIn('미분류', [r['name'] for r in data['projects']])
        self.assertEqual(data['models'][0]['route'], 'codex')
        self.assertTrue(all(0 < r['share'] <= 100 for r in data['models']+data['projects']))

    def test_facts_are_observations_without_forecasts(self):
        with self.store.connect() as c:
            self.store.save_state(c, 'report:2026-10-26', dict(
                week='2026-10-26', start='2026-10-26', end='2026-11-01', totals=dict(tokens=1000),
                routes=[dict(route='codex', tokens=600, change_pct=150.0), dict(route='claude-code', tokens=400, change_pct=-5.0)],
                exhausted=[dict(route='codex', bucket='codex · 10080분', count=2, display_name='주간')],
                quota_use=[dict(route='codex', bucket='codex · 10080분', zero_hours=12.5, streak=3)]))
            self.store.save_state(c, 'month:2026-10', dict(month='2026-10', partial=False, subscriptions=[
                dict(route='codex', monthly_usd=200, value=900, ratio=4.5, tokens=10),
                dict(route='claude-code', monthly_usd=100, value=40, ratio=0.4, tokens=5)]))
        data = self.build(subscriptions={'codex': 200, 'claude-code': 100, 'devin': 20})
        facts = {f['kind']: f for f in data['facts']}
        self.assertEqual([f['kind'] for f in data['facts']][:3], ['exhausted', 'zero_hours', 'service_change'])
        self.assertEqual((facts['exhausted']['count'], facts['exhausted']['streak'], facts['exhausted']['display_name']), (2, 3, '주간'))
        self.assertEqual(facts['zero_hours']['hours'], 12.5)
        # A small service's change (claude-code −5%) is not named.
        self.assertEqual((facts['service_change']['route'], facts['service_change']['change_pct']), ('codex', 150.0))
        self.assertEqual((facts['idle_subscription']['route'], facts['idle_subscription']['collected']), ('devin', True))
        self.assertEqual((facts['value_below_price']['route'], facts['value_below_price']['ratio']), ('claude-code', 0.4))
        self.assertEqual(facts['peak_day']['day'], '2026-10-26')
        self.assertGreaterEqual(facts['peak_day']['ratio'], overview.PEAK_DAY_RATIO)
        self.assertLessEqual(len(data['facts']), overview.FACTS)
        month = data['month']
        self.assertEqual((month['month'], month['price'], month['value']), ('2026-10', 300, 940))
        self.assertAlmostEqual(month['ratio'], 940/300)
        forbidden = {'forecast', 'projection', 'projected', 'expected', 'pace', 'baseline', 'month_end'}
        self.assertFalse(forbidden & set(keys(data)), forbidden & set(keys(data)))
        json.dumps(data)

    def test_an_empty_store_has_nothing_to_say(self):
        with tempfile.TemporaryDirectory() as folder:
            data = overview.build(Store(Path(folder)/'usage.db'), NOW, pricing=PRICING)
        self.assertEqual((data['today']['tokens'], data['week']['tokens'], data['recent']['tokens']), (0, 0, 0))
        self.assertEqual((data['recent']['services'], data['models'], data['projects'], data['month'], data['facts']), ([], [], [], None, []))
        self.assertEqual(data['recent']['active_days'], 0)

    def test_a_priced_service_without_a_record_source_is_not_called_unused(self):
        with self.store.connect() as c:
            self.store.source(c, 'devin-records', 'unavailable', 'sessions.db 없음', NOW-60)
        data = self.build(subscriptions={'devin': 20})
        idle = next(f for f in data['facts'] if f['kind'] == 'idle_subscription')
        self.assertEqual((idle['route'], idle['collected']), ('devin', False))


class OverviewWebTests(unittest.TestCase):
    def test_overview_api_answers_the_tab(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = create_app(dict(database=str(root/'usage.db'), origin='https://pc.example', secret_key='t', allowed_logins=['owner'],
                                  config_file=str(root/'config.json'), subscription_prices={'codex': 20}))
            env = {'REMOTE_ADDR': '', 'gunicorn.socket': SimpleNamespace(family=socket.AF_UNIX)}
            response = app.test_client().get('/api/overview', base_url='https://pc.example', headers={'Tailscale-User-Login': 'owner'},
                                             environ_overrides=env)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertLessEqual({'today', 'week', 'recent', 'models', 'projects', 'month', 'facts'}, set(data))
            self.assertEqual([f['kind'] for f in data['facts']], ['idle_subscription'])


if __name__ == '__main__':
    unittest.main()

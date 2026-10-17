"""A subscription the user marks ended stops quota polling and folds its quota rows."""
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from llm_usage import collect, limits
from llm_usage.config import ended_routes, with_local
from llm_usage.store import Store, USER_ENDED_DETAIL
from llm_usage.webapp import create_app

NOW = time.time()


class EndedSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'usage.db')
        with self.store.connect() as c:
            for bucket in ('daily', 'weekly'):
                self.store.limit(c, 'devin', bucket, 100, NOW+3600, NOW-10, 'devin')
            self.store.source(c, 'devin', 'ok', 'Free · 계정 사용량 조회', NOW-10)
            self.store.limit(c, 'codex', 'codex · 10080분', 60, NOW+86400, NOW-10, 'codex')
            self.store.source(c, 'codex', 'ok', '', NOW-10)

    def test_ended_route_rows_fold_without_touching_other_routes(self):
        rows = self.store.limits(NOW, ended={'devin'})['limits']
        devin = [r for r in rows if r['route'] == 'devin']
        self.assertEqual({r['status'] for r in devin}, {'ended'})
        self.assertTrue(all(r['user_ended'] and r['stale'] for r in devin))
        self.assertTrue(all('구독 종료' in r['note'] for r in devin))
        codex = next(r for r in rows if r['route'] == 'codex')
        self.assertEqual(codex['status'], 'fresh')
        self.assertFalse(codex['user_ended'])
        # Without the setting the same data stays current.
        self.assertEqual({r['status'] for r in self.store.limits(NOW)['limits'] if r['route'] == 'devin'}, {'fresh'})

    def test_routes_without_rows_still_report_ended(self):
        rows = self.store.limits(NOW, ended={'opencode-go'})['limits']
        placeholder = next(r for r in rows if r['route'] == 'opencode-go')
        self.assertEqual(placeholder['status'], 'ended')

    def test_re_enabled_route_waits_for_the_next_poll_instead_of_staying_ended(self):
        with self.store.connect() as c:
            self.store.source(c, 'devin', 'ended', USER_ENDED_DETAIL, NOW-5)
        rows = [r for r in self.store.limits(NOW)['limits'] if r['route'] == 'devin']
        self.assertEqual({r['status'] for r in rows}, {'unavailable'})
        self.assertTrue(all('재개' in r['detail'] for r in rows))
        # A provider-reported end (OpenCode Go entitlement) is not a user setting.
        with self.store.connect() as c:
            self.store.source(c, 'devin', 'ended', '계정에 구독이 없습니다.', NOW-1)
        self.assertEqual({r['status'] for r in self.store.limits(NOW)['limits'] if r['route'] == 'devin'}, {'ended'})

    def test_poll_skips_ended_subscriptions_and_marks_their_sources(self):
        settings = dict(database=str(self.root/'usage.db'), ended_subscriptions={'devin': {'since': NOW}, 'codex': {'since': NOW}})
        called = []
        with patch.object(limits, 'claude_contexts', return_value=[None]), \
             patch.object(limits, 'poll_claude', lambda *a: called.append('claude')), \
             patch.object(limits, 'poll_claude_resources', lambda *a: called.append('claude-resources')), \
             patch.object(limits, 'collect_claude_cloud', lambda *a: called.append('cloud')), \
             patch.object(limits, 'poll_devin', lambda *a: called.append('devin')), \
             patch.object(limits, 'poll_antigravity_app', lambda *a: called.append('antigravity')), \
             patch.object(limits, 'read_codex', side_effect=AssertionError('ended Codex was polled')), \
             patch.object(limits.urllib.request, 'build_opener', side_effect=AssertionError('network')):
            settings['opencode_auth'] = str(self.root/'missing.json')
            limits.poll_limits(self.store, settings)
        self.assertNotIn('devin', called)
        self.assertIn('claude', called)
        with self.store.connect() as c:
            sources = {r['name']: dict(r) for r in c.execute('SELECT * FROM sources')}
        for name in ('devin', 'codex', 'codex-account-usage', 'codex-thread-usage'):
            self.assertEqual(sources[name]['status'], 'ended', name)
            self.assertEqual(sources[name]['detail'], USER_ENDED_DETAIL)

    def test_status_line_of_an_ended_route_is_not_ingested(self):
        inbox = self.root/'status'; inbox.mkdir()
        (inbox/'antigravity.json').write_text(json.dumps(dict(route='antigravity', checked=NOW,
                                                              data={'quota': {'gemini-5h': {'remaining_fraction': .5}}})))
        collect.collect_status(self.store, dict(status_inboxes=[str(inbox)], ended_subscriptions={'antigravity': {}}))
        with self.store.connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM limits WHERE route='antigravity'").fetchone())
        collect.collect_status(self.store, dict(status_inboxes=[str(inbox)]))
        with self.store.connect() as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM limits WHERE route='antigravity'").fetchone())

    def test_ended_routes_ignore_malformed_settings(self):
        self.assertEqual(ended_routes({'ended_subscriptions': ['devin']}), set())
        self.assertEqual(ended_routes({'ended_subscriptions': {'devin': {}}}), {'devin'})
        self.assertEqual(ended_routes(None), set())


class EndedSubscriptionApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.origin = 'https://fixture.example'
        store = Store(self.root/'usage.db')
        with store.connect() as c:
            store.limit(c, 'devin', 'weekly', 100, NOW+3600, NOW-10, 'devin')
            store.source(c, 'devin', 'ok', '', NOW-10)
            store.event(c, 'e1', NOW-60, 'Cognition', 'devin', 'swe-2', dict(uncached_input=10, output=5), session='s')
        (self.root/'config.json').write_text('{}')
        self.cfg = dict(database=str(self.root/'usage.db'), config_file=str(self.root/'config.json'), origin=self.origin,
                        secret_key='fixture', allowed_logins=['owner'], subscription_prices={'devin': 20, 'codex': 200},
                        pricing={'swe-2': dict(input=1, output=1)})
        self.client = create_app(self.cfg).test_client()
        self.env = {'gunicorn.socket': SimpleNamespace(family=socket.AF_UNIX), 'REMOTE_ADDR': ''}
        self.identity = {'Tailscale-User-Login': 'owner'}

    def get(self, path):
        return self.client.get(path, base_url=self.origin, headers=self.identity, environ_overrides=self.env)

    def post(self, path, body):
        token = self.get('/api/bootstrap').json['csrf']
        return self.client.post(path, json=body, base_url=self.origin, environ_overrides=self.env,
                                headers={**self.identity, 'Origin': self.origin, 'X-CSRF-Token': token})

    def test_saving_ended_routes_persists_dates_and_applies_to_limits_and_value(self):
        response = self.post('/api/config/subscription-prices', {'prices': {'devin': 20, 'codex': 200}, 'ended': ['devin', 'devin']})
        self.assertEqual(response.status_code, 200)
        saved = json.loads((self.root/'local.json').read_text())
        self.assertEqual(list(saved['ended_subscriptions']), ['devin'])
        since = saved['ended_subscriptions']['devin']['since']
        self.assertEqual(self.get('/api/config').json['ended_subscriptions'], {'devin': {'since': since}})
        self.assertEqual(with_local({'database': self.cfg['database']})['ended_subscriptions'], {'devin': {'since': since}})
        rows = [r for r in self.get('/api/limits').json['limits'] if r['route'] == 'devin']
        self.assertEqual({r['status'] for r in rows}, {'ended'})
        subs = {s['route'] for s in self.get('/api/usage?period=7d').json['subscriptions']}
        self.assertNotIn('devin', subs); self.assertIn('codex', subs)
        # Saving again keeps the original date; omitting 'ended' keeps the state.
        self.post('/api/config/subscription-prices', {'prices': {}, 'ended': ['devin']})
        self.assertEqual(json.loads((self.root/'local.json').read_text())['ended_subscriptions']['devin']['since'], since)
        self.post('/api/config/subscription-prices', {'prices': {'devin': 20}})
        self.assertIn('devin', json.loads((self.root/'local.json').read_text())['ended_subscriptions'])
        self.post('/api/config/subscription-prices', {'prices': {'devin': 20}, 'ended': []})
        self.assertEqual(json.loads((self.root/'local.json').read_text())['ended_subscriptions'], {})
        self.assertEqual({r['status'] for r in self.get('/api/limits').json['limits'] if r['route'] == 'devin'}, {'fresh'})

    def test_invalid_ended_lists_and_account_route_prices(self):
        for body in ({'prices': {}, 'ended': 'devin'}, {'prices': {}, 'ended': ['Devin!']}, {'prices': {}, 'ended': ['devin\n']},
                     {'prices': {'codex\n': 1}}):
            self.assertEqual(self.post('/api/config/subscription-prices', body).status_code, 400, body)
        response = self.post('/api/config/subscription-prices', {'prices': {'claude-code:a1b2c3d4e5f6': 100}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['subscription_prices'], {'claude-code:a1b2c3d4e5f6': 100})


if __name__ == '__main__':
    unittest.main()

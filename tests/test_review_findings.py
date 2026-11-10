"""Regressions for the 2026-11-10 review: bodies that are not objects, incomplete rates,
unusable rate files, records SQLite cannot store, and reads beside another application's database."""
import http.client
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from llm_usage import notify, planning
from llm_usage.collect import collect_local
from llm_usage.pricing import BUILTIN, entry_key, estimate, load_pricing, rate_for
from llm_usage.sqlite_ro import open_readonly
from llm_usage.store import Store, stamp
from llm_usage.webapp import create_app

NOW = stamp('2026-11-10T03:00:00Z')


class Sock:
    family = socket.AF_UNIX


class WebInputTests(unittest.TestCase):
    origin = 'https://dashboard.test'
    identity = {'Tailscale-User-Login': 'fixture'}
    env = {'gunicorn.socket': Sock()}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.data = self.root/'data'; self.data.mkdir()
        (self.root/'config.json').write_text('{}')
        # A hand-written rate file in the config folder, below the web-edited one.
        (self.root/'pricing.json').write_text(json.dumps({'my-model': dict(input=1, output=2)}))
        Store(self.data/'usage.db')
        self.client = create_app(dict(database=str(self.data/'usage.db'), origin=self.origin, secret_key='synthetic'*4,
                                      allowed_logins=['fixture'], config_file=str(self.root/'config.json'))).test_client()

    def post(self, path, **kw):
        token = self.client.get('/api/bootstrap', base_url=self.origin, environ_overrides=self.env, headers=self.identity).json['csrf']
        headers = {**self.identity, 'Origin': self.origin, 'X-CSRF-Token': kw.pop('token', token)}
        return self.client.post(path, base_url=self.origin, environ_overrides=self.env, headers=headers, **kw)

    def test_a_body_that_is_not_an_object_is_a_bad_request(self):
        for path in ('/api/config/subscription-prices', '/api/config/pricing', '/api/config/refresh', '/api/config/value-alert',
                     '/api/config/thresholds', '/api/config/project-budgets', '/api/config/notify'):
            for body in ([1], 'x', 1):
                with self.subTest(path=path, body=body):
                    self.assertEqual(self.post(path, json=body).status_code, 400)
        deep = self.post('/api/config/refresh', data='['*30000, content_type='application/json')
        self.assertEqual(deep.status_code, 400)

    def test_application_time_is_only_read_for_a_dated_change(self):
        self.assertEqual(self.post('/api/config/subscription-prices', json=dict(prices={'codex': 20}, effective_at='x')).status_code, 200)
        self.assertEqual(self.post('/api/config/subscription-prices', json=dict(prices={'codex': 25}, effective='at', effective_at='x')).status_code, 400)

    def test_a_token_header_outside_ascii_is_refused_not_an_error(self):
        self.assertEqual(self.post('/api/config/refresh', json=dict(refresh_seconds=60), token='토큰').status_code, 403)

    def test_model_names_and_rates_are_checked_whole(self):
        self.assertEqual(self.post('/api/config/pricing', json=dict(model='gpt-5\n', input=1, output=2)).status_code, 400)
        # A rate with no input or output prices nothing: it is refused, not stored and ignored.
        self.assertEqual(self.post('/api/config/pricing', json=dict(model='new-model', cached=1)).status_code, 400)
        self.assertFalse((self.data/'pricing.json').exists())
        self.assertEqual(self.post('/api/config/pricing', json=dict(model='new-model', input=3, output=6, cached=0.3)).status_code, 200)
        # Emptying an optional rate removes it; the required ones stay.
        self.assertEqual(self.post('/api/config/pricing', json=dict(model='new-model', cached=None)).status_code, 200)
        self.assertEqual(json.loads((self.data/'pricing.json').read_text()), {'new-model': dict(input=3, output=6)})

    def test_a_web_edit_does_not_copy_the_config_folder_rates(self):
        self.assertEqual(self.post('/api/config/pricing', json=dict(model='other-model', input=5, output=6)).status_code, 200)
        self.assertEqual(set(json.loads((self.data/'pricing.json').read_text())), {'other-model'})
        (self.root/'pricing.json').write_text(json.dumps({'my-model': dict(input=100, output=200)}))
        config = dict(database=str(self.data/'usage.db'), pricing_file=str(self.root/'pricing.json'))
        self.assertEqual(rate_for('my-model', load_pricing(config))['input'], 100)

    def test_a_bot_token_that_would_break_its_address_is_refused(self):
        self.assertEqual(self.post('/api/config/notify', json=dict(telegram_token='123456:SECRET AAA', telegram_chat='1')).status_code, 400)
        self.assertEqual(self.post('/api/config/notify', json=dict(telegram_token=' 123456:SECRET-aaa_BBB ', telegram_chat='1')).status_code, 200)


class NotifyTests(unittest.TestCase):
    def test_a_request_the_client_cannot_build_names_only_its_type(self):
        with patch.object(notify, '_post', side_effect=http.client.InvalidURL("URL can't contain control characters. '/bot1:SECRET X/'")):
            errors = notify.send(dict(telegram_token='1:SECRET X', telegram_chat='1'), '제목', '본문')
        self.assertEqual(errors, {'telegram': 'InvalidURL'})

    def test_recovery_needs_a_new_reset_not_a_second_of_jitter(self):
        row = dict(route='codex', bucket='codex · 300분', status='fresh', remaining=15.5, resets=NOW+3601, checked=NOW,
                   scope=dict(role='common', group='common', label='공통 한도'))
        before = {'codex|codex · 300분': dict(low=True, zero=False, resets=NOW+3600)}
        key = next(iter(notify.quota_messages([row], {}, 10, NOW)[1]))
        sent, _ = notify.quota_messages([row], {key: before['codex|codex · 300분']}, 10, NOW)
        self.assertEqual([m[0] for m in sent], [])
        sent, _ = notify.quota_messages([{**row, 'resets': NOW+18000}], {key: before['codex|codex · 300분']}, 10, NOW)
        self.assertEqual([m[0] for m in sent], ['recovered'])


class PlanningTests(unittest.TestCase):
    def test_what_was_recorded_about_an_observation_stays_on_its_scope(self):
        rows = [dict(route='claude-code', bucket='five_hour', status='fresh', remaining=0.0, resets=NOW+600, checked=NOW,
                     history=[], scope={'identity_unverified': True})]
        planning.enrich(rows, NOW)
        self.assertEqual((rows[0]['scope']['role'], rows[0]['scope'].get('identity_unverified')), ('common', True))
        # An unverified local reading raises no quota notice.
        self.assertEqual(notify.quota_messages(rows, {}, 10, NOW)[0], [])


class PricingTests(unittest.TestCase):
    def table(self, entries):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        path = Path(folder.name)/'pricing.json'; path.write_text(json.dumps(entries))
        return load_pricing({'pricing_file': str(path)})

    def test_an_unset_optional_rate_is_ignored_and_a_wrong_one_drops_the_entry(self):
        table = self.table({'a': dict(input=1, output=2, cache_write=None), 'b': dict(input=1, output=2, cached='0.1'),
                            'c': dict(input=1, output=-2), 'd': [dict(since='2026-01-01', input=1, output=2, cached=None)]})
        self.assertEqual(table['a'], dict(input=1, output=2))
        self.assertEqual(estimate(dict(uncached_input=1_000_000, cache_creation=1_000_000), rate_for('a', table)), 2)
        self.assertNotIn('b', table); self.assertNotIn('c', table)
        self.assertEqual(table['d'], [dict(since='2026-01-01', input=1, output=2)])

    def test_a_rate_file_that_is_not_an_object_is_no_rates(self):
        self.assertIn('gpt-5', self.table([1, 2]))

    def test_a_tier_word_anywhere_in_the_suffix_is_another_model(self):
        for model in ('gpt-5-codex-mini', 'gemini-2.5-flash-preview-lite', 'gemini-3.1-pro-preview-flash'):
            with self.subTest(model=model):
                self.assertIsNone(entry_key(model, BUILTIN))
        self.assertEqual(entry_key('gpt-5-codex', BUILTIN), 'gpt-5')
        self.assertEqual(entry_key('gemini-2.5-pro-preview-06-05', BUILTIN), 'gemini-2.5-pro')


class QuotaPlaceholderTests(unittest.TestCase):
    def test_a_route_with_no_reading_is_not_an_earlier_accounts_observation(self):
        from llm_usage.resources import observe_account
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        store = Store(Path(folder.name)/'usage.db')
        with store.connect() as c:
            observe_account(c, 'codex', 'acct-1', NOW-30)
            store.save_state(c, 'quota_snapshot:codex', dict(source='codex', checked=NOW-30, buckets=[], absent=[]))
        row = next(r for r in store.limits(now=NOW)['limits'] if r['route'] == 'codex')
        self.assertEqual((row['bucket'], row['previous_account'], row['retired']), ('미제공', False, False))


def claude_line(message, **extra):
    return json.dumps({'type': 'assistant', 'timestamp': '2026-11-09T10:00:00Z', 'requestId': 'r-'+message, 'sessionId': 's', 'cwd': '/nowhere/p',
                       'message': {'id': message, 'model': 'claude-test', 'usage': {'input_tokens': 1, 'output_tokens': 2}}, **extra})+'\n'


class CollectorRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.records = self.root/'projects'; self.records.mkdir()
        self.store = Store(self.root/'usage.db')
        self.config = dict(sources=[dict(name='claude', route='claude-code', kind='jsonl', path=str(self.records))])

    def events(self):
        with self.store.connect() as c:
            return c.execute('SELECT COUNT(*) FROM events').fetchone()[0]

    def test_a_record_sqlite_cannot_store_is_one_unreadable_record(self):
        (self.records/'a.jsonl').write_text(claude_line('bad', sessionId={'x': 1})+claude_line('a1')+claude_line('a2'))
        (self.records/'b.jsonl').write_text(claude_line('b1'))
        collect_local(self.store, self.config)
        self.assertEqual(self.events(), 3)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM import_errors').fetchone()[0], 1)

    def test_a_whole_file_failure_is_over_once_the_file_is_read(self):
        path = self.records/'a.jsonl'; path.write_text(claude_line('a1'))
        collect_local(self.store, self.config)
        with path.open('a') as f: f.write(claude_line('a2'))
        path.chmod(0)
        try: collect_local(self.store, self.config)
        finally: path.chmod(0o644)
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM import_errors WHERE record='file'").fetchone()[0], 1)
        collect_local(self.store, self.config)
        self.assertEqual(self.events(), 2)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM import_errors').fetchone()[0], 0)


class ReadOnlyDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)/'app'; self.folder.mkdir(); self.path = self.folder/'app.db'

    def create(self, mode):
        c = sqlite3.connect(self.path)
        c.execute('PRAGMA journal_mode='+mode); c.execute('CREATE TABLE t(x)'); c.execute('INSERT INTO t VALUES(1)')
        c.commit(); c.close()

    def test_a_closed_wal_database_gets_no_files_beside_it(self):
        self.create('WAL')
        con = open_readonly(self.path)
        try: self.assertEqual(con.execute('SELECT COUNT(*) FROM t').fetchone()[0], 1)
        finally: con.close()
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ['app.db'])

    def test_a_database_locked_by_its_writer_is_not_read_around(self):
        self.create('DELETE')
        writer = sqlite3.connect(self.path, isolation_level=None)
        try:
            writer.execute('BEGIN EXCLUSIVE'); writer.execute('INSERT INTO t VALUES(2)')
            with self.assertRaises(sqlite3.OperationalError):
                open_readonly(self.path, timeout=0.2)
        finally:
            writer.execute('ROLLBACK'); writer.close()


if __name__ == '__main__':
    unittest.main()

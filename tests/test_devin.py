"""Devin sessions.db collection and GetUserStatus quota polling; fixtures only."""
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from llm_usage.devin import collect_database, collect_databases, model_name, roots
from llm_usage.limits import devin_limits, poll_devin
from llm_usage.verify import reconcile
from llm_usage.store import Store

NOW = 1789453250.0


def assistant(request_id=None, model='swe-2-max', input=100, output=20, cache_read=50, cache_create=None):
    meta = {'request_id': request_id, 'generation_model': model, 'num_tokens': output,
            'metrics': {'input_tokens': input, 'output_tokens': output,
                        'cache_read_tokens': cache_read, 'cache_creation_tokens': cache_create}}
    return json.dumps({'message_id': 'm-' + (request_id or 'none'), 'role': 'assistant',
                       'content': '', 'metadata': meta})


def make_db(path, rows):
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY AUTOINCREMENT, '
                'session_id TEXT NOT NULL, node_id INTEGER NOT NULL, parent_node_id INTEGER, '
                'chat_message TEXT NOT NULL, created_at INTEGER NOT NULL, metadata TEXT)')
    con.executemany('INSERT INTO message_nodes (session_id,node_id,chat_message,created_at) '
                    'VALUES (?,?,?,?)', rows)
    con.commit()
    con.close()


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'usage.db')
        self.db = self.root / 'sessions.db'

    def events(self):
        with self.store.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM events WHERE route='devin'")]

    def test_request_id_dedupes_chain_snapshots_and_collects_subagent_rows(self):
        # One logical request is rewritten as identical chain-snapshot rows.
        make_db(self.db, [
            ('s1', 1, json.dumps({'role': 'user', 'content': 'hi', 'metadata': {}}), NOW - 100),
            ('s1', 2, assistant('req-a', input=5434, output=101, cache_read=9681), NOW - 90),
            ('s1', 3, assistant('req-a', input=5434, output=101, cache_read=9681), NOW - 60),
            ('s1', 4, assistant('req-b', model='claude-opus-5-high', input=3909, output=484,
                               cache_read=16886), NOW - 50),
            # Subagent responses land in the same table; cache fields can be null.
            ('s1', 5, assistant('req-sub', input=1976, output=323, cache_read=None), NOW - 40),
            ('s1', 6, assistant(None, input=10, output=5, cache_read=0), NOW - 30),
        ])
        errors = collect_database(self.store, self.db, lambda m: 'Cognition' if 'swe' in (m or '') else 'Anthropic')
        self.assertEqual(errors, 1)  # the request_id-less assistant row is recorded, not counted
        rows = {r['id']: r for r in self.events()}
        self.assertEqual(len(rows), 3)
        a = next(r for r in rows.values() if r['model'] == 'swe-2 (max)' and r['uncached_input'] == 5434)
        self.assertEqual((a['provider'], a['ts'], a['output'], a['cached_input']),
                         ('Cognition', NOW - 90, 101, 9681))
        b = next(r for r in rows.values() if r['model'] == 'claude-opus-5 (high)')
        self.assertEqual(b['provider'], 'Anthropic')
        sub = next(r for r in rows.values() if r['output'] == 323)
        self.assertEqual(sub['cached_input'], 0)
        with self.store.connect() as c:
            kinds = [r['kind'] for r in c.execute('SELECT kind FROM import_errors')]
            self.assertEqual(kinds, ['MissingRequestId'])

    def test_generation_model_splits_reasoning_level_suffix(self):
        self.assertEqual(model_name('swe-2-max'), 'swe-2 (max)')
        self.assertEqual(model_name('swe-1-6-fast'), 'swe-1-6 (fast)')
        self.assertEqual(model_name('claude-opus-5-high'), 'claude-opus-5 (high)')
        for unchanged in ('swe-2', 'compactor', 'gemini-3.8-flash', None):
            self.assertEqual(model_name(unchanged), unchanged)

    def test_incremental_checkpoint_keeps_first_observed_timestamp(self):
        make_db(self.db, [('s1', 1, assistant('req-a', input=100, output=10, cache_read=0), NOW)])
        collect_database(self.store, self.db, lambda m: 'Cognition')
        con = sqlite3.connect(self.db)
        con.execute('INSERT INTO message_nodes (session_id,node_id,chat_message,created_at) VALUES (?,?,?,?)',
                    ('s1', 2, assistant('req-b', input=200, output=30, cache_read=0), NOW + 10))
        con.commit()
        con.close()
        collect_database(self.store, self.db, lambda m: 'Cognition')
        rows = sorted(self.events(), key=lambda r: r['ts'])
        self.assertEqual([(r['uncached_input'], r['output'], r['ts']) for r in rows],
                         [(100, 10, NOW), (200, 30, NOW + 10)])

    def test_roots_detects_home_dbs_and_marks_missing_aggregate(self):
        home = self.root / 'home'
        target = home / '.local/share/devin/cli/sessions.db'
        target.parent.mkdir(parents=True)
        make_db(target, [('s1', 1, assistant('req-a'), NOW)])
        settings = {'sources': [], 'homes': [str(home)]}
        self.assertEqual([(n, p) for n, p in roots(settings)], [('WSL Devin', target)])
        collect_databases(self.store, settings, lambda m: 'Cognition')
        self.assertEqual(len(self.events()), 1)
        with self.store.connect() as c:
            src = {r['name']: r['status'] for r in c.execute('SELECT name,status FROM sources')}
        self.assertEqual(src['devin-records'], 'ok')
        empty = self.root / 'nowhere'
        collect_databases(self.store, {'sources': [{'name': 'Custom Devin', 'kind': 'devin',
                                                  'path': str(empty)}], 'homes': []}, lambda m: 'Cognition')
        with self.store.connect() as c:
            src = {r['name']: r['status'] for r in c.execute('SELECT name,status FROM sources')}
        self.assertEqual(src['devin-records'], 'unavailable')


    def test_recreated_database_is_rescanned_and_reconciles(self):
        make_db(self.db, [('s1', i, assistant(f'old-{i}'), NOW - 100 + i) for i in range(1, 6)])
        collect_database(self.store, self.db, lambda m: 'Cognition')
        self.db.unlink()  # reinstall: row_id restarts at 1
        make_db(self.db, [('s2', i, assistant(f'new-{i}'), NOW + i) for i in range(1, 4)])
        collect_database(self.store, self.db, lambda m: 'Cognition')
        self.assertEqual(len(self.events()), 8)
        result = reconcile(self.store.path, {'sources': [{'kind': 'devin', 'path': str(self.db)}], 'homes': []})['devin']
        self.assertEqual((result['missing_events'], result['mismatched_events'], result['source_errors']), (0, 0, 0))

    def test_replaced_database_with_more_rows_is_rescanned(self):
        make_db(self.db, [('s1', i, assistant(f'old-{i}'), NOW - 100 + i) for i in range(1, 4)])
        collect_database(self.store, self.db, lambda m: 'Cognition')
        fresh = self.root / 'fresh.db'
        make_db(fresh, [('s2', i, assistant(f'new-{i}'), NOW + i) for i in range(1, 6)])
        fresh.replace(self.db)  # new inode; row_ids 1..5 overlap the old checkpoint (3)
        collect_database(self.store, self.db, lambda m: 'Cognition')
        self.assertEqual(len(self.events()), 8)

    def test_legacy_integer_checkpoint_still_resumes(self):
        make_db(self.db, [('s1', i, assistant(f'r-{i}'), NOW + i) for i in range(1, 4)])
        with self.store.connect() as c:
            self.store.save_state(c, 'devin:' + str(self.db), 2)
        collect_database(self.store, self.db, lambda m: 'Cognition')
        self.assertEqual(len(self.events()), 1)


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'usage.db')
        self.auth = self.root / 'credentials.toml'
        self.auth.write_text('windsurf_api_key = "TEST-ONLY-PRIVATE"\n'
                             'api_server_url = "https://server.example"\n')
        self.config = {'devin_auth': str(self.auth)}

    def plan_response(self):
        return BytesIO(json.dumps({'userStatus': {'planStatus': {
            'planInfo': {'planName': 'Pro', 'billingStrategy': 'BILLING_STRATEGY_QUOTA'},
            'planStart': '2026-09-15T06:07:34Z', 'planEnd': '2026-10-15T06:07:34Z',
            'dailyQuotaRemainingPercent': 93, 'weeklyQuotaRemainingPercent': 96,
            'dailyQuotaResetAtUnix': '1789459200', 'weeklyQuotaResetAtUnix': '1789891200',
            'overageBalanceMicros': '6926546'}}}).encode())

    def test_quota_parsed_throttled_and_never_persists_credentials(self):
        with patch('llm_usage.limits.time.time', return_value=NOW), \
             patch('llm_usage.limits.urllib.request.build_opener') as factory:
            factory.return_value.open.return_value = self.plan_response()
            poll_devin(self.store, self.config)
            poll_devin(self.store, self.config)
            self.assertEqual(factory.return_value.open.call_count, 1)
            request = factory.return_value.open.call_args[0][0]
            self.assertEqual(request.full_url,
                             'https://server.example/exa.seat_management_pb.SeatManagementService/GetUserStatus')
            self.assertNotIn('TEST-ONLY-PRIVATE', json.dumps(request.headers))
        rows = {r['bucket']: r for r in self.store.limits(NOW)['limits'] if r['route'] == 'devin'}
        self.assertEqual(rows['daily']['remaining'], 93)
        self.assertEqual(rows['daily']['resets'], 1789459200)
        self.assertEqual(rows['weekly']['remaining'], 96)
        self.assertTrue(all(r['status'] == 'fresh' and r['source'] == 'devin' for r in rows.values()))
        with self.store.connect() as c:
            self.assertNotIn('TEST-ONLY-PRIVATE', '\n'.join(c.iterdump()))
            plan = self.store.state(c, 'devin:plan')
            self.assertEqual(plan['plan_name'], 'Pro')
            self.assertAlmostEqual(plan['overage_usd'], 6.926546)

    def test_missing_credentials_blocks_until_file_changes(self):
        config = {'devin_auth': str(self.root / 'absent.toml')}
        with patch('llm_usage.limits.time.time', return_value=NOW), \
             patch('llm_usage.limits.urllib.request.build_opener') as factory:
            poll_devin(self.store, config)
            poll_devin(self.store, config)
            factory.assert_not_called()
        with self.store.connect() as c:
            self.assertTrue(self.store.state(c, 'devin_status_poll')['auth_blocked'])
        row = next(r for r in self.store.limits(NOW)['limits'] if r['route'] == 'devin')
        self.assertIsNone(row['remaining'])
        self.assertEqual(row['status'], 'error')

    def test_credit_billed_plan_without_quota_fields_is_unavailable(self):
        with patch('llm_usage.limits.time.time', return_value=NOW), \
             patch('llm_usage.limits.urllib.request.build_opener') as factory:
            factory.return_value.open.return_value = BytesIO(json.dumps({'userStatus': {
                'planStatus': {'planInfo': {'planName': 'Teams',
                                            'billingStrategy': 'BILLING_STRATEGY_CREDITS'},
                               'availablePromptCredits': 120}}}).encode())
            poll_devin(self.store, self.config)
        row = next(r for r in self.store.limits(NOW)['limits'] if r['route'] == 'devin')
        self.assertIsNone(row['remaining'])
        self.assertEqual(row['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()

"""Usage that never reaches a local record file: Claude Code cloud sessions and
Codex account activity outside this PC (ChatGPT agents such as dot, Codex cloud)."""
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from llm_usage.cloud import collect_claude_cloud
from llm_usage.limits import poll_limits, read_codex
from llm_usage.store import Store, identity, stamp

NOW = stamp('2026-10-12T11:00:00Z')
SECRET = 'TRANSCRIPT-TEXT-MUST-NOT-BE-STORED'


def session(sid, kind='anthropic_cloud', updated='2026-10-12T10:00:00Z', repo='agent-browser'):
    return {'id': sid, 'environment_kind': kind, 'updated_at': updated, 'created_at': '2026-10-12T09:00:00Z',
            'title': SECRET, 'session_context': {'sources': [{'type': 'git_repository', 'url': f'https://github.com/me/{repo}'}]}}


def assistant(mid, ts, output, sidechain=False, model='claude-opus-5-5'):
    return {'event_id': mid + ts, 'payload': {'type': 'assistant', 'sessionId': 'inner-1', 'isSidechain': sidechain,
            'timestamp': ts, 'message': {'id': mid, 'model': model, 'content': [{'type': 'text', 'text': SECRET}],
            'usage': {'input_tokens': 2, 'cache_read_input_tokens': 100, 'cache_creation_input_tokens': 10,
                      'output_tokens': output}}}}


class FakeApi:
    def __init__(self, sessions, events):
        self.sessions, self.events, self.calls, self.gone = sessions, events, [], set()

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        assert 'Bearer TEST-ONLY-PRIVATE' == headers['Authorization']
        if '/v1/sessions?' in url:
            pages = self.sessions
            index = 1 if 'after_id=' in url else 0
            return {'data': pages[index], 'has_more': index + 1 < len(pages), 'last_id': pages[index][-1]['id']}
        sid = url.split('/v1/code/sessions/')[1].split('/')[0]
        if sid in self.gone:
            raise HTTPError(url, 404, 'Not Found', {}, None)
        pages = self.events[sid]
        index = 1 if 'cursor=' in url else 0
        return {'data': pages[index], 'next_cursor': 'next' if index + 1 < len(pages) else None}


class ClaudeCloudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); self.store = Store(root / 'usage.db')
        (root / '.claude').mkdir()
        self.auth = root / '.claude' / '.credentials.json'
        self.auth.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'TEST-ONLY-PRIVATE', 'expiresAt': (NOW + 3600) * 1000}}))
        (root / '.claude.json').write_text(json.dumps({'oauthAccount': {'accountUuid': 'user-1', 'organizationUuid': 'org-1'}}))
        self.config = {'claude_auth': str(self.auth)}

    def run_collect(self, api):
        with patch('llm_usage.cloud.time.time', return_value=NOW):
            collect_claude_cloud(self.store, self.config, fetch=api)

    def rows(self):
        with self.store.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM events ORDER BY ts')]

    def test_cloud_messages_are_counted_once_and_bridge_sessions_are_left_to_local_files(self):
        api = FakeApi([[session('session_cloud'), session('session_bridge', kind='bridge')]],
                      {'session_cloud': [[assistant('msg_1', '2026-10-12T09:30:00Z', 5),
                                          assistant('msg_1', '2026-10-12T09:30:00Z', 40),
                                          assistant('msg_2', '2026-10-12T09:31:00Z', 7, sidechain=True)]]})
        self.run_collect(api)
        rows = self.rows()
        self.assertEqual([(r['id'], r['output'], r['route'], r['agent_kind']) for r in rows],
                         [(identity('claude-code', 'msg_1'), 40, 'claude-code', 'main'),
                          (identity('claude-code', 'msg_2'), 7, 'claude-code', 'sub')])
        self.assertEqual((rows[0]['uncached_input'], rows[0]['cached_input'], rows[0]['cache_creation']), (2, 100, 10))
        self.assertEqual((rows[0]['provider'], rows[0]['project'], rows[0]['session']), ('Anthropic', 'agent-browser', 'inner-1'))
        self.assertFalse(any('session_bridge' in url for url, _ in api.calls))
        headers = api.calls[0][1]
        self.assertEqual((headers['x-organization-uuid'], headers['anthropic-version']), ('org-1', '2023-06-01'))
        with self.store.connect() as c:
            source = c.execute("SELECT status,detail FROM sources WHERE name='claude-cloud'").fetchone()
            dump = '\n'.join(c.iterdump())
        self.assertEqual(source['status'], 'ok')
        self.assertIn('클라우드 세션 1개', source['detail'])
        self.assertNotIn(SECRET, dump)
        self.assertNotIn('TEST-ONLY-PRIVATE', dump)

    def test_a_message_already_read_from_a_local_transcript_is_not_added_twice(self):
        with self.store.connect() as c:
            self.store.event(c, identity('claude-code', 'msg_1'), NOW - 3600, 'Anthropic', 'claude-code', 'claude-opus-5-5',
                             dict(uncached_input=2, cached_input=100, output=40, cache_creation=10), session='inner-1')
        self.run_collect(FakeApi([[session('session_cloud')]], {'session_cloud': [[assistant('msg_1', '2026-10-12T09:30:00Z', 40)]]}))
        self.assertEqual(len(self.rows()), 1)

    def test_pages_are_followed_and_an_unchanged_session_is_not_read_again(self):
        api = FakeApi([[session('session_a')], [session('session_b', updated='2026-10-12T10:05:00Z')]],
                      {'session_a': [[assistant('msg_a1', '2026-10-12T09:30:00Z', 3)], [assistant('msg_a2', '2026-10-12T09:40:00Z', 4)]],
                       'session_b': [[assistant('msg_b1', '2026-10-12T09:50:00Z', 5)]]})
        self.run_collect(api)
        self.assertEqual(len(self.rows()), 3)
        reads = len([u for u, _ in api.calls if '/teleport-events' in u])
        self.run_collect(api)
        self.assertEqual(len([u for u, _ in api.calls if '/teleport-events' in u]), reads)
        api.sessions[1][0]['updated_at'] = '2026-10-12T10:30:00Z'
        api.events['session_b'][0].append(assistant('msg_b2', '2026-10-12T10:20:00Z', 6))
        self.run_collect(api)
        self.assertEqual(len(self.rows()), 4)

    def test_expired_login_is_reported_without_a_request(self):
        self.auth.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'TEST-ONLY-PRIVATE', 'expiresAt': (NOW - 1) * 1000}}))
        api = FakeApi([[session('session_cloud')]], {})
        self.run_collect(api)
        self.assertEqual(api.calls, [])
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM sources WHERE name='claude-cloud'").fetchone()[0], 'error')

    def test_one_failing_session_keeps_the_others_and_retries_later(self):
        api = FakeApi([[session('session_a'), session('session_b')]],
                      {'session_b': [[assistant('msg_b1', '2026-10-12T09:50:00Z', 5)]]})
        self.run_collect(api)
        self.assertEqual(len(self.rows()), 1)
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM sources WHERE name='claude-cloud'").fetchone()[0], 'partial')
        api.events['session_a'] = [[assistant('msg_a1', '2026-10-12T09:30:00Z', 3)]]
        self.run_collect(api)
        self.assertEqual(len(self.rows()), 2)

    def test_a_session_without_a_transcript_is_not_read_every_cycle(self):
        api = FakeApi([[session('session_gone')]], {})
        api.gone.add('session_gone')
        self.run_collect(api)
        self.run_collect(api)
        self.assertEqual(len([u for u, _ in api.calls if '/teleport-events' in u]), 1)
        with self.store.connect() as c:
            source = c.execute("SELECT status,detail FROM sources WHERE name='claude-cloud'").fetchone()
        self.assertEqual(source['status'], 'ok'); self.assertIn('기록 없음 1개', source['detail'])


    def test_transcript_beyond_100_pages_resumes_without_false_completion(self):
        from urllib.parse import parse_qs,urlparse
        calls=[]
        def fetch(url,headers):
            if '/v1/sessions?' in url:return {'data':[session('large')]}
            if '/teleport-events?' not in url:return {'data':[]}
            cursor=int(parse_qs(urlparse(url).query).get('cursor',['0'])[0]);calls.append(cursor)
            return {'data':[assistant('page-'+str(cursor),'2026-10-12T09:30:00Z',1)],
                    'next_cursor':str(cursor+1) if cursor<100 else None}
        for _ in range(5):
            self.run_collect(fetch)
            with self.store.connect() as c:
                self.assertEqual(c.execute("SELECT status FROM sources WHERE name='claude-cloud'").fetchone()[0],'partial')
                self.assertTrue(self.store.state(c,'claude-cloud:large')['teleport_cursor'])
        self.run_collect(fetch)
        self.assertEqual(calls,list(range(101)))
        self.assertEqual(len(self.rows()),101)
        self.run_collect(fetch)
        self.assertEqual(len(calls),101)
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM sources WHERE name='claude-cloud'").fetchone()[0],'ok')


def utc(day, hour=12):
    return stamp(f'{day}T{hour:02d}:00:00Z')


class CodexAccountUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.store = Store(self.root / 'usage.db')

    def fake_app_server(self, usage_reply):
        server = self.root / 'fake-codex'
        server.write_text('#!' + sys.executable + '\n' + textwrap.dedent('''
            import json,sys
            for line in sys.stdin:
                msg=json.loads(line)
                if msg.get('id')==1:print(json.dumps({'id':1,'result':{}}),flush=True)
                elif msg.get('method')=='account/rateLimits/read':print(json.dumps({'id':msg['id'],'result':{'rateLimits':{}}}),flush=True)
                elif msg.get('method')=='account/usage/read':
                    USAGE
            ''').replace('USAGE', usage_reply))
        server.chmod(0o755)
        return str(server)

    def test_account_usage_is_read_in_the_same_app_server_session(self):
        server = self.fake_app_server("print(json.dumps({'id':msg['id'],'result':{'summary':{},'dailyUsageBuckets':[{'startDate':'2026-10-11','tokens':9}]}}),flush=True)")
        extra = {'account/usage/read': None}
        self.assertEqual(read_codex(server, timeout=5, extra=extra), {'rateLimits': {}})
        self.assertEqual(extra['account/usage/read']['dailyUsageBuckets'], [{'startDate': '2026-10-11', 'tokens': 9}])

    def test_a_silent_usage_read_does_not_hold_back_the_rate_limits(self):
        server = self.fake_app_server('pass')
        extra = {'account/usage/read': None}
        started = time.monotonic()
        self.assertEqual(read_codex(server, timeout=5, extra=extra, extra_grace=0.5), {'rateLimits': {}})
        self.assertLess(time.monotonic() - started, 4)
        self.assertIsNone(extra['account/usage/read'])

    def test_gap_is_account_total_minus_local_openai_records_per_utc_day(self):
        with self.store.connect() as c:
            for key, ts, route, out in [('a', utc('2026-10-11'), 'codex', 300), ('b', utc('2026-10-11', 23), 'codex', 100),
                                        ('c', utc('2026-10-11'), 'codex:ollama', 5000), ('d', utc('2026-10-12', 1), 'codex', 900),
                                        ('e', utc('2026-10-11'), 'claude-code', 7000)]:
                self.store.event(c, key, ts, 'OpenAI', route, 'gpt', dict(output=out))
            self.store.account_tokens(c, 'codex', [{'startDate': '2026-10-11', 'tokens': 1000},
                                                   {'startDate': '2026-10-12', 'tokens': 600}], NOW)
            gap = self.store.account_gap(c, 'codex', NOW, days=14)
        self.assertEqual(gap['days'], [dict(day='2026-10-11', account=1000, local=400, gap=600, over=0),
                                       dict(day='2026-10-12', account=600, local=900, gap=0, over=300)])
        self.assertEqual((gap['total_gap'], gap['checked']), (600, NOW))
        self.assertEqual(self.store.usage(period='7d', now=NOW)['account_gap']['total_gap'], 600)
        self.assertIsNone(self.store.usage(period='7d', now=NOW, sections='insights').get('account_gap'))

    def test_poll_records_account_buckets_and_reports_their_source(self):
        def fake(binary, timeout=35, extra=None, extra_grace=10, extra_params=None):
            extra['account/usage/read'] = {'summary': {}, 'dailyUsageBuckets': [{'startDate': '2026-10-12', 'tokens': 50}]}
            return {'rateLimits': {}}
        with patch('llm_usage.limits.poll_claude'), patch('llm_usage.limits.poll_claude_resources'), patch('llm_usage.limits.poll_devin'), patch('llm_usage.limits.collect_claude_cloud'), \
             patch('llm_usage.limits.poll_antigravity_app'), patch('llm_usage.limits.read_codex', side_effect=fake), \
             patch('llm_usage.limits.OPENCODE_GO_USAGE_URL', 'http://127.0.0.1:9/none'):
            poll_limits(self.store, {'codex_binary': 'codex', 'opencode_auth': str(self.root / 'missing.json')})
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT route,day,tokens FROM account_token_buckets').fetchall()[0][:], ('codex', '2026-10-12', 50))
            row = c.execute("SELECT status FROM sources WHERE name='codex-account-usage'").fetchone()
        self.assertEqual(row[0], 'ok')

    def poll_fake(self, fake):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name in ('poll_claude','poll_claude_resources','poll_devin','collect_claude_cloud','poll_antigravity_app','poll_codex_thread'):
                stack.enter_context(patch('llm_usage.limits.'+name))
            stack.enter_context(patch('llm_usage.limits.read_codex',side_effect=fake))
            poll_limits(self.store,{'codex_binary':'fixture','opencode_auth':str(self.root/'absent')})

    def test_quota_failures_preserve_usage_and_do_not_bind_previous_login(self):
        from llm_usage.resources import observe_account
        with self.store.connect() as c:
            observe_account(c,'codex',('old',None),time.time())
            self.store.account_tokens(c,'codex',[dict(startDate='2026-10-11',tokens=500)],NOW)
        for mode in ('rpc','parser'):
            with self.subTest(mode=mode):
                def fake(binary,**kw):
                    kw['extra']['account/usage/read']={'dailyUsageBuckets':[dict(startDate='2026-10-12',tokens=50)]}
                    kw['extra']['account/read']={'workspaceRouting':{'chatgptAccountId':'new'}}
                    if mode=='rpc':raise RuntimeError('quota failed')
                    return {'accountId':'new','rateLimitsByLimitId':[],'rateLimits':'malformed'}
                self.poll_fake(fake)
                with self.store.connect() as c:
                    gap=self.store.account_gap(c,'codex',NOW)
                    self.assertEqual([(r['day'],r['account']) for r in gap['days']],[('2026-10-12',50)])
                    self.assertEqual(c.execute("SELECT status FROM sources WHERE name='codex-account-usage'").fetchone()[0],'ok')
                    self.assertEqual(c.execute("SELECT status FROM sources WHERE name='codex'").fetchone()[0],'error')

    def test_empty_and_sparse_new_account_do_not_reuse_old_totals(self):
        from llm_usage.resources import observe_account
        from llm_usage.limits import record_codex_account
        from llm_usage.reconciliation import supplements
        for who,buckets in [('old',[dict(startDate='2026-10-11',tokens=500)]),('new',[])]:
            with self.store.connect() as c:observe_account(c,'codex',(who,None),time.time())
            record_codex_account(self.store,{'dailyUsageBuckets':buckets})
        with self.store.connect() as c:
            self.assertIsNone(self.store.account_gap(c,'codex',NOW))
            self.assertEqual(supplements(c)[0],[])
        record_codex_account(self.store,{'dailyUsageBuckets':[dict(startDate='2026-10-12',tokens=50)]})
        with self.store.connect() as c:
            self.assertEqual([r['day'] for r in self.store.account_gap(c,'codex',NOW)['days']],['2026-10-12'])
            self.assertEqual(sum(r['tokens'] for r in supplements(c)[0]),50)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM account_token_buckets').fetchone()[0],2)
            observe_account(c,'codex',('old',None),time.time())
            self.assertEqual(self.store.account_gap(c,'codex',NOW)['days'][0]['account'],500)

    def test_unknown_account_epoch_is_stable_and_changes_with_native_login(self):
        def poll(email):
            def fake(binary,**kw):
                kw['extra']['account/read']={'account':{'type':'chatgpt','email':email}}
                kw['extra']['account/usage/read']={'dailyUsageBuckets':[]}
                return {'rateLimits':{}}
            self.poll_fake(fake)
            with self.store.connect() as c:return self.store.account_namespace(c,'codex')
        first=poll('first@example.test')
        self.assertEqual(first,poll('first@example.test'))
        self.assertNotEqual(first,poll('second@example.test'))
        with self.store.connect() as c:self.assertNotIn('example.test','\n'.join(c.iterdump()))

    def test_legacy_migration_retains_but_does_not_assign_history(self):
        import sqlite3
        from contextlib import closing
        from llm_usage.resources import observe_account
        from llm_usage.reconciliation import supplements
        path=self.root/'legacy.db'
        with closing(sqlite3.connect(path)) as c:
            c.execute('CREATE TABLE account_tokens(route TEXT,day TEXT,tokens INTEGER,checked REAL,PRIMARY KEY(route,day))')
            c.execute('INSERT INTO account_tokens VALUES (?,?,?,?)',('codex','2026-10-11',500,NOW))
            c.commit()
        migrated=Store(path);Store(path)
        with migrated.connect() as c:
            self.assertEqual(migrated.account_gap(c,'codex',NOW)['total_gap'],500)
            observe_account(c,'codex',('new',None),time.time())
            self.assertIsNone(migrated.account_gap(c,'codex',NOW))
            self.assertEqual(supplements(c)[0],[])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM account_token_buckets').fetchone()[0],1)

    def test_rate_error_waits_for_usage_in_either_reply_order(self):
        for order in ('before','after'):
            with self.subTest(order=order):
                server=Path(self.fake_app_server("print(json.dumps({'id':msg['id'],'result':{'dailyUsageBuckets':[]}}),flush=True)"))
                s=server.read_text().replace("print(json.dumps({'id':msg['id'],'result':{'rateLimits':{}}}),flush=True)",
                    "print(json.dumps({'id':2,'error':{'code':-1}}),flush=True)" if order=='before' else 'pass')
                if order=='after':s=s.replace("{'dailyUsageBuckets':[]}}),flush=True)","{'dailyUsageBuckets':[]}}),flush=True); print(json.dumps({'id':2,'error':{'code':-1}}),flush=True)")
                server.write_text(s)
                extra={'account/usage/read':None}
                with self.assertRaises(RuntimeError):read_codex(str(server),timeout=5,extra=extra,extra_grace=.5)
                self.assertEqual(extra['account/usage/read'],{'dailyUsageBuckets':[]})



if __name__ == '__main__':
    unittest.main()

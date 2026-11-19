"""Aside sessions: requests of the ChatGPT (Codex) and Claude subscriptions that Aside makes
itself are stored under those subscriptions' routes, once each, and nothing but numbers is kept."""

import json
from pathlib import Path
import tempfile
import time
import unittest

from llm_usage import aside
from llm_usage.collect import collect_local
from llm_usage.store import Store, identity, stamp
from llm_usage.verify import failed, partial, reconcile

CONVERSATION = 'CONVERSATION-TEXT-MUST-NOT-BE-STORED'
T0 = stamp('2026-11-09T03:00:00Z')


def response(rid, provider='openai-codex', model='gpt-test', at=0, **usage):
    counts = {'input': 100, 'output': 20, 'cacheRead': 1000, 'cacheWrite': 0, **usage}
    counts['totalTokens'] = sum(counts[k] for k in ('input', 'output', 'cacheRead', 'cacheWrite'))
    return {
        'role': 'assistant',
        'content': [{'type': 'text', 'text': CONVERSATION}],
        'api': 'fixture',
        'provider': provider,
        'model': model,
        'usage': counts,
        'thinkingLevel': 'high',
        'timestamp': int((T0 + at) * 1000),
        'responseId': rid,
    }


class AsideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.sessions = self.home / '.aside' / 'u' / '0' / 'sessions'
        self.store = Store(self.root / 'usage.db')
        self.settings = {'database': str(self.root / 'usage.db'), 'homes': [str(self.home)], 'sources': []}

    def write(self, session, lines, mode='w'):
        folder = self.sessions / session
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / 'messages.jsonl').open(mode) as f:
            f.writelines(json.dumps(line) + '\n' for line in lines)

    def collect(self):
        collect_local(self.store, self.settings, include_derived=False)

    def events(self):
        with self.store.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM events ORDER BY ts,id')]

    def source(self):
        with self.store.connect() as c:
            return tuple(c.execute("SELECT status,detail FROM sources WHERE name='WSL Aside'").fetchone())

    def test_requests_land_on_the_subscription_they_spend(self):
        self.write(
            '2026-11-09_abc',
            [
                {'role': 'user', 'content': CONVERSATION, 'timestamp': 1},
                response('resp_1', reasoning=5),
                {'role': 'toolResult', 'content': CONVERSATION, 'toolName': 'browser', 'timestamp': 2},
                response('msg_1', 'claude-code', 'claude-test', at=60, cacheWrite=300, cacheWrite1h=100, reasoning=8),
                response('other', 'openrouter', 'some-model', at=120),  # not a subscription tracked here
            ],
        )
        self.collect()
        codex, claude = self.events()
        self.assertEqual(
            (codex['route'], codex['provider'], codex['model'], codex['effort'], codex['project'], codex['agent_kind']),
            ('codex', 'OpenAI', 'gpt-test', 'high', 'Aside', 'main'),
        )
        self.assertEqual(
            [
                codex[k]
                for k in (
                    'uncached_input',
                    'cached_input',
                    'output',
                    'cache_creation',
                    'reasoning',
                    'reasoning_observed',
                )
            ],
            [100, 1000, 20, 0, 5, 1],
        )
        self.assertEqual(
            (claude['route'], claude['provider'], claude['id']),
            ('claude-code', 'Anthropic', identity('claude-code', 'msg_1')),
        )
        self.assertEqual(
            [claude[k] for k in ('cache_creation', 'cache_creation_1h', 'cache_creation_5m', 'cache_ttl_observed')],
            [300, 100, 200, 1],
        )
        # One opaque session per folder, and the origin is kept apart from the local records.
        self.assertEqual(codex['session'], claude['session'])
        self.assertTrue(codex['session'].startswith('aside-'))
        self.assertNotIn('abc', codex['session'])
        with self.store.connect() as c:
            origins = c.execute('SELECT source,COUNT(*) FROM usage_origins GROUP BY 1').fetchall()
        self.assertEqual([tuple(r) for r in origins], [('aside', 2)])
        self.assertEqual(self.source(), ('ok', '1개 기록 파일 확인 · 해석 실패 0건'))
        with self.store.connect() as c:
            self.assertNotIn(CONVERSATION, '\n'.join(c.iterdump()))
        self.assertTrue(self.store.rollup_matches())

    def test_a_replayed_response_counts_once_and_new_lines_are_read_incrementally(self):
        first = response('resp_1')
        self.write('s1', [first, response('resp_2', at=10), first])
        self.write('s2', [first])  # a branch of the session repeats the response in another file
        self.collect()
        self.assertEqual(len(self.events()), 2)
        self.write('s1', [response('resp_3', at=20)], mode='a')
        self.collect()
        self.assertEqual(sorted(e['ts'] - T0 for e in self.events()), [0, 10, 20])
        self.assertEqual(self.store.usage(period='all')['totals']['requests'], 3)
        self.assertTrue(self.store.rollup_matches())

    def test_a_response_without_an_identity_is_its_sessions_line_at_that_time(self):
        cut = response(None, at=5)
        self.write('s1', [cut, cut, response(None, at=6)])
        self.collect()
        self.assertEqual(len(self.events()), 2)

    def test_an_unreadable_line_is_counted_and_the_rest_is_kept(self):
        bad = response('resp_bad', at=1)
        bad['usage']['output'] = 'many'
        early = response('resp_time', at=2)
        early['timestamp'] = 'yesterday'
        self.write('s1', [response('resp_1'), bad, early, response('resp_2', at=3, reasoning=50)])
        self.collect()
        # Reasoning above the output is refused as well: three lines are counted, one is stored.
        self.assertEqual([e['output'] for e in self.events()], [20])
        self.assertEqual(self.source(), ('partial', '1개 기록 파일 확인 · 해석 실패 3건'))

    def test_daily_check_reads_the_session_files_apart_from_the_collector(self):
        self.write(
            's1',
            [
                response('resp_1', reasoning=5),
                response('msg_1', 'claude-code', 'claude-test', at=60, cacheWrite=300, cacheWrite1h=100),
                response('resp_1', reasoning=5),  # replayed after a compaction: one request
                response(None, at=90),
                response('other', 'openrouter', 'some-model', at=120),
            ],
        )
        self.write('s2', [response('resp_1', reasoning=5)])  # a branch: the response again, in another folder
        self.collect()
        result = reconcile(self.store.path, self.settings)
        self.assertEqual(
            {k: result['aside'][k] for k in ('source_files', 'requests', 'matched_requests', 'missing_events')},
            {'source_files': 2, 'requests': 3, 'matched_requests': 3, 'missing_events': 0},
        )
        self.assertFalse(failed(result))
        self.assertFalse(partial(result))
        # Lines written after the collector's checkpoint wait for it, not counted as missing.
        self.write('s1', [response('resp_2', at=150)], mode='a')
        result = reconcile(self.store.path, self.settings)
        self.assertEqual((result['aside']['pending_files'], result['aside']['missing_events']), (1, 0))
        self.assertTrue(partial(result))
        self.collect()
        # A stored request that is lost, or that disagrees with its line, fails the check.
        with self.store.connect() as c:
            c.execute('DELETE FROM events WHERE id=?', (identity('claude-code', 'msg_1'),))
        result = reconcile(self.store.path, self.settings)
        self.assertEqual((result['aside']['missing_events'], result['aside']['mismatched_events']), (1, 0))
        self.assertTrue(failed(result))
        with self.store.connect() as c:
            c.execute("UPDATE events SET output=output-1 WHERE route='codex' AND output>0")
        result = reconcile(self.store.path, self.settings)
        self.assertEqual(result['aside']['mismatched_events'], 3)
        with self.store.connect() as c:
            self.store.save_state(c, 'verify', dict(ts=time.time(), ok=not failed(result), **result))
        source = next(s for s in self.store.usage(period='all')['sources'] if s['name'] == '원본 대조')
        self.assertEqual(source['status'], 'error')
        self.assertIn('누락 1건 · 불일치 3건', source['detail'])

    def test_claude_copy_keeps_transcript_attribution_in_either_order_and_after_upgrade(self):
        from llm_usage import claude_attribution
        from llm_usage.claude_usage import prepare_replay
        from llm_usage.collect import PARSER_VERSION, collect_file
        from unittest.mock import patch
        from test_usage_details import calls_message

        root = self.root / 'transcripts'
        root.mkdir()
        path = root / 'archive.jsonl'
        original = calls_message()
        path.write_text(json.dumps(original) + '\n')
        self.settings['sources'] = [dict(name='Claude', kind='jsonl', route='claude-code', path=str(root))]
        self.write('s1', [response('parent', 'claude-code', 'claude-alias', input=100, cacheRead=0, output=40)])
        aside_path = self.sessions / 's1' / 'messages.jsonl'
        for order in ('aside-first', 'transcript-first', 'upgrade'):
            with self.subTest(order=order):
                self.store = Store(self.root / (order + '.db'))
                self.settings['database'] = self.store.path
                account_key = 'b' * 64
                account_route = 'claude-code:' + account_key[:12]
                with self.store.connect() as c:
                    c.execute(
                        'INSERT INTO claude_accounts VALUES (?,?,?,?,?)', (account_route, account_key, 1, 1, '{}')
                    )
                if order == 'aside-first':
                    collect_file(self.store, aside_path, 'aside')
                if order == 'upgrade':
                    collect_file(self.store, path, 'claude-code')
                    collect_file(self.store, aside_path, 'aside')
                    with self.store.connect() as c:
                        c.execute(
                            "UPDATE events SET session=?,project='Aside',account=? WHERE requests=1",
                            (aside.session_id(aside_path), account_route),
                        )
                        c.execute(
                            "INSERT INTO claude_evidence VALUES (?,?,?,'aside')", ('detail-session', T0, account_key)
                        )
                        key = 'file:' + str(path)
                        self.store.save_state(c, key, dict(self.store.state(c, key), parser_version=PARSER_VERSION))
                    prepare_replay(self.store)
                self.collect()
                self.assertEqual(
                    aside.attribute(self.store, {**self.settings, 'aside_claude_account': account_route}), 0
                )
                with patch.object(claude_attribution, 'home_candidates', return_value=[]):
                    claude_attribution.resolve(self.store, self.settings)
                rows = sorted(self.events(), key=lambda row: row['requests'], reverse=True)
                self.assertEqual(
                    [(r['requests'], r['advisor_calls'], r['session'], r['project']) for r in rows],
                    [(1, 0, 'detail-session', 'project'), (0, 1, 'detail-session', 'project')],
                )
                self.assertEqual(rows[0]['model'], 'claude-parent')
                self.assertEqual([r['ts'] for r in rows], [stamp(original['timestamp'])] * 2)
                self.assertIsNone(rows[0]['effort'])
                self.assertEqual([r['account'] for r in rows], ['claude-code', 'claude-code'])
                with self.store.connect() as c:
                    self.assertIsNone(c.execute("SELECT 1 FROM claude_evidence WHERE kind='aside'").fetchone())
                    self.assertEqual(
                        {
                            r[0]
                            for r in c.execute('SELECT source FROM usage_origins WHERE event_id=?', (rows[0]['id'],))
                        },
                        {'local', 'aside'},
                    )
                self.assertTrue(self.store.rollup_matches())
                self.assertFalse(failed(reconcile(self.store.path, self.settings)))

    def test_verification_uses_reasoning_observed_at_the_largest_output(self):
        older = response('r', reasoning=5)
        newer = response('r', output=30)
        for rid, records in (('forward', (older, newer)), ('backward', (newer, older))):
            self.write(rid, [{**record, 'responseId': rid} for record in records])
        self.collect()
        result = reconcile(self.store.path, self.settings)
        self.assertEqual(result['aside']['matched_requests'], 2)
        self.assertFalse(failed(result))

    def test_verification_distinguishes_conflicted_and_replaced_cache_splits(self):
        first = response('r', cacheWrite=300, cacheWrite1h=100)
        second = response('r', cacheWrite=300, cacheWrite1h=200)
        self.write('s', [first, second])
        self.collect()
        result = reconcile(self.store.path, self.settings)
        self.assertEqual(result['aside']['detail_conflicts'], 1)
        self.assertTrue(partial(result))
        self.assertFalse(failed(result))
        with self.store.connect() as c:
            c.execute("""UPDATE events SET cache_creation_5m=200, cache_creation_1h=100,
                cache_creation_unknown=0, cache_ttl_observed=1, cache_ttl_conflict=0""")
        self.assertTrue(failed(reconcile(self.store.path, self.settings)))
        self.write('s', [response('r', cacheWrite=400, cacheWrite1h=100)], mode='a')
        self.collect()
        result = reconcile(self.store.path, self.settings)
        self.assertEqual(result['aside']['matched_requests'], 1)
        self.assertFalse(partial(result))

    def test_verification_reports_unsupported_files_and_invalid_timestamps_as_partial(self):
        import gzip

        upper = aside.TIMESTAMP_LIMIT
        stamps = (upper - 1, upper, 0, float('nan'), float('inf'))
        self.write('s', [{**response(f'r{i}'), 'timestamp': ts} for i, ts in enumerate(stamps)])
        (self.sessions / 's' / 'old.jsonl.gz').write_bytes(gzip.compress(b'{}\n'))
        self.collect()
        result = reconcile(self.store.path, self.settings)
        counts = result['aside']
        self.assertEqual(
            (counts['matched_requests'], counts['unreadable_records'], counts['unsupported_files']), (1, 4, 1)
        )
        self.assertTrue(partial(result))
        self.assertFalse(failed(result))

    def test_claude_requests_take_the_account_the_setting_names(self):
        from unittest.mock import patch

        from llm_usage import claude_attribution
        from llm_usage.claude_accounts import UNKNOWN

        first, second = 'a' * 64, 'b' * 64
        other = 'claude-code:' + second[:12]
        self.write('s1', [response('msg_1', 'claude-code', 'claude-test'), response('resp_1', at=5)])
        self.collect()
        with self.store.connect() as c:
            c.executemany(
                'INSERT INTO claude_accounts VALUES (?,?,?,?,?)',
                [('claude-code', first, 1, 1, '{}'), (other, second, 1, 1, '{}')],
            )
            self.store.save_state(c, 'claude_primary_account', first)
            c.execute("UPDATE events SET account=? WHERE route='claude-code'", (UNKNOWN,))  # as the resolver left it

        def resolve(settings):
            marked = aside.attribute(self.store, settings)
            with patch.object(claude_attribution, 'home_candidates', return_value=[]):
                claude_attribution._resolve(self.store, settings)
            with self.store.connect() as c:
                return marked, dict(c.execute('SELECT route,account FROM events'))

        # No setting: nothing is claimed. The Codex request is never touched.
        self.assertEqual(resolve(self.settings), (0, {'claude-code': UNKNOWN, 'codex': None}))
        self.assertEqual(
            resolve({**self.settings, 'aside_claude_account': 'claude-code'}),
            (1, {'claude-code': 'claude-code', 'codex': None}),
        )
        self.assertEqual(resolve({**self.settings, 'aside_claude_account': 'claude-code'})[0], 0)  # settled
        # Another login: the earlier evidence goes, so the request is not ambiguous.
        self.assertEqual(resolve({**self.settings, 'aside_claude_account': other})[1]['claude-code'], other)
        # An account this dashboard does not know is said, and nothing is claimed.
        self.assertEqual(resolve({**self.settings, 'aside_claude_account': 'claude-code:000000000000'})[0], 0)
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM sources WHERE name='Aside 계정'").fetchone()[0], 'error')
        resolve({**self.settings, 'aside_claude_account': other})
        with self.store.connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM sources WHERE name='Aside 계정'").fetchone())

    def test_only_selected_homes_and_explicit_sources_are_read(self):
        self.write('s1', [response('resp_1')])
        other = self.root / 'elsewhere' / 'sessions'
        (other / 's9').mkdir(parents=True)
        (other / 's9' / 'messages.jsonl').write_text(json.dumps(response('resp_9')) + '\n')
        self.assertEqual(aside.roots({'homes': [], 'sources': []}), [])
        self.assertEqual([name for name, _ in aside.roots(self.settings)], ['WSL Aside'])
        explicit = {
            'homes': [str(self.home)],
            'sources': [dict(name='Lab Aside', kind='aside', route='aside', path=str(other))],
        }
        self.assertEqual([name for name, _ in aside.roots(explicit)], ['Lab Aside', 'WSL Aside'])
        # A second profile of the same home is its own source.
        (self.home / '.aside' / 'u' / '1' / 'sessions').mkdir(parents=True)
        self.assertEqual([name for name, _ in aside.roots(self.settings)], ['WSL Aside', 'WSL Aside 2'])


if __name__ == '__main__':
    unittest.main()

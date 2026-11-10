"""Reasoning effort is a dimension of recorded use: read from each tool's own records,
never guessed, and absent (not 'low' or 0) where a record does not state it."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from llm_usage.claude_usage import ingest_message
from llm_usage.collect import collect_file
from llm_usage.store import Store, model_effort, stamp

NOW = stamp('2026-11-10T03:00:00Z')
PRICING = {'gpt-test': dict(input=1, output=10), 'claude-test': dict(input=3, output=15), 'swe-x': dict(input=3, output=15)}


def claude(message, effort=None, output=10, **usage):
    record = {'type': 'assistant', 'timestamp': '2026-11-10T01:00:00Z', 'requestId': 'r-'+message, 'sessionId': 's', 'cwd': '/nowhere/p',
              'message': {'id': message, 'model': 'claude-test', 'usage': {'input_tokens': 5, 'output_tokens': output, **usage}}}
    if effort is not None:
        record['effort'] = effort
    return record


class EffortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.store = Store(self.root/'usage.db')

    def efforts(self, where='1=1'):
        with self.store.connect() as c:
            return {r['id']: (r['effort'], r['speed']) for r in c.execute('SELECT id,effort,speed FROM events WHERE '+where)}

    def test_model_names_that_carry_an_effort(self):
        self.assertEqual([model_effort(m) for m in ('swe-2 (max)', 'claude-sonnet-5-5-high', 'gemini-3.1-pro-low', 'gpt-6-astra', 'gemini-3.8-flash-n', None)],
                         ['max', 'high', 'low', None, None, None])

    def test_claude_records_state_effort_and_speed_and_a_replay_without_them_keeps_them(self):
        with self.store.connect() as c:
            ingest_message(self.store, c, 'claude-code', claude('m1', 'xhigh', speed='fast'))
            ingest_message(self.store, c, 'claude-code', claude('m2'))
            ingest_message(self.store, c, 'claude-code', claude('m3', {'not': 'a level'}))
            ingest_message(self.store, c, 'claude-code', claude('m1'))  # an older copy of the same response
        self.assertEqual(sorted(self.efforts().values(), key=repr), [('xhigh', 'fast'), (None, None), (None, None)])

    def test_codex_requests_take_the_effort_of_their_turn(self):
        def count(ts, total, last):
            usage = lambda n: {'input_tokens': n, 'cached_input_tokens': 0, 'output_tokens': n//10, 'reasoning_output_tokens': 0}
            return {'type': 'event_msg', 'timestamp': ts, 'payload': {'type': 'token_count', 'info': {'total_token_usage': usage(total), 'last_token_usage': usage(last)}}}
        lines = [{'type': 'session_meta', 'payload': {'id': 'codex-session', 'model_provider': 'openai'}},
                 {'type': 'turn_context', 'timestamp': '2026-11-10T01:00:00Z', 'payload': {'model': 'gpt-test'}},
                 count('2026-11-10T01:00:05Z', 100, 100),
                 {'type': 'turn_context', 'timestamp': '2026-11-10T01:10:00Z', 'payload': {'model': 'gpt-test', 'effort': 'low'}},
                 count('2026-11-10T01:10:05Z', 300, 200),
                 {'type': 'turn_context', 'timestamp': '2026-11-10T01:20:00Z', 'payload': {'model': 'gpt-test', 'effort': 'max'}},
                 count('2026-11-10T01:20:05Z', 700, 400)]
        path = self.root/'rollout.jsonl'; path.write_text(''.join(json.dumps(line)+'\n' for line in lines))
        collect_file(self.store, path, 'codex')
        with self.store.connect() as c:
            got = [(r['uncached_input'], r['effort']) for r in c.execute('SELECT uncached_input,effort FROM events ORDER BY ts')]
        self.assertEqual(got, [(100, None), (200, 'low'), (400, 'max')])
        # Read again by a newer parser, the stored requests keep and regain their turn's effort.
        with self.store.connect() as c:
            c.execute('UPDATE events SET effort=NULL'); c.execute('DELETE FROM session_effort')
            c.execute("UPDATE state SET data=json_set(data,'$.parser_version',0) WHERE key LIKE 'file:%'")
        collect_file(self.store, path, 'codex')
        with self.store.connect() as c:
            self.assertEqual([r[0] for r in c.execute('SELECT effort FROM events ORDER BY ts')], [None, 'low', 'max'])
        self.assertTrue(self.store.rollup_matches())

    def test_usage_reports_volume_per_effort_and_groups_by_it(self):
        with self.store.connect() as c:
            for i in range(4):
                ingest_message(self.store, c, 'claude-code', {**claude(f'h{i}', 'high', output=100), 'timestamp': '2026-11-10T01:00:00Z'})
            for i in range(2):
                ingest_message(self.store, c, 'claude-code', {**claude(f'x{i}', 'max', output=500), 'timestamp': '2026-11-10T01:30:00Z'})
            ingest_message(self.store, c, 'claude-code', claude('plain', output=7))
            self.store.event(c, 'd1', NOW-3600, 'Cognition', 'devin', 'swe-x (max)', dict(uncached_input=1000, output=50))
        usage = self.store.usage(period='today', group='effort', pricing=PRICING, sections='core', now=NOW)
        rows = {(e['model'], e['effort']): e for e in usage['efforts']}
        self.assertEqual({k: (v['requests'], v['output']) for k, v in rows.items()},
                         {('claude-test', 'high'): (4, 400), ('claude-test', 'max'): (2, 1000), ('claude-test', None): (1, 7), ('swe-x', 'max'): (1, 50)})
        self.assertAlmostEqual(rows[('claude-test', 'max')]['cost'], 2*(5*3+500*15)/1e6)
        self.assertEqual(sum(e['tokens'] for e in usage['efforts']), sum(usage['totals'][k] for k in ('uncached_input', 'cached_input', 'output', 'cache_creation')))
        self.assertEqual(set(usage['labels']), {'high', 'max', '미기록'})
        self.assertTrue(self.store.rollup_matches())

    def test_an_older_database_gains_the_columns_and_names_already_tell_the_effort(self):
        path = self.root/'old.db'; Store(path)
        con = sqlite3.connect(path)
        con.execute("INSERT INTO events (id,ts,provider,route,model,uncached_input,cached_input,output,cache_creation,reasoning) VALUES ('a',?,'Cognition','devin','swe-x (high)',1,0,1,0,0)", (NOW-60,))
        con.execute("INSERT INTO events (id,ts,provider,route,model,uncached_input,cached_input,output,cache_creation,reasoning) VALUES ('b',?,'OpenAI','codex','gpt-test',1,0,1,0,0)", (NOW-60,))
        for statement in ('DROP TRIGGER events_rollup_insert', 'DROP TRIGGER events_rollup_update', 'DROP TRIGGER events_rollup_delete', 'DROP TABLE usage_hourly',
                          'ALTER TABLE events DROP COLUMN effort', 'ALTER TABLE events DROP COLUMN speed', "UPDATE state SET data='3' WHERE key='rollup_version'"):
            con.execute(statement)
        con.commit(); con.close()
        store = Store(path)
        with store.connect() as c:
            self.assertEqual(dict(c.execute('SELECT id,effort FROM events')), {'a': 'high', 'b': None})
        self.assertTrue(store.rollup_matches())


if __name__ == '__main__':
    unittest.main()

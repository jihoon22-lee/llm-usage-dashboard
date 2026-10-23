"""Canonical per-response usage alongside legacy cumulative observations."""
import json
from pathlib import Path
import tempfile
import unittest

from llm_usage.collect import collect_file, collect_local, prepare_normalization
from llm_usage.record_files import zstd
from llm_usage.verify import _codex
from llm_usage.store import Store
from llm_usage.telemetry import ingest, resolve


def usage(inp, out, cached=0, creation=0, reasoning=0):
    return dict(input_tokens=inp, output_tokens=out, cached_input_tokens=cached,
                cache_write_input_tokens=creation, reasoning_output_tokens=reasoning,
                total_tokens=inp+out)


def meta(session='native', **extra):
    return dict(type='session_meta', payload=dict(id=session, session_id='transport-session',
                model_provider='openai', cwd='/fixture/project', **extra))


def model(name='gpt-test', ts='2026-10-23T01:00:00Z'):
    return dict(type='turn_context', timestamp=ts, payload=dict(model=name))


def response(rid, nums, total=None, ts='2026-10-23T01:00:01Z'):
    return dict(type='token_usage_record', timestamp=ts, payload=dict(response_id=rid,
                session_id='transport-session', usage=nums, thread_token_usage=total or nums))


def point(total, last=None, ts='2026-10-23T01:00:01Z'):
    return dict(type='event_msg', timestamp=ts, payload=dict(type='token_count',
                info=dict(total_token_usage=total, last_token_usage=total if last is None else last)))


class NativeCodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'usage.db');self.path=self.root/'native.jsonl'

    def collect(self, rows, *, path=None, append=False):
        path=path or self.path
        with path.open('a' if append else 'w') as f:
            f.write(''.join(json.dumps(row)+'\n' for row in rows))
        return collect_file(self.store, path, 'codex')

    def totals(self):
        with self.store.connect() as c:
            return tuple(c.execute('SELECT COUNT(*),COALESCE(SUM(uncached_input+cached_input+cache_creation+output),0) FROM events').fetchone())

    def test_response_proof_recovers_consumption_hidden_by_a_repeated_baseline(self):
        rows=[meta(),model()]
        for i,(part,cumulative,legacy,last) in enumerate([
                (usage(100,20),usage(100,20),usage(100,20),usage(100,20)),
                (usage(300,50),usage(400,70),usage(100,20),usage(0,0)),
                (usage(10,2),usage(410,72),usage(110,22),usage(10,2))]):
            ts=f'2026-10-23T01:00:0{i+1}Z'
            rows.extend([response(f'r{i}',part,cumulative,ts),point(legacy,last,ts)])
        self.assertEqual(self.collect(rows),0)
        self.assertEqual(self.totals(),(3,482))
        self.collect(rows,path=self.root/'copy.jsonl')
        self.store=Store(self.root/'usage.db')
        collect_file(self.store,self.path,'codex')
        self.assertEqual(self.totals(),(3,482))
        self.assertTrue(self.store.rollup_matches())
        with self.store.connect() as c:
            self.assertEqual({r[0] for r in c.execute('SELECT DISTINCT session FROM events')},{'native'})

    def test_legacy_only_history_survives_a_partial_native_upgrade_and_replay(self):
        old=[meta(),model(),point(usage(100,20))]
        self.collect(old)
        self.collect([response('second',usage(10,2),usage(110,22),'2026-10-23T01:01:00Z'),
                      point(usage(110,22),usage(10,2),'2026-10-23T01:01:00Z')],append=True)
        self.assertEqual(self.totals(),(2,132))
        self.collect(old+[point(usage(110,22),usage(10,2),'2026-10-23T01:01:00Z')],path=self.root/'legacy-copy.jsonl')
        self.assertEqual(self.totals(),(2,132))

    def test_a_pending_response_survives_restart_and_late_legacy_delivery(self):
        self.collect([meta(),model(),response('one',usage(100,20))])
        self.assertEqual(self.totals(),(1,120))
        self.store=Store(self.root/'usage.db')
        self.collect([point(usage(100,20))],append=True)
        self.assertEqual(self.totals(),(1,120))

    def test_pre_v3_normalization_copies_native_evidence_before_accepting_checkpoints(self):
        self.collect([meta(),model(),point(usage(100,20))])
        with self.store.connect() as c:self.store.save_state(c,'normalized_version',2)
        rows=[meta(),model(),response('one',usage(100,20)),point(usage(100,20))]
        self.path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        cfg={'sources':[dict(name='native',path=str(self.root),kind='jsonl',route='codex')]}
        self.assertTrue(prepare_normalization(self.store,cfg))
        self.assertEqual(self.totals(),(1,120))
        with self.store.connect() as c:
            for table in ('codex_responses','codex_response_links','usage_aliases'):
                self.assertEqual(c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],1,table)
            self.assertEqual(self.store.state(c,'normalized_version'),4)
        collect_local(self.store,cfg,include_derived=False)
        self.assertTrue(prepare_normalization(self.store,cfg))
        self.assertEqual(self.totals(),(1,120));self.assertTrue(self.store.rollup_matches())

    def test_rebuilding_native_events_preserves_the_historical_project_label(self):
        self.collect([meta(),model(),response('one',usage(100,20)),point(usage(100,20))])
        with self.store.connect() as c:c.execute("UPDATE events SET project='historical-project'")
        self.collect([response('two',usage(10,2),usage(110,22),'2026-10-23T02:00:00Z'),
                      point(usage(110,22),usage(10,2),'2026-10-23T02:00:00Z')],append=True)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT project FROM events ORDER BY ts LIMIT 1').fetchone()[0],'historical-project')
    def test_a_duplicate_counter_notification_does_not_keep_the_legacy_copy(self):
        self.collect([meta(),model(),point(usage(100,20)),
                      response('one',usage(100,20)),
                      point(usage(100,20),ts='2026-10-23T01:00:02Z')])
        self.assertEqual(self.totals(),(1,120))

    def test_new_response_at_repeated_baseline_keeps_an_unlinked_older_request(self):
        self.collect([meta(),model(),point(usage(100,20)),
                      response('new',usage(300,50),usage(400,70),'2026-10-23T01:00:02Z'),
                      point(usage(100,20),usage(0,0),'2026-10-23T01:00:02Z')])
        self.assertEqual(self.totals(),(2,470))

    def test_conflicting_response_identity_is_reported_without_overwriting_original(self):
        rows=[meta(),model(),response('one',usage(100,20)),point(usage(100,20))]
        self.collect(rows)
        self.assertEqual(self.collect([response('one',usage(200,30))],append=True),1)
        self.assertEqual(self.totals(),(1,120))

    def test_compressed_only_import_copy_and_replacement(self):
        folder=self.root/'sources';folder.mkdir();path=folder/'record.jsonl.zst'
        rows=[meta(),model(),response('one',usage(100,20)),point(usage(100,20))]
        def write(values):
            with zstd.open(path,'wt') as f:f.write(''.join(json.dumps(r)+'\n' for r in values))
        cfg={'sources':[dict(name='compressed',path=str(folder),kind='jsonl',route='codex')]}
        write(rows);collect_local(self.store,cfg,include_derived=False)
        self.assertEqual(self.totals(),(1,120))
        (folder/'copy.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
        collect_local(self.store,cfg,include_derived=False)
        self.assertEqual(self.totals(),(1,120))
        write(rows+[response('second',usage(10,2),usage(110,22),'2026-10-23T02:00:00Z'),
                    point(usage(110,22),usage(10,2),'2026-10-23T02:00:00Z')])
        collect_local(self.store,cfg,include_derived=False)
        self.assertEqual(self.totals(),(2,132))

    def test_bad_compressed_file_does_not_block_other_files_and_retries(self):
        folder=self.root/'sources';folder.mkdir();bad=folder/'a.jsonl.zst';bad.write_bytes(b'not zstandard')
        rows=[meta(),model(),response('one',usage(100,20)),point(usage(100,20))]
        (folder/'b.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
        cfg={'sources':[dict(name='compressed',path=str(folder),kind='jsonl',route='codex')]}
        collect_local(self.store,cfg,include_derived=False)
        self.assertEqual(self.totals(),(1,120))
        with self.store.connect() as c:self.assertEqual(c.execute("SELECT status FROM sources WHERE name='compressed'").fetchone()[0],'partial')
        with zstd.open(bad,'wt') as f:f.write(''.join(json.dumps(r)+'\n' for r in rows))
        collect_local(self.store,cfg,include_derived=False)
        with self.store.connect() as c:self.assertEqual(c.execute("SELECT status FROM sources WHERE name='compressed'").fetchone()[0],'ok')
        self.assertEqual(self.totals(),(1,120))

    def test_independent_verifier_checks_native_usage_and_uncovered_legacy(self):
        rows=[meta(),model(),point(usage(100,20)),response('new',usage(300,50),usage(400,70),'2026-10-23T02:00:00Z'),
              point(usage(100,20),usage(0,0),'2026-10-23T02:00:00Z')]
        self.collect(rows)
        result=_codex(self.store.path)
        self.assertEqual(result['mismatches'],0)
        self.assertEqual(result['matched_native_sessions'],1)
        with self.store.connect() as c:c.execute('UPDATE events SET output=output+1 WHERE id IN (SELECT id FROM codex_responses)')
        self.assertEqual(_codex(self.store.path)['mismatches'],1)

    def test_inherited_response_in_a_fork_is_not_attributed_to_the_child(self):
        self.collect([meta(),model(),response('parent',usage(100,20)),point(usage(100,20))])
        self.collect([meta('child',forked_from_id='native',timestamp='2026-10-23T02:00:00Z'),
                      meta(),model(),response('parent',usage(100,20)),point(usage(100,20)),
                      model('gpt-child','2026-10-23T02:00:01Z'),
                      response('child',usage(10,2),ts='2026-10-23T02:00:02Z'),
                      point(usage(10,2),ts='2026-10-23T02:00:02Z')],path=self.root/'fork.jsonl')
        self.assertEqual(self.totals(),(2,132))

    def test_codex_telemetry_resolves_after_the_native_response_without_counting_twice(self):
        import time
        now=time.time()
        values={'event.name':'codex.sse_event','kind':'response.completed','model':'gpt-test',
                'response_id':'one','conversation_id':'transport-session','input_tokens':100,
                'output_tokens':20,'cached_input_tokens':0}
        record=dict(timeUnixNano=str(int(now*1e9)),attributes=[dict(key=k,value={
            'intValue' if isinstance(v,int) else 'stringValue':v}) for k,v in values.items()])
        result=ingest(self.store,dict(resourceLogs=[dict(scopeLogs=[dict(logRecords=[record])])]),now)
        self.assertEqual(result['pending'],1)
        self.collect([meta(),model(),response('one',usage(100,20)),point(usage(100,20))])
        with self.store.connect() as c:
            self.assertEqual(resolve(self.store,c),1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM telemetry_pending').fetchone()[0],0)
        self.assertEqual(self.totals(),(1,120))


if __name__=='__main__':unittest.main()

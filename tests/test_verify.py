"""Daily reconciliation (verify.reconcile) on disposable fixtures; no real sources."""
from pathlib import Path
import tempfile
import unittest

from llm_usage.store import Store
from llm_usage.verify import failed, reconcile


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'usage.db')
        self.cfg={'homes':[],'sources':[]}

    def test_codex_rebuild_matches_and_tampering_is_reported(self):
        with self.store.connect() as c:
            for i,(inp,out,last_inp,last_out) in enumerate([(100,10,100,10),(250,30,150,20),(400,45,150,15)]):
                sid=self.store.codex_point(c,'s',1000+i,'OpenAI','codex','gpt-test',
                    dict(input_tokens=inp,output_tokens=out),dict(input_tokens=last_inp,output_tokens=last_out))
            self.store.rebuild_codex(c,sid)
        result=reconcile(self.store.path,self.cfg)
        self.assertEqual(result['codex']['matched_complete_monotonic_sessions'],1)
        self.assertEqual(result['codex']['mismatches'],0)
        self.assertFalse(failed(result))
        with self.store.connect() as c:
            c.execute('UPDATE events SET output=output+1 WHERE id=(SELECT id FROM events ORDER BY ts LIMIT 1)')
        result=reconcile(self.store.path,self.cfg)
        self.assertEqual(result['codex']['mismatches'],1)
        self.assertTrue(failed(result))


if __name__=='__main__':unittest.main()

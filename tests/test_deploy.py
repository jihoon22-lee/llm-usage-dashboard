"""Release directories used by packaging/deploy.py (no services are touched)."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'packaging'))
import deploy  # noqa: E402


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'llm-usage'
        self.head=deploy.run('git','rev-parse','HEAD',capture=True).stdout.strip()

    def test_release_is_an_export_of_the_commit_built_once(self):
        target=deploy.build_release(self.head,self.root)
        self.assertTrue((target/'llm_usage/webapp.py').is_file())
        self.assertFalse((target/'.git').exists());self.assertFalse((target/'.venv').exists())
        self.assertEqual((target/'RELEASE').read_text().split()[0],self.head)
        stamp=(target/'RELEASE').stat().st_mtime_ns
        self.assertEqual(deploy.build_release(self.head,self.root),target)
        self.assertEqual((target/'RELEASE').stat().st_mtime_ns,stamp)
        self.assertEqual([p.name for p in (self.root/'releases').iterdir()],[self.head])

    def fake(self,name,age):
        folder=self.root/'releases'/name;(folder/'llm_usage').mkdir(parents=True)
        os.utime(folder,(time.time()-age,time.time()-age));return folder

    def test_switch_previous_and_prune_keep_the_two_live_releases(self):
        releases=[self.fake(f'r{i}',1000-i) for i in range(8)]  # r7 newest
        deploy.switch(releases[1],self.root);deploy.switch(releases[2],self.root)
        self.assertEqual(deploy.current_release(self.root),releases[2])
        self.assertEqual(deploy.previous_release(self.root),releases[1])
        self.assertFalse((self.root/'current.new').exists())
        deploy.prune(self.root,keep=3)
        left={p.name for p in (self.root/'releases').iterdir()}
        # The three newest plus the current (r2) and previous (r1) ones survive.
        self.assertEqual(left,{'r7','r6','r5','r2','r1'})
        # Rolling back makes the release we left the new "previous".
        deploy.switch(releases[1],self.root)
        self.assertEqual(deploy.previous_release(self.root),releases[2])

    def test_previous_skips_releases_that_no_longer_exist(self):
        a,b=self.fake('a',30),self.fake('b',20)
        deploy.switch(a,self.root);deploy.switch(b,self.root)
        import shutil;shutil.rmtree(a)
        self.assertIsNone(deploy.previous_release(self.root))


if __name__=='__main__':unittest.main()

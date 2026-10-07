import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('release_guard', Path(__file__).resolve().parents[1] / 'scripts/release_guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class ReleaseGuardTests(unittest.TestCase):
    def test_ci_requires_all_jobs_success_even_skips_and_cancellations(self):
        good = {name:{'result':'success'} for name in guard.REQUIRED_JOBS}
        guard.require_success(good)
        for state in ['failure','skipped','cancelled','pending',None]:
            with self.subTest(state=state), self.assertRaises(ValueError):
                guard.require_success({**good, 'browser':{'result':state}})
        with self.assertRaises(ValueError):
            guard.require_success({k:v for k,v in good.items() if k != 'browser'})

    def test_version_and_main_ancestry(self):
        guard.require_tag('v0.1.0','0.1.0',True)
        for tag,version,ancestor in [('v0.2.0','0.1.0',True),('v0.1.0','0.1.0',False),('0.1.0','0.1.0',True)]:
            with self.assertRaises(ValueError):
                guard.require_tag(tag,version,ancestor)

    def test_idempotent_release_never_replaces_published_or_draft_bytes(self):
        expected = {'wheel':'abc','source':'def'}
        self.assertEqual(guard.compare_assets(expected,expected,published=True), [])
        self.assertEqual(guard.compare_assets(expected,{'wheel':'abc'},published=False), ['source'])
        for existing,published in [({'wheel':'wrong'},False),({'wheel':'abc'},True),({'extra':'x'},False)]:
            with self.assertRaises(ValueError):
                guard.compare_assets(expected,existing,published=published)

    def test_artifact_tampering_and_provenance_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel, source = root/'demo.whl', root/'demo.tar.gz'
            wheel.write_bytes(b'wheel'); source.write_bytes(b'source')
            manifest = root/'release-manifest.json'
            manifest.write_text(json.dumps(dict(commit='a'*40,tag='v0.1.0',run_id='123',
                                                 files={p.name:guard.sha256(p) for p in [wheel,source]})))
            (root/'SHA256SUMS').write_text(''.join(f'{guard.sha256(p)}  {p.name}\n' for p in sorted([wheel,source,manifest])))
            guard.verify_manifest(root,commit='a'*40,tag='v0.1.0',run_id='123')
            for key,value in [('commit','b'*40),('tag','v0.2.0'),('run_id','456')]:
                with self.assertRaises(ValueError):
                    guard.verify_manifest(root,**{key:value})
            wheel.write_bytes(b'tampered')
            with self.assertRaises(ValueError):
                guard.verify_manifest(root)

    def test_manifest_rejects_unlisted_files_and_changed_checksum_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'a.whl').write_bytes(b'wheel')
            (root/'a.tar.gz').write_bytes(b'source')
            (root/'release-manifest.json').write_text(json.dumps(dict(files={
                name:guard.sha256(root/name) for name in ['a.whl','a.tar.gz']})))
            sums = ''.join(f'{guard.sha256(root/name)}  {name}\n' for name in ['a.tar.gz','a.whl','release-manifest.json'])
            (root/'SHA256SUMS').write_text(sums)
            guard.verify_manifest(root)
            (root/'unexpected.json').write_text('{}')
            with self.assertRaises(ValueError):
                guard.verify_manifest(root)
            (root/'unexpected.json').unlink()
            (root/'SHA256SUMS').write_text(sums.replace(sums[:64], '0'*64, 1))
            with self.assertRaises(ValueError):
                guard.verify_manifest(root)

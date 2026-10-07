"""Publisher lifecycle contract using an in-memory GitHub API and synthetic bytes."""
import importlib.util
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

scripts = Path(__file__).resolve().parents[1] / 'scripts'
spec = importlib.util.spec_from_file_location('release_guard', scripts / 'release_guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
with patch.dict(sys.modules, {'release_guard':guard}):
    spec = importlib.util.spec_from_file_location('publish_release', scripts / 'publish_release.py')
    publisher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publisher)


class ReleasePublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root/'a.whl').write_bytes(b'wheel')
        (self.root/'a.tar.gz').write_bytes(b'source')
        manifest = dict(commit='a'*40,tag='v0.1.0',version='0.1.0',run_id='123',repository='example/project',
                        files={name:guard.sha256(self.root/name) for name in ['a.whl','a.tar.gz']})
        (self.root/'release-manifest.json').write_text(json.dumps(manifest))
        names = sorted([*manifest['files'],'release-manifest.json'])
        (self.root/'SHA256SUMS').write_text(''.join(f'{guard.sha256(self.root/name)}  {name}\n' for name in names))
        self.env = dict(GITHUB_SHA='a'*40,RELEASE_TAG='v0.1.0',GITHUB_REPOSITORY='example/project',
                        GITHUB_RUN_ID='123',TESTED_ARTIFACT_ID='456',GH_TOKEN='synthetic')
        self.release = None
        self.assets = {}
        self.calls = []
        self.remote_sha = 'a'*40
        self.annotated = False
        self.move_after_upload = False

    def api(self, path, method='GET', data=None, **kwargs):
        self.calls.append((method,path))
        if method == 'GET' and '/git/ref/tags/' in path:
            return {'object':{'type':'tag' if self.annotated else 'commit', 'sha':'c'*40 if self.annotated else self.remote_sha}}
        if method == 'GET' and '/git/tags/' in path:
            return {'object':{'type':'commit','sha':self.remote_sha}}
        if method == 'GET':
            if self.release is None:
                raise urllib.error.HTTPError(path,404,'missing',{},None)
            return {**self.release,'assets':[dict(id=index,name=name) for index,name in enumerate(self.assets)]}
        if method == 'POST' and path.endswith('/releases'):
            self.assertTrue(data['draft'])
            self.release = dict(id=1,tag_name='v0.1.0',draft=True,assets=[],upload_url='https://uploads.github.com/release/1{?name}')
            return self.release
        if method == 'POST':
            self.assertTrue(self.release['draft'])
            name = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)['name'][0]
            self.assertNotIn(name,self.assets)
            self.assets[name] = data
            if self.move_after_upload:
                self.remote_sha = 'b'*40
            return {}
        if method == 'PATCH':
            self.assertEqual(data,{'draft':False})
            self.assertEqual(self.assets,{p.name:p.read_bytes() for p in self.root.iterdir()})
            self.release['draft'] = False
            self.release['immutable'] = True
            return self.release
        raise AssertionError('Unexpected API mutation')

    def download(self, command, *, stdout, check):
        index = int(command[2].rsplit('/',1)[-1])
        stdout.write(list(self.assets.values())[index])

    def invoke(self):
        with patch.dict(os.environ,self.env), patch.object(sys,'argv',['publish_release.py',str(self.root)]), \
                patch.object(publisher,'api',side_effect=self.api), patch.object(publisher.subprocess,'run',side_effect=self.download):
            with redirect_stdout(io.StringIO()):
                publisher.main()

    def test_new_release_is_draft_until_every_download_matches_then_rerun_is_noop(self):
        self.invoke()
        self.assertFalse(self.release['draft'])
        self.assertEqual(self.calls[-1][0],'PATCH')
        self.calls.clear()
        self.invoke()
        self.assertEqual({method for method,path in self.calls},{'GET'})

    def test_different_existing_asset_blocks_without_replacing(self):
        self.invoke()
        self.assets['a.whl'] = b'changed'
        for draft in [True,False]:
            self.release['draft'] = draft
            self.calls.clear()
            with self.assertRaises(ValueError):
                self.invoke()
            self.assertEqual({method for method,path in self.calls},{'GET'})

    def test_partial_identical_draft_resumes_only_missing_assets(self):
        self.release = dict(id=1,tag_name='v0.1.0',draft=True,assets=[],upload_url='https://uploads.github.com/release/1{?name}')
        self.assets['a.whl'] = (self.root/'a.whl').read_bytes()
        self.invoke()
        uploaded = [path for method,path in self.calls if method == 'POST']
        self.assertEqual(len(uploaded),3)
        self.assertTrue(all('name=a.whl' not in path for path in uploaded))
        self.assertFalse(self.release['draft'])

    def test_unprotected_existing_release_is_never_reported_complete(self):
        self.invoke()
        self.release['immutable'] = False
        self.calls.clear()
        with self.assertRaisesRegex(ValueError,'immutable'):
            self.invoke()
        self.assertEqual({method for method,path in self.calls},{'GET'})

    def test_annotated_tag_is_peeled_to_the_tested_commit(self):
        self.annotated = True
        self.invoke()
        self.assertFalse(self.release['draft'])
        self.assertTrue(any('/git/tags/' in path for method,path in self.calls))

    def test_tag_moved_during_upload_leaves_draft_unpublished(self):
        self.move_after_upload = True
        with self.assertRaisesRegex(ValueError,'tag moved'):
            self.invoke()
        self.assertTrue(self.release['draft'])
        self.assertFalse(any(method=='PATCH' for method,path in self.calls))

    def test_existing_release_with_wrong_tag_is_rejected(self):
        self.release = dict(id=1,tag_name='v0.2.0',draft=True,assets=[],upload_url='https://uploads.github.com/release/1{?name}')
        with self.assertRaisesRegex(ValueError,'tag identity'):
            self.invoke()
        self.assertFalse(any(method!='GET' for method,path in self.calls))

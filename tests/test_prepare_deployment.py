"""Preparation failures must never replace an existing deployment or environment."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('prepare_deployment', Path(__file__).resolve().parents[1] / 'packaging/prepare_deployment.py')
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)

class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main')
        (self.repo / 'uv.lock').write_text('test lock')
        (self.repo / 'pyproject.toml').write_text('[project]\nname="fixture"\n')
        self.git('add', 'uv.lock', 'pyproject.toml')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Fixture')
        self.commit = self.git('rev-parse', 'HEAD').strip()
        self.destination = self.root / 'candidate'

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo, text=True)

    def test_collision_preserves_existing_files(self):
        self.destination.mkdir()
        marker = self.destination / 'keep'
        marker.write_text('untouched')
        with self.assertRaises(FileExistsError):
            prepare.prepare(self.repo, self.destination, '/bin/false', '/usr/bin/python3')
        self.assertEqual(marker.read_text(), 'untouched')

    def test_failed_dependency_install_removes_only_owned_candidate(self):
        sibling = self.root / 'current'
        sibling.write_text('operating')
        with patch.object(prepare, 'install', side_effect=RuntimeError('resolver failed')):
            with self.assertRaises(RuntimeError):
                prepare.prepare(self.repo, self.destination, '/bin/false', '/usr/bin/python3')
        self.assertFalse(self.destination.exists())
        self.assertEqual(sibling.read_text(), 'operating')

    def test_dirty_checkout_is_rejected_before_install(self):
        (self.repo / 'uv.lock').write_text('changed')
        with patch.object(prepare, 'install') as install:
            with self.assertRaises(ValueError):
                prepare.prepare(self.repo, self.destination, '/bin/false', '/usr/bin/python3')
        install.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_symlink_parent_is_rejected(self):
        link = self.root / 'link'
        link.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaises(ValueError):
            prepare.prepare(self.repo, link / 'candidate', '/bin/false', '/usr/bin/python3')
        self.assertFalse((self.repo / 'candidate').exists())

    def test_success_uses_final_path_and_writes_manifest(self):
        with patch.object(prepare, 'install', return_value={'gunicorn':'test'}) as install:
            prepare.prepare(self.repo, self.destination, '/bin/false', '/usr/bin/python3')
        self.assertEqual(install.call_args.args[0], self.destination)
        import json
        manifest = json.loads((self.destination / 'candidate.json').read_text())
        self.assertEqual(manifest['commit'], self.commit)
        self.assertEqual(manifest['environment'], str(self.destination / '.venv'))
        self.assertFalse((self.destination / '.git').exists())

    def test_inherited_package_and_python_environment_is_removed(self):
        with patch.dict(os.environ, {'UV_PROJECT_ENVIRONMENT':'/bad', 'PIP_INDEX_URL':'https://bad', 'PYTHONPATH':'/bad', 'VIRTUAL_ENV':'/bad', 'LLM_USAGE_CONFIG':'/private', 'SSLKEYLOGFILE':'/private'}):
            env = prepare.clean_environment(self.root)
        self.assertFalse(set(env) & {'UV_PROJECT_ENVIRONMENT','PIP_INDEX_URL','PYTHONPATH','VIRTUAL_ENV','LLM_USAGE_CONFIG','SSLKEYLOGFILE'})
        self.assertEqual(env['HOME'], str(self.root))

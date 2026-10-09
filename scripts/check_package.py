"""Validate distribution allowlists and install the tested wheel outside its source tree."""
import email
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import venv
import zipfile

from release_guard import sha256, verify_manifest

root = Path(__file__).resolve().parents[1]
dist = root / 'dist'
project = tomllib.loads((root / 'pyproject.toml').read_text())['project']
version = project['version']
wheel, = dist.glob('*.whl')
sdist, = dist.glob('*.tar.gz')
asset_names = {'analysis.js','app.js','charts.js','core.js','format.js','icon-192.png','icon-512.png',
               'icon.svg','index.html','insights.js','manifest.json','planning.js','resources.js','quota.js','settings.js','style.css','sw.js','theme.js'}
assert {p.name for p in (root / 'llm_usage/web').iterdir() if p.is_file()} == asset_names
assets = {'llm_usage/web/' + name:sha256(root / 'llm_usage/web' / name) for name in sorted(asset_names)}
package_files = {p.relative_to(root).as_posix() for p in (root / 'llm_usage').glob('*.py')} | set(assets)
with zipfile.ZipFile(wheel) as archive:
    names = set(archive.namelist())
    metadata_name, = [n for n in names if n.endswith('.dist-info/METADATA')]
    metadata = email.message_from_bytes(archive.read(metadata_name))
    assert metadata['Version'] == version and metadata['Name'] == project['name']
    assert metadata['License-Expression'] == 'MIT', metadata
    allowed_metadata = {'METADATA', 'WHEEL', 'entry_points.txt', 'top_level.txt', 'RECORD', 'licenses/LICENSE'}
    metadata_prefix = metadata_name.rsplit('/', 1)[0] + '/'
    assert names == package_files | {metadata_prefix + n for n in allowed_metadata}, names - package_files
    for name, digest in assets.items():
        import hashlib
        assert hashlib.sha256(archive.read(name)).hexdigest() == digest, name
with tarfile.open(sdist) as archive:
    files = {m.name.split('/', 1)[1] for m in archive.getmembers() if m.isfile()}
    allowed_root = {'PKG-INFO', 'pyproject.toml', 'README.md', 'LICENSE', 'MANIFEST.in', 'setup.cfg'}
    allowed_egg = {'PKG-INFO', 'SOURCES.txt', 'dependency_links.txt', 'entry_points.txt', 'requires.txt', 'top_level.txt'}
    allowed = package_files | allowed_root | {'llm_usage_dashboard.egg-info/' + n for n in allowed_egg}
    assert not files - allowed, 'Unexpected source package files: ' + repr(files - allowed)
    assert package_files <= files, 'Missing source package files'
with tempfile.TemporaryDirectory(prefix='llm-package-') as temporary:
    temp = Path(temporary)
    environment = temp / 'venv'
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / 'bin/python'
    subprocess.run([str(python), '-m', 'pip', 'install', str(wheel)], cwd=temp, check=True)
    subprocess.run([str(python), '-m', 'pip', 'check'], cwd=temp, check=True)
    env = {k:v for k,v in os.environ.items() if k != 'PYTHONPATH'}
    subprocess.run([str(environment / 'bin/llm-usage'), '--help'], cwd=temp, env=env, check=True)
    smoke = '''
import json, pathlib, sys, socket
from types import SimpleNamespace
from llm_usage.webapp import create_app
import llm_usage
assert not pathlib.Path(llm_usage.__file__).is_relative_to(pathlib.Path(sys.argv[1]))
app=create_app(dict(database='synthetic.db', origin='https://dashboard.example',secret_key='synthetic-only',allowed_logins=['owner']))
client=app.test_client()
kwargs=dict(headers={'Tailscale-User-Login':'owner'}, environ_overrides={'gunicorn.socket':SimpleNamespace(family=socket.AF_UNIX)})
for path in ['/', '/manifest.json', '/sw.js', *['/assets/'+p.name for p in pathlib.Path(app.static_folder).iterdir() if p.is_file()]]:
    with client.get(path, **kwargs) as response: assert response.status_code==200, path
print('Installed wheel entrypoint, metadata and all web assets passed')
'''
    subprocess.run([str(python), '-c', smoke, str(root)], cwd=temp, env=env, check=True)
manifest = dict(version=version, commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
                tag=os.environ.get('RELEASE_TAG',''), run_id=os.environ.get('GITHUB_RUN_ID','local'),
                repository=os.environ.get('GITHUB_REPOSITORY','local'),
                files={p.name:sha256(p) for p in [wheel, sdist]}, web_assets=assets)
(dist / 'release-manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
(dist / 'SHA256SUMS').write_text(''.join(f'{sha256(p)}  {p.name}\n' for p in sorted([wheel,sdist,dist / 'release-manifest.json'])))
verify_manifest(dist)
print('Distribution manifests and SHA256SUMS verified')

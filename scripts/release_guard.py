"""Fail-closed release invariants shared by CI, publishing, and regression tests."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib

REQUIRED_JOBS = {'python', 'javascript', 'browser', 'package', 'public-checks', 'security', 'codeql'}


def require_success(needs):
    if set(needs) != REQUIRED_JOBS or any(job.get('result') != 'success' for job in needs.values()):
        raise ValueError('Every required CI job must succeed; failures, skips and cancellations block release')


def require_tag(tag, version, ancestor):
    if not re.fullmatch(r'v\d+\.\d+\.\d+', tag) or tag != 'v' + version:
        raise ValueError('Release tag must exactly match the project version')
    if not ancestor:
        raise ValueError('Release commit must be reachable from origin/main')


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_manifest(folder, *, commit=None, tag=None, run_id=None):
    folder = Path(folder)
    data = json.loads((folder / 'release-manifest.json').read_text())
    for key, expected in [('commit', commit), ('tag', tag), ('run_id', run_id)]:
        if expected is not None and str(data.get(key)) != str(expected):
            raise ValueError('Manifest provenance mismatch: ' + key)
    if data.get('tag'):
        require_tag(data['tag'], data.get('version', ''), True)
    files = data['files']
    if len(files) != 2 or not any(n.endswith('.whl') for n in files) or not any(n.endswith('.tar.gz') for n in files):
        raise ValueError('Expected exactly one wheel and one source archive')
    for name, digest in files.items():
        if Path(name).name != name or not re.fullmatch('[0-9a-f]{64}', digest) or sha256(folder / name) != digest:
            raise ValueError('Artifact checksum mismatch: ' + name)
    if {p.name for p in folder.iterdir()} != set(files) | {'release-manifest.json', 'SHA256SUMS'}:
        raise ValueError('Unexpected release files')
    expected_sums = ''.join(f'{sha256(folder / n)}  {n}\n' for n in sorted([*files, 'release-manifest.json']))
    if (folder / 'SHA256SUMS').read_text() != expected_sums:
        raise ValueError('SHA256SUMS mismatch')
    return data


def compare_assets(expected, existing, *, published):
    """Never replace an asset. An incomplete draft may only gain missing assets."""
    if set(existing) - set(expected):
        raise ValueError('Unexpected existing release assets')
    for name, digest in existing.items():
        if expected[name] != digest:
            raise ValueError('Existing asset differs: ' + name)
    if published and existing != expected:
        raise ValueError('Published release is incomplete; refusing mutation')
    return sorted(set(expected) - set(existing))


if __name__ == '__main__':
    if sys.argv[1] == 'ci':
        require_success(json.loads(sys.argv[2]))
    elif sys.argv[1] == 'tag':
        version = tomllib.loads(Path('pyproject.toml').read_text())['project']['version']
        ancestor = subprocess.run(['git', 'merge-base', '--is-ancestor', 'HEAD', 'origin/main']).returncode == 0
        require_tag(sys.argv[2], version, ancestor)
    else:
        raise SystemExit('Unknown guard')

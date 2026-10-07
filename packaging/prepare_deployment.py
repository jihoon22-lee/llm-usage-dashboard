"""Prepare an independent, final-path runtime; never activate units or current links."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

UV_VERSION = '0.12.23'


def clean_environment(home):
    # Explicit allowlist: no inherited provider, Python, uv, pip or TLS key-log settings.
    return {'PATH': os.defpath, 'HOME': str(home), 'LANG': 'C.UTF-8'}


def git(repo, *args):
    return subprocess.check_output(['git', *args], cwd=repo, text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(destination, uv, python):
    with tempfile.TemporaryDirectory(prefix='llm-candidate-home-') as temporary:
        env = clean_environment(temporary)
        version = subprocess.check_output([uv, '--version'], env=env, text=True).split()
        if version[:2] != ['uv', UV_VERSION]:
            raise ValueError('Use the documented uv version: ' + UV_VERSION)
        subprocess.run([uv, '--no-config', 'sync', '--locked', '--no-dev', '--no-editable',
                        '--python', python, '--no-managed-python', '--no-python-downloads',
                        '--default-index', 'https://pypi.org/simple'], cwd=destination, env=env, check=True)
        runtime = str(destination / '.venv/bin/python')
        subprocess.run([uv, '--no-config', 'pip', 'check', '--python', runtime], env=env, check=True)
        subprocess.run([runtime, '-m', 'llm_usage.cli', '--help'], cwd=temporary, env=env, check=True,
                       stdout=subprocess.DEVNULL)
        code = ('import importlib.metadata as m,json,sys,llm_usage; '
                'print(json.dumps({"python":sys.version,"packages":'
                '{d.metadata["Name"]:d.version for d in m.distributions()}}))')
        return json.loads(subprocess.check_output([runtime, '-c', code], cwd=temporary, env=env, text=True))


def prepare(repo, destination, uv, python):
    repo, destination = Path(repo).absolute(), Path(destination).absolute()
    if os.getuid() == 0:
        raise ValueError('Run preparation as the service owner, not root')
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(destination)) or '..' in destination.parts:
        raise ValueError('Candidate path must be an absolute systemd-safe path')
    for path in (destination, *destination.parents):
        if path.is_symlink():
            raise ValueError('Candidate path must not contain symlinks')
    if destination.exists():
        raise FileExistsError('Candidate already exists; inspect it instead of replacing it')
    if destination.is_relative_to(repo.resolve()):
        raise ValueError('Prepare outside the checkout')
    parent = destination.parent
    if not parent.is_dir() or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o022:
        raise ValueError('Candidate parent must exist, belong to this user and not be group/world writable')
    if git(repo, 'branch', '--show-current') != 'main' or git(repo, 'status', '--porcelain'):
        raise ValueError('Prepare from a clean main checkout')
    commit = git(repo, 'rev-parse', 'HEAD')
    archive = subprocess.check_output(['git', 'archive', '--format=tar', commit], cwd=repo)
    with tarfile.open(fileobj=io.BytesIO(archive)) as content:
        members = content.getmembers()
        if any(not (m.isfile() or m.isdir()) or Path(m.name).is_absolute() or '..' in Path(m.name).parts for m in members):
            raise ValueError('Candidate archive must contain only regular source files and directories')
        destination.mkdir(mode=0o700)  # Exclusive ownership begins only here.
        try:
            content.extractall(destination, filter='data')
            source_hashes = {m.name: digest(destination / m.name) for m in members if m.isfile()}
            if not (destination / 'uv.lock').is_file():
                raise ValueError('Commit has no uv.lock')
            installed = install(destination, str(uv), str(python))
            manifest = {'commit': commit, 'uv': UV_VERSION, 'environment': str(destination / '.venv'),
                        'lock_sha256': digest(destination / 'uv.lock'), 'source_sha256': source_hashes,
                        'installed': installed, 'activated': False}
            (destination / 'candidate.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
        except BaseException:
            shutil.rmtree(destination)
            raise
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True, type=Path, help='New final directory outside the checkout; parent must exist')
    parser.add_argument('--uv', required=True, type=Path, help='Absolute path to verified uv ' + UV_VERSION)
    parser.add_argument('--python', default=sys.executable, type=Path)
    args = parser.parse_args()
    if not args.uv.is_absolute() or not args.python.is_absolute():
        parser.error('--uv and --python must be absolute executable paths')
    try:
        target = prepare(Path(__file__).resolve().parents[1], args.destination, args.uv, args.python)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Preparation failed: {exc}\n')
    print(f'Candidate prepared: {target}\nNo services or release links were changed.')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Deploy the checked-out main to the running services and confirm they took it.

  .venv/bin/python packaging/deploy.py [--smoke]   deploy HEAD of main
  .venv/bin/python packaging/deploy.py --prepare   only build HEAD as the current release
  .venv/bin/python packaging/deploy.py --rollback  return to the previous release

1. refuses a dirty tree or a branch other than main
2. runs the unit tests, the JavaScript syntax check and the Node unit tests
3. release mode (units run from ~/.local/lib/llm-usage/current, see
   packaging/system_update.py --release): exports HEAD with `git archive` into
   releases/<commit> and atomically repoints `current`, so editing this checkout
   never changes what is running. Without release mode the units run this
   checkout directly (the original install).
4. restarts the web unit (PartOf carries it to the collector) through the
   password-less rule from packaging/system_update.py; a failed restart puts the
   previous release back. Without the rule it reloads only the web workers
   (checkout mode) or stops (release mode).
5. waits for /healthz, a collector restarted after step 4 and its next heartbeat;
   in release mode a failed check rolls back to the previous release
"""
import argparse
import io
import ipaddress
import json
import os
from pathlib import Path
import shutil
import re
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.request
import urllib.parse

PROJECT=Path(__file__).resolve().parents[1]
DATABASE=Path.home()/'.local/share/llm-usage/usage.db'
RELEASE_ROOT=Path.home()/'.local/lib/llm-usage'
KEEP=5


def run(*args,check=True,capture=False):
    return subprocess.run(args,cwd=PROJECT,check=check,text=True,
                          stdout=subprocess.PIPE if capture else None,stderr=subprocess.STDOUT if capture else None)


def unit(name,prop):
    return run('systemctl','show',name,'-p',prop,'--value',capture=True).stdout.strip()


def started(name):
    """Monotonic start time of the unit's current activation, in microseconds."""
    value=unit(name,'ActiveEnterTimestampMonotonic')
    return int(value) if value.isdigit() else 0


def heartbeat():
    try:
        with sqlite3.connect(f'file:{DATABASE}?mode=ro',uri=True,timeout=5) as c:
            row=c.execute("SELECT data FROM state WHERE key='collector'").fetchone()
        return (json.loads(row[0]) if row else {}).get('checked') or 0
    except (sqlite3.Error,ValueError):return 0


def wait(predicate,seconds):
    deadline=time.time()+seconds
    while time.time()<deadline:
        if predicate():return True
        time.sleep(1)
    return False


def healthy():
    try:
        with urllib.request.urlopen('http://127.0.0.1:8766/healthz',timeout=2) as response:return response.status==200
    except OSError:return False


# ---- releases -------------------------------------------------------------

def build_release(commit,root=RELEASE_ROOT):
    """Export a commit's tracked files into releases/<commit> once; returns the folder."""
    releases=root/'releases';target=releases/commit
    if target.exists():return target
    staging=releases/f'.{commit}.staging'
    shutil.rmtree(staging,ignore_errors=True);staging.mkdir(parents=True)
    archive=subprocess.run(['git','archive','--format=tar',commit],cwd=PROJECT,check=True,stdout=subprocess.PIPE).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:tar.extractall(staging,filter='data')
    (staging/'RELEASE').write_text(f'{commit}\n{time.strftime("%Y-%m-%dT%H:%M:%S%z")}\n')
    os.replace(staging,target)
    return target


def current_release(root=RELEASE_ROOT):
    link=root/'current'
    return Path(os.readlink(link)) if link.is_symlink() else None


def switch(target,root=RELEASE_ROOT):
    """Point root/current at target atomically and record it in the history."""
    link=root/'current';staging=root/'current.new'
    if staging.is_symlink() or staging.exists():staging.unlink()
    os.symlink(target,staging);os.replace(staging,link)
    with (root/'history').open('a') as handle:handle.write(f'{int(time.time())} {target.name}\n')


def previous_release(root=RELEASE_ROOT):
    """The release that was current before the present one, if it still exists."""
    current=current_release(root)
    try:names=[line.split()[1] for line in (root/'history').read_text().splitlines() if line.strip()]
    except OSError:return None
    for name in reversed(names):
        candidate=root/'releases'/name
        if candidate!=current and candidate.is_dir():return candidate
    return None


def prune(root=RELEASE_ROOT,keep=KEEP):
    """Keep the newest releases plus the current and previous ones."""
    releases=sorted((p for p in (root/'releases').glob('*') if p.is_dir() and not p.name.startswith('.')),
                    key=lambda p:p.stat().st_mtime,reverse=True)
    protected={current_release(root),previous_release(root)}
    for old in releases[keep:]:
        if old not in protected:shutil.rmtree(old)


def release_mode(root=RELEASE_ROOT):
    return unit('llm-usage.service','WorkingDirectory')==str(root/'current')


# ---- deployment -----------------------------------------------------------

def restart(before_web,before_collector):
    if run('sudo','-n','systemctl','restart','llm-usage.service',check=False,capture=True).returncode!=0:return 'denied'
    if not wait(healthy,60):return 'unhealthy'
    if not wait(lambda:started('llm-usage.service')>before_web and started('llm-usage-collector.service')>before_collector,60):return 'not restarted'
    mark=time.time()
    return None if wait(lambda:heartbeat()>mark,180) else 'no heartbeat'


def checks():
    if run('git','rev-parse','--abbrev-ref','HEAD',capture=True).stdout.strip()!='main':sys.exit('main 체크아웃에서만 배포합니다.')
    if run('git','status','--porcelain',capture=True).stdout.strip():sys.exit('커밋되지 않은 변경이 있습니다.')
    python=str(PROJECT/'.venv/bin/python')
    for script in sorted((PROJECT/'llm_usage/web').glob('*.js')):run('node','--check',str(script))
    run('node','--test','tests/js/*.test.mjs')
    run(python,'-m','unittest','discover','-s','tests','-q')


def deploy_release(target,rollback_to,label):
    before=(started('llm-usage.service'),started('llm-usage-collector.service'))
    switch(target)
    problem=restart(*before)
    if problem is None:
        prune();print(f'배포 완료: {label} (릴리스 {target.name[:12]})');return True
    if rollback_to is not None:
        switch(rollback_to)
        if problem!='denied':restart(started('llm-usage.service'),started('llm-usage-collector.service'))
    if problem=='denied':
        sys.exit('재시작 권한이 없어 릴리스를 되돌렸습니다. sudo .venv/bin/python packaging/system_update.py --owner $USER 를 먼저 실행하세요.')
    sys.exit(f'배포 확인 실패({problem}): 이전 릴리스 {rollback_to.name[:12] if rollback_to else "없음"}로 되돌렸습니다.')


def smoke_origin(value):
    """Validate a live target before checks or service mutations; stdlib only."""
    try:
        if not isinstance(value,str) or any(ch.isspace() or ord(ch)<32 or ord(ch)==127 for ch in value) or any(ch in value for ch in '\\?#'):
            raise ValueError
        parts=urllib.parse.urlsplit(value)
        if parts.scheme!='https' or not parts.netloc or parts.path not in ('','/') or any(ch in parts.netloc for ch in '@%'):
            raise ValueError
        host=parts.hostname
        if not host or parts.netloc.endswith(':') or (parts.port is not None and not 1<=parts.port<=65535):raise ValueError
        if ':' in host:
            if not re.fullmatch(r'\[[0-9A-Fa-f:.]+\](?::[0-9]+)?',parts.netloc):raise ValueError
            ipaddress.IPv6Address(host)
        else:
            if not re.fullmatch(r'[^:\[\]]+(?::[0-9]+)?',parts.netloc):raise ValueError
            ascii_host=host.encode('idna').decode('ascii').removesuffix('.')
            if len(ascii_host)>253 or not all(re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?',label) for label in ascii_host.split('.')):
                raise ValueError
        return 'https://'+parts.netloc
    except (ValueError,UnicodeError):
        raise ValueError('--smoke-origin requires a valid HTTPS origin without credentials, path, query or fragment') from None


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--smoke',action='store_true',help='Also run tests/browser_smoke.py against the private origin')
    parser.add_argument('--prepare',action='store_true',help='Build HEAD as the current release without restarting')
    parser.add_argument('--rollback',action='store_true',help='Return to the previous release (release mode only)')
    parser.add_argument('--smoke-origin',help='Explicit HTTPS origin for the optional read-only live smoke')
    args=parser.parse_args()
    if args.smoke and not args.smoke_origin:parser.error('--smoke requires --smoke-origin HTTPS_ORIGIN')
    if args.smoke_origin is not None:
        try:args.smoke_origin=smoke_origin(args.smoke_origin)
        except ValueError as exc:parser.error(str(exc))
    python=str(PROJECT/'.venv/bin/python')
    if args.rollback:
        if not release_mode():sys.exit('릴리스 모드가 아니어서 되돌릴 이전 릴리스가 없습니다.')
        previous=previous_release()
        if previous is None:sys.exit('이전 릴리스가 없습니다.')
        deploy_release(previous,current_release(),'롤백')
        return
    checks()
    head=run('git','rev-parse','HEAD',capture=True).stdout.strip()
    if args.prepare:
        switch(build_release(head));print(f'릴리스 준비: {head[:12]} → {RELEASE_ROOT/"current"}');return
    if release_mode():
        deploy_release(build_release(head),current_release(),head[:7])
    else:
        before_web,before_collector=started('llm-usage.service'),started('llm-usage-collector.service')
        problem=restart(before_web,before_collector)
        if problem=='denied':
            os.kill(int(unit('llm-usage.service','MainPID')),signal.SIGHUP)
            print('재시작 권한이 없어 웹 worker만 다시 읽었습니다. 수집기는 이전 코드로 실행 중입니다.')
            print('한 번만 실행: sudo .venv/bin/python packaging/system_update.py')
            if not wait(healthy,60):sys.exit('실패: 웹 healthz 확인 시간 초과')
        elif problem:sys.exit(f'실패: {problem}')
        print(f'배포 완료: {head[:7]} (체크아웃 실행 · 릴리스 모드 아님)'+('' if problem else ' (웹·수집기 재시작)'))
    if args.smoke:run(python,'tests/browser_smoke.py','--origin',args.smoke_origin)


if __name__=='__main__':main()

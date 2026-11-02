#!/usr/bin/env python3
"""Deploy the checked-out main to the running services and confirm they took it.

  .venv/bin/python packaging/deploy.py [--smoke]   deploy HEAD of main
  .venv/bin/python packaging/deploy.py --prepare   only build HEAD as the current release
  .venv/bin/python packaging/deploy.py --rollback  return to the previous release

1. refuses a dirty tree or a branch other than main
2. release mode (units run from ~/.local/lib/llm-usage/current, see
   packaging/system_update.py --release): exports HEAD with `git archive` into
   releases/<commit> and gives it its own environment, releases/<commit>/.venv,
   synced by uv from that release's uv.lock without the dev group. Without release
   mode the units run this checkout directly (the original install).
3. runs the unit tests (in release mode with the release's environment), the
   JavaScript syntax check and the Node unit tests
4. release mode: atomically repoints `current`, so editing this checkout never
   changes what is running. Units started from current/.venv switch their
   dependencies with the code; units still started from this checkout's .venv
   switch only the code, and the deployer says how to move them.
5. restarts the web unit (PartOf carries it to the collector) through the
   password-less rule from packaging/system_update.py; a failed restart puts the
   previous release back. Without the rule it reloads only the web workers
   (checkout mode) or stops (release mode).
6. waits for /healthz, a collector restarted after step 5 and its next heartbeat;
   in release mode a failed check rolls back to the previous release
"""
import argparse
from contextlib import closing
import io
import ipaddress
import json
import os
from pathlib import Path
import shutil
import re
import signal
import shlex
import pwd
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.request
import urllib.parse

PROJECT=Path(__file__).resolve().parents[1]
RELEASE_ROOT=Path.home()/'.local/lib/llm-usage'
KEEP=5
# Release environments are synced by the uv version CI uses; a release's environment is
# complete once its marker file exists (packaging/system_update.py checks the same name).
UV_VERSION='0.12.23'
ENVIRONMENT_MARKER='ENVIRONMENT'


def run(*args,check=True,capture=False):
    return subprocess.run(args,cwd=PROJECT,check=check,text=True,
                          stdout=subprocess.PIPE if capture else None,stderr=subprocess.STDOUT if capture else None)


def unit(name,prop):
    return run('systemctl','show',name,'-p',prop,'--value',capture=True).stdout.strip()


def started(name):
    """Monotonic start time of the unit's current activation, in microseconds."""
    value=unit(name,'ActiveEnterTimestampMonotonic')
    return int(value) if value.isdigit() else 0


def deployment_database():
    """Resolve the two installed services' config before changing a release link.

    Never evaluate systemd environment text as shell code. EnvironmentFile-based
    overrides need explicit unit configuration so this preflight can prove identity.
    """
    databases=[]
    try:
        for name in ('llm-usage.service','llm-usage-collector.service'):
            if unit(name,'EnvironmentFiles'):
                raise ValueError('EnvironmentFile overrides are not supported')
            env=dict(item.split('=',1) for item in shlex.split(unit(name,'Environment')) if '=' in item)
            unset=shlex.split(unit(name,'UnsetEnvironment'))
            if any(item.split('=',1)[0] in ('HOME','LLM_USAGE_CONFIG') for item in unset):
                raise ValueError('ambiguous service environment')
            user=unit(name,'User')
            if not user:raise ValueError('missing service owner')
            owner=pwd.getpwuid(int(user)) if user.isdigit() else pwd.getpwnam(user)
            home=Path(env.get('HOME') or owner.pw_dir)
            config=Path(env.get('LLM_USAGE_CONFIG') or home/'.config/llm-usage/config.json')
            if not config.is_absolute():raise ValueError('relative configuration')
            override=os.environ.get('LLM_USAGE_CONFIG')
            if override is not None and Path(override).resolve()!=config.resolve():
                raise ValueError('shell and service configurations differ')
            data=json.loads(config.read_text())
            database=Path(data['database'])
            if not database.is_absolute():raise ValueError('relative database')
            databases.append(database.resolve())
        if databases[0]!=databases[1]:raise ValueError('service databases differ')
        return databases[0]
    except (OSError,ValueError,TypeError,KeyError,subprocess.CalledProcessError):
        raise ValueError('서비스 설정의 database를 확인할 수 없습니다. 두 유닛의 명시적 설정·경로·권한과 셸 환경을 확인하세요.') from None


def heartbeat(database):
    try:
        with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True,timeout=5)) as c:
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


def base_python():
    """The interpreter this checkout's .venv was made from; release environments use it too."""
    return os.path.realpath(PROJECT/'.venv/bin/python')


def prepare_environment(release,python=None,uv=('uvx','--from',f'uv=={UV_VERSION}','uv')):
    """Give a release its own .venv from its own uv.lock (no dev group), once.

    A virtual environment records absolute paths, so it is made in the release's final
    folder. The services cannot write bytecode there (ProtectHome=read-only), so it is
    compiled now. The app itself is imported from the release folder, as the units run
    it, and is not installed. An environment with its marker is never synced again: it
    may be the one running. Without the marker a partial one is removed and remade."""
    if (release/ENVIRONMENT_MARKER).is_file():return
    python=python or base_python();target=release/'.venv'
    shutil.rmtree(target,ignore_errors=True)
    env={k:v for k,v in os.environ.items() if k not in ('VIRTUAL_ENV','PYTHONPATH','PYTHONHOME')}
    env.update(UV_PROJECT_ENVIRONMENT=str(target),UV_PYTHON_DOWNLOADS='never')
    try:
        subprocess.run([*uv,'sync','--locked','--no-dev','--no-install-project','--compile-bytecode','--python',python],
                       cwd=release,env=env,check=True)
        # Also proves the services' two programs start: -I keeps the release folder off sys.path.
        version=subprocess.run([str(target/'bin/python'),'-I','-c','import sys,flask,gunicorn;print(sys.version.split()[0])'],
                               cwd='/',check=True,text=True,stdout=subprocess.PIPE).stdout.strip()
        if not (target/'bin/gunicorn').is_file():raise RuntimeError('gunicorn 실행 파일 없음')
    except (OSError,RuntimeError,subprocess.CalledProcessError):
        shutil.rmtree(target,ignore_errors=True);raise
    marker=release/(ENVIRONMENT_MARKER+'.new')
    marker.write_text(f'uv {UV_VERSION}\npython {python} {version}\n')
    os.replace(marker,release/ENVIRONMENT_MARKER)


def environment(release):
    """prepare_environment for the deployer: a failure stops before any service change."""
    try:prepare_environment(release)
    except (OSError,RuntimeError,subprocess.CalledProcessError) as exc:
        sys.exit(f'릴리스 {release.name[:12]}의 실행 환경을 만들지 못했습니다({exc}). uv {UV_VERSION}(uvx)와 '
                 f'{base_python()}, 네트워크 또는 uv 캐시를 확인하세요. 서비스와 current 링크는 바꾸지 않았습니다.')


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


def unit_program(name):
    """The program a unit starts, from `systemctl show -p ExecStart` (`{ path=... ; argv[]=... }`)."""
    found=re.search(r'\bpath=(\S+)',unit(name,'ExecStart'))
    return found[1] if found else ''


def release_environment_mode(root=RELEASE_ROOT):
    """Both units start their program from current/.venv (packaging/system_update.py --release)."""
    prefix=str(root/'current/.venv/bin')+'/'
    return all(unit_program(name).startswith(prefix) for name in ('llm-usage.service','llm-usage-collector.service'))


# ---- deployment -----------------------------------------------------------

def restart(before_web,before_collector,database):
    if run('sudo','-n','systemctl','restart','llm-usage.service',check=False,capture=True).returncode!=0:return 'denied'
    if not wait(healthy,60):return 'unhealthy'
    if not wait(lambda:started('llm-usage.service')>before_web and started('llm-usage-collector.service')>before_collector,60):return 'not restarted'
    mark=time.time()
    return None if wait(lambda:heartbeat(database)>mark,180) else 'no heartbeat'


def tree_checks():
    if run('git','rev-parse','--abbrev-ref','HEAD',capture=True).stdout.strip()!='main':sys.exit('main 체크아웃에서만 배포합니다.')
    if run('git','status','--porcelain',capture=True).stdout.strip():sys.exit('커밋되지 않은 변경이 있습니다.')


def code_checks(python):
    """The JavaScript checks and the unit tests, run from this clean checkout with `python`."""
    for script in sorted((PROJECT/'llm_usage/web').glob('*.js')):run('node','--check',str(script))
    run('node','--test','tests/js/*.test.mjs')
    run(python,'-m','unittest','discover','-s','tests','-q')


def checks():
    tree_checks();code_checks(str(PROJECT/'.venv/bin/python'))


def deploy_release(target,rollback_to,label,database):
    before=(started('llm-usage.service'),started('llm-usage-collector.service'))
    switch(target)
    problem=restart(*before,database)
    if problem is None:
        prune();print(f'배포 완료: {label} (릴리스 {target.name[:12]})');return True
    if rollback_to is not None:
        switch(rollback_to)
        if problem!='denied':restart(started('llm-usage.service'),started('llm-usage-collector.service'),database)
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
    try:database=deployment_database()
    except ValueError as exc:parser.error(str(exc))
    if args.rollback:
        if not release_mode():sys.exit('릴리스 모드가 아니어서 되돌릴 이전 릴리스가 없습니다.')
        previous=previous_release()
        if previous is None:sys.exit('이전 릴리스가 없습니다.')
        # Units started from current/.venv need one in the release they return to; a release
        # from before release environments gets it from its own lock.
        if release_environment_mode():environment(previous)
        deploy_release(previous,current_release(),'롤백',database)
        return
    if args.prepare or release_mode():
        tree_checks()
        head=run('git','rev-parse','HEAD',capture=True).stdout.strip()
        # Units started from current/.venv run each release in its own environment, and
        # --prepare readies one for system_update.py --release. Release-mode units still
        # started from this checkout's .venv are checked with it and switch only the code.
        moved=release_environment_mode();target=build_release(head)
        if moved or args.prepare:
            environment(target);code_checks(str(target/'.venv/bin/python'))
        else:code_checks(python)
        current=current_release()
        if args.prepare:
            switch(target);print(f'릴리스 준비: {head[:12]} → {RELEASE_ROOT/"current"}')
            # Running release-mode units would pick up other code at their next restart, without
            # the deployer's checks: such an install deploys the commit first (docs/operations.md).
            if current is not None and current!=target and release_mode():
                print('경고: 실행 중인 릴리스와 다른 커밋입니다. 서비스는 다음 재시작부터 이 코드를 실행합니다. '
                      '릴리스로 실행 중인 설치는 이 커밋을 먼저 일반 배포한 뒤 --prepare 하세요.')
            return
        if moved and current is not None:environment(current)
        deploy_release(target,current,head[:7],database)
        if not moved:
            print('두 유닛은 아직 이 checkout의 .venv로 실행되어 코드만 바뀌었습니다. 릴리스마다의 환경으로 옮기려면 한 번만: '
                  '.venv/bin/python packaging/deploy.py --prepare 다음 '
                  'sudo .venv/bin/python packaging/system_update.py --owner $USER --release')
    else:
        checks()
        head=run('git','rev-parse','HEAD',capture=True).stdout.strip()
        before_web,before_collector=started('llm-usage.service'),started('llm-usage-collector.service')
        problem=restart(before_web,before_collector,database)
        if problem=='denied':
            pid=unit('llm-usage.service','MainPID')
            if not pid.isdigit() or int(pid)<=0:
                sys.exit('웹 서비스의 유효한 PID가 없어 다시 읽지 않았습니다. 서비스 상태와 재시작 권한을 확인하세요.')
            os.kill(int(pid),signal.SIGHUP)
            print('재시작 권한이 없어 웹 worker만 다시 읽었습니다. 수집기는 이전 코드로 실행 중입니다.')
            print('한 번만 실행: sudo .venv/bin/python packaging/system_update.py')
            if not wait(healthy,60):sys.exit('실패: 웹 healthz 확인 시간 초과')
        elif problem:sys.exit(f'실패: {problem}')
        print(f'배포 완료: {head[:7]} (체크아웃 실행 · 릴리스 모드 아님)'+('' if problem else ' (웹·수집기 재시작)'))
    if args.smoke:run(python,'tests/browser_smoke.py','--origin',args.smoke_origin)


if __name__=='__main__':main()

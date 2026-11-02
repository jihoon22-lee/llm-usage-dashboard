#!/usr/bin/env python3
"""Apply root-only settings to an existing install, idempotently.

Run once with sudo after updating the code:
  sudo .venv/bin/python packaging/system_update.py --owner YOUR_USER

- collector unit: Restart=always, WatchdogSec=1800, NotifyAccess=main
- /etc/sudoers.d/llm-usage: the owner may restart only these two units without a
  password, so packaging/deploy.py can reload new code without an interactive sudo
- --release: both units run from ~<owner>/.local/lib/llm-usage/current with the
  release's own environment, current/.venv (both built by `packaging/deploy.py
  --prepare` as the owner first): gunicorn and `python -m llm_usage.cli collect`
  from there import the release. Only the files are checked here; nothing from the
  release runs as root. --checkout switches back to running this checkout directly.
Previous files are backed up under /var/backups/llm-usage/<time>-<random>/. After a restart the
web health and a running collector are checked; on failure the unit files this run
replaced are put back and the services restarted from them.
"""
import argparse
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from install_common import resolve_owner, service_path

UNITS=Path('/etc/systemd/system')
WEB=UNITS/'llm-usage.service'
COLLECTOR=UNITS/'llm-usage-collector.service'
PROJECT=Path(__file__).resolve().parents[1]
SUDOERS=Path('/etc/sudoers.d/llm-usage')
BACKUPS=Path('/var/backups/llm-usage')
COLLECTOR_SETTINGS={'Restart':'always','WatchdogSec':'1800','NotifyAccess':'main'}
# Written by packaging/deploy.py once a release's environment is complete.
ENVIRONMENT_MARKER='ENVIRONMENT'


def sudoers_text(owner):
    systemctl=shutil.which('systemctl') or '/usr/bin/systemctl'
    return (f'# Managed by llm-usage packaging/system_update.py: restart only, no other commands.\n'
            f'{owner} ALL=(root) NOPASSWD: {systemctl} restart llm-usage.service, {systemctl} restart llm-usage-collector.service\n')


def set_service_keys(text,values):
    """Replace or add keys inside the [Service] section only."""
    lines=text.splitlines();out=[];section=None;seen=set()
    for line in lines:
        header=re.fullmatch(r'\[(\w+)\]',line.strip())
        if header:
            if section=='Service':
                out.extend(f'{k}={v}' for k,v in values.items() if k not in seen)
            section=header[1]
        elif section=='Service':
            key=line.split('=',1)[0].strip()
            if key in values:
                if key in seen:continue
                line=f'{key}={values[key]}';seen.add(key)
        out.append(line)
    if section=='Service':out.extend(f'{k}={v}' for k,v in values.items() if k not in seen)
    return '\n'.join(out)+'\n'


def service_value(text,key):
    """A key's value inside the [Service] section, or None."""
    section=None
    for line in text.splitlines():
        header=re.fullmatch(r'\[(\w+)\]',line.strip())
        if header:section=header[1]
        elif section=='Service' and '=' in line and line.split('=',1)[0].strip()==key:return line.split('=',1)[1].strip()
    return None


def with_program(command,program,name):
    """ExecStart with its program swapped for `program` and the arguments kept as they are."""
    current,_,arguments=(command or '').partition(' ')
    if not current.startswith('/') or Path(current).name!=name:
        raise ValueError(f'유닛의 ExecStart가 예상한 {name} 실행이 아닙니다: {command!r}')
    return f'{program} {arguments}'.rstrip()


def healthy(seconds=60):
    deadline=time.time()+seconds
    while time.time()<deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:8766/healthz',timeout=2) as response:
                if response.status==200:return True
        except OSError:pass
        time.sleep(1)
    return False


def collector_running(settle=15):
    """Still running after `settle` seconds: Restart=always would otherwise hide a crash loop."""
    time.sleep(settle)
    shown=subprocess.run(['systemctl','show','llm-usage-collector.service','-p','ActiveState','-p','SubState','-p','NRestarts'],
                         check=True,text=True,stdout=subprocess.PIPE).stdout
    state=dict(line.split('=',1) for line in shown.splitlines() if '=' in line)
    return state.get('ActiveState')=='active' and state.get('SubState')=='running' and state.get('NRestarts')=='0'


def restart_and_check():
    """None when both services came back; otherwise what failed."""
    if subprocess.run(['systemctl','restart','llm-usage.service']).returncode!=0:return '재시작'
    if not healthy():return '웹 health'
    if not collector_running():return '수집기 실행'
    return None


def restore(backup):
    """Put back the unit files this run replaced, then start the services from them."""
    restored=[unit for unit in (WEB,COLLECTOR) if (backup/unit.name).exists()]
    for unit in restored:shutil.copy2(backup/unit.name,unit)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','restart','llm-usage.service'])
    return restored


def install_sudoers(owner,backup):
    text=sudoers_text(owner)
    if SUDOERS.exists():
        if SUDOERS.read_text()==text:return False
        shutil.copy2(SUDOERS,backup/'sudoers-llm-usage')
    with tempfile.NamedTemporaryFile('w',dir=SUDOERS.parent,delete=False,prefix='.llm-usage-') as handle:
        handle.write(text);candidate=Path(handle.name)
    try:
        candidate.chmod(0o440)
        subprocess.run(['visudo','-cf',str(candidate)],check=True,stdout=subprocess.DEVNULL)
        os.replace(candidate,SUDOERS)
    finally:
        candidate.unlink(missing_ok=True)
    return True


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--owner',help='Non-root service owner (defaults to SUDO_USER)')
    parser.add_argument('--no-restart',action='store_true')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--release',action='store_true',help='Run the units from the release built by deploy.py --prepare')
    mode.add_argument('--checkout',action='store_true',help='Run the units from this checkout again')
    args=parser.parse_args()
    if os.geteuid()!=0:parser.error('sudo로 실행하세요.')
    try:owner=resolve_owner(args.owner,PROJECT)
    except ValueError as exc:parser.error(str(exc))
    args.owner=owner.pw_name
    release=service_path(Path(owner.pw_dir)/'.local/lib/llm-usage/current')
    environment=release/'.venv'
    # File checks only: as root, never run or import anything from the owner's release.
    if args.release and not ((release/'llm_usage/webapp.py').is_file() and (release/ENVIRONMENT_MARKER).is_file()
                             and (environment/'bin/python').is_file() and (environment/'bin/gunicorn').is_file()):
        parser.error('릴리스나 그 실행 환경이 없습니다. 소유자 계정으로 packaging/deploy.py --prepare 를 먼저 실행하세요.')
    web_values={}
    if args.release or args.checkout:
        directory=str(release if args.release else PROJECT)
        venv=environment if args.release else PROJECT/'.venv'
        try:web_values=dict(WorkingDirectory=directory,
                            ExecStart=with_program(service_value(WEB.read_text(),'ExecStart'),f'{venv}/bin/gunicorn','gunicorn'))
        except ValueError as exc:parser.error(str(exc))
    # Unique per run: a failed check restores exactly the files this run replaced.
    BACKUPS.mkdir(parents=True,exist_ok=True,mode=0o700)
    backup=Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%d-%H%M%S-'),dir=BACKUPS))
    collector_settings=dict(COLLECTOR_SETTINGS)
    if web_values:
        collector_settings.update(WorkingDirectory=directory,
            ExecStart=f'{venv}/bin/python -m llm_usage.cli collect' if args.release else f'{PROJECT}/.venv/bin/llm-usage collect')
        web_text=WEB.read_text();web_updated=set_service_keys(web_text,web_values)
        if web_updated!=web_text:
            shutil.copy2(WEB,backup/WEB.name);WEB.write_text(web_updated);WEB.chmod(0o644)
            print('웹 유닛 실행 위치: '+directory)
    current=COLLECTOR.read_text();updated=set_service_keys(current,collector_settings)
    if updated!=current:
        shutil.copy2(COLLECTOR,backup/COLLECTOR.name)
        COLLECTOR.write_text(updated);COLLECTOR.chmod(0o644)
        print('수집기 유닛 갱신: '+', '.join(f'{k}={v}' for k,v in collector_settings.items()))
    else:print('수집기 유닛: 이미 적용됨')
    print('sudoers 설치' if install_sudoers(args.owner,backup) else 'sudoers: 이미 적용됨')
    subprocess.run(['systemctl','daemon-reload'],check=True)
    if not args.no_restart:
        problem=restart_and_check()
        if problem:
            restored=restore(backup)
            sys.exit(f'{problem} 확인 실패: '+(f'바꾸기 전 유닛({", ".join(u.name for u in restored)})으로 되돌리고 다시 시작했습니다. '
                     if restored else '이번에 바꾼 유닛이 없어 그대로 다시 시작했습니다. ')
                     +f'백업: {backup}. systemctl status와 journalctl로 원인을 확인하세요.')
        print('웹·수집기 재시작, health와 수집기 실행 확인')
    else:print('재시작하지 않았습니다. 직접 재시작한 뒤 health와 수집기를 확인하세요.')
    if not any(backup.iterdir()):backup.rmdir()
    else:print('백업: '+str(backup))


if __name__=='__main__':main()

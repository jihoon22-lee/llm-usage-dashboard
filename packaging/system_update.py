#!/usr/bin/env python3
"""Apply root-only settings to an existing install, idempotently.

Run once with sudo after updating the code:
  sudo .venv/bin/python packaging/system_update.py --owner YOUR_USER

- collector unit: Restart=always, WatchdogSec=1800, NotifyAccess=main
- /etc/sudoers.d/llm-usage: the owner may restart only these two units without a
  password, so packaging/deploy.py can reload new code without an interactive sudo
- --release: both units run from ~<owner>/.local/lib/llm-usage/current (built by
  `packaging/deploy.py --prepare` as the owner first), and the collector starts
  with `python -m` so it imports the release, not the editable checkout.
  --checkout switches back to running this checkout directly.
Previous files are backed up under /var/backups/llm-usage/<time>/.
"""
import argparse
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import tempfile
import time

from install_common import resolve_owner, service_path

UNITS=Path('/etc/systemd/system')
WEB=UNITS/'llm-usage.service'
COLLECTOR=UNITS/'llm-usage-collector.service'
PROJECT=Path(__file__).resolve().parents[1]
SUDOERS=Path('/etc/sudoers.d/llm-usage')
COLLECTOR_SETTINGS={'Restart':'always','WatchdogSec':'1800','NotifyAccess':'main'}


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
    if args.release and not (release/'llm_usage/webapp.py').is_file():
        parser.error('릴리스가 없습니다. 소유자 계정으로 packaging/deploy.py --prepare 를 먼저 실행하세요.')
    backup=Path('/var/backups/llm-usage')/time.strftime('%Y%m%d-%H%M%S');backup.mkdir(parents=True,mode=0o700)
    collector_settings=dict(COLLECTOR_SETTINGS)
    if args.release or args.checkout:
        directory=str(release if args.release else PROJECT)
        collector_settings.update(WorkingDirectory=directory,
            ExecStart=f'{PROJECT}/.venv/bin/python -m llm_usage.cli collect' if args.release else f'{PROJECT}/.venv/bin/llm-usage collect')
        web_text=WEB.read_text();web_updated=set_service_keys(web_text,{'WorkingDirectory':directory})
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
        subprocess.run(['systemctl','restart','llm-usage.service'],check=True)
        print('웹·수집기 재시작')
    if not any(backup.iterdir()):backup.rmdir()
    else:print('백업: '+str(backup))


if __name__=='__main__':main()

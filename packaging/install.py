#!/usr/bin/env python3
"""Install only service units and the dedicated Serve port. No app dependencies on WRG."""
import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

from install_common import prepare_data_directory, resolve_owner, service_path

PROJECT=Path(__file__).resolve().parents[1]
UNITS=Path('/etc/systemd/system')
BACKUPS=Path('/var/backups/llm-usage')


def run(*args,capture=False):
    return subprocess.run(args,check=True,text=True,stdout=subprocess.PIPE if capture else None).stdout


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--owner',help='Non-root service owner (defaults to SUDO_USER)')
    p.add_argument('--resource-guard',action='store_true',help='Register with the installed Resource Guard service controller')
    args=p.parse_args()
    if os.geteuid()!=0:p.error('관리자 권한으로 실행하세요.')
    try:owner=resolve_owner(args.owner,PROJECT)
    except ValueError as exc:p.error(str(exc))
    args.owner=owner.pw_name;home=Path(owner.pw_dir)
    config_path=home/'.config/llm-usage/config.json';config=json.loads(config_path.read_text())
    try:data_dir=service_path(config['database']).parent
    except (KeyError,ValueError):p.error('설정의 database에 안전한 절대 경로가 필요합니다.')
    status=json.loads(run('tailscale','status','--json',capture=True));node=status['Self']
    dns=node['DNSName'].rstrip('.');login=status['User'][str(node['UserID'])]['LoginName']
    if config['origin']!=f'https://{dns}:9444' or config['allowed_logins']!=[login]:raise RuntimeError('Tailscale 소유자/주소 불일치')
    serve=json.loads(run('tailscale','serve','status','--json',capture=True))
    target=f'{dns}:9444';proxy=serve.get('Web',{}).get(target,{}).get('Handlers',{}).get('/',{}).get('Proxy')
    if '9444' in serve.get('TCP',{}) and proxy not in ('unix:/run/llm-usage/http.sock','http+unix:///run/llm-usage/http.sock'):
        raise RuntimeError('9444 포트 사용 중')
    if serve.get('AllowFunnel',{}).get(target):raise RuntimeError('9444 공개 Funnel 설정을 먼저 해제해야 합니다.')
    if not (UNITS/'llm-usage.service').exists():
        with socket.socket() as probe:probe.bind(('127.0.0.1',8766))
    try:prepare_data_directory(data_dir,owner)
    except ValueError as exc:p.error(str(exc))
    backup=BACKUPS/time.strftime('%Y%m%d-%H%M%S')
    backup.mkdir(parents=True,mode=0o700)
    (backup/'tailscale-serve.json').write_text(json.dumps(serve))
    env=f'Environment=LLM_USAGE_CONFIG={config_path}\nEnvironment=HOME={home}\nEnvironment=PATH={home}/.local/bin:/usr/local/bin:/usr/bin:/bin\n'
    common=f'''Type=simple
User={args.owner}
Group={owner.pw_gid}
WorkingDirectory={PROJECT}
{env}RestartSec=5
TimeoutStopSec=20
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=read-only
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
CapabilityBoundingSet=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
UMask=0077
TasksMax=96
'''
    web=f'''[Unit]
Description=LLM Usage
Wants=llm-usage-collector.service
After=network.target
[Service]
{common}Restart=on-failure
ExecStart={PROJECT}/.venv/bin/gunicorn --no-control-socket --workers 1 --threads 4 --timeout 120 --umask 0077 --bind 127.0.0.1:8766 --bind unix:/run/llm-usage/http.sock llm_usage.webapp:create_app()
RuntimeDirectory=llm-usage
RuntimeDirectoryMode=0700
ReadWritePaths={data_dir}
MemoryHigh=192M
MemoryMax=384M
[Install]
WantedBy=multi-user.target
'''
    collector=f'''[Unit]
Description=LLM Usage background collector
PartOf=llm-usage.service
After=network-online.target
[Service]
{common}# A signal from outside systemd (e.g. a stray kill) must not leave collection stopped;
# systemctl stop and the web unit's PartOf stop still stop it.
Restart=always
# The collector pings the watchdog every loop; a hung loop is restarted too.
WatchdogSec=1800
NotifyAccess=main
ExecStart={PROJECT}/.venv/bin/llm-usage collect
ReadWritePaths={data_dir} -{home}/.codex
MemoryHigh=512M
MemoryMax=1024M
Nice=10
'''
    for name,content in [('llm-usage.service',web),('llm-usage-collector.service',collector)]:
        dest=UNITS/name
        if dest.exists():shutil.copy2(dest,backup/name)
        dest.write_text(content);dest.chmod(0o644)
    run('systemctl','daemon-reload')
    run('systemctl','enable','llm-usage.service')
    run('systemctl','restart','llm-usage.service')
    for attempt in range(60):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8766/healthz',timeout=2) as response:
                if response.status==200:break
        except OSError:time.sleep(.25)
    else:raise RuntimeError('LLM Usage 서버 상태 점검 실패')
    run('tailscale','serve','--bg','--https=9444','unix:/run/llm-usage/http.sock')
    if args.resource_guard:
        dest=Path('/opt/wsl-resource-guard')
        if not (dest/'wsl_resource_guard/service_control.py').exists():raise RuntimeError('Resource Guard 서비스 관리 설치가 필요합니다.')
        sys.path.insert(0,str(dest))
        from wsl_resource_guard.services import request_control, ServiceError
        for attempt in range(60):
            try:
                registered=request_control({'op':'list'})
                break
            except ServiceError:time.sleep(.25)
        else:raise RuntimeError('Resource Guard 서비스 관리자 준비 실패')
        if not any(row['id']=='llm-usage' for row in registered['services']):
            request_control({'op':'register','key':'systemd:llm-usage.service','name':'LLM Usage','url':config['origin']+'/'})
    print('Installed: '+config['origin']+'/')
    print('Backup: '+str(backup))


if __name__=='__main__':main()

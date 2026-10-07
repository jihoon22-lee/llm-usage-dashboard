import argparse
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import subprocess

from .config import atomic_json,config_path,settings
from .store import Store


def _windows_path(path):
    text=str(path)
    match=re.fullmatch(r'/mnt/([a-zA-Z])/(.+)',text)
    if not match or any(part in ('.','..','') for part in match[2].split('/')) or any(
            ch in text for ch in '\\"%!&|<>^`$') or any(ch.isspace() and ch!=' ' or ord(ch)<32 or ord(ch)==127 for ch in text):
        raise ValueError('Windows 홈은 안전한 절대 /mnt/<drive>/... 경로여야 합니다.')
    return match[1].upper()+':\\'+match[2].replace('/','\\')


def windows_homes(values):
    """Only explicit, existing WSL drive paths with a verified Windows conversion."""
    homes=[];seen=set()
    for value in values or []:
        expected=_windows_path(value);home=Path(value)
        if not home.is_dir():raise ValueError('Windows 홈 디렉터리가 존재하지 않습니다.')
        try:converted=subprocess.check_output(['wslpath','-w',str(home)],text=True,stderr=subprocess.DEVNULL).rstrip('\r\n')
        except (OSError,subprocess.CalledProcessError):raise ValueError('wslpath로 Windows 홈을 확인할 수 없습니다.') from None
        if converted.casefold()!=expected.casefold():raise ValueError('Windows 홈 변환 결과가 일치하지 않습니다.')
        key=converted.casefold()
        if key not in seen:homes.append(home);seen.add(key)
    return homes


def windows_hook_command(wrapper,route,directory):
    # Quotes protect spaces; expansion/control characters are rejected above.
    return f'python "{_windows_path(wrapper)}" --route {route} --directory "{_windows_path(directory)}"'


def initialize(selected_windows_homes=None):
    path=config_path()
    if path.exists():return
    homes=[Path.home(),*windows_homes(selected_windows_homes)]
    status=json.loads(subprocess.check_output(['tailscale','status','--json'],text=True))
    node=status['Self'];login=status['User'][str(node['UserID'])]['LoginName']
    if login=='tagged-devices':raise ValueError('소유자 Tailscale 계정을 설정해야 합니다.')
    sources=[];inboxes=[]
    for home in homes:
        platform='WSL' if home==Path.home() else 'Windows'
        for route,rel in [('codex','.codex/sessions'),('codex','.codex/archived_sessions'),('claude-code','.claude/projects')]:
            if (home/rel).exists():sources.append(dict(name=platform+' '+rel,route=route,kind='jsonl',path=str(home/rel)))
        for rel in ('.local/share/opencode/opencode.db','AppData/Roaming/opencode/opencode.db'):
            if (home/rel).exists():sources.append(dict(name=platform+' OpenCode',route='opencode-go',kind='opencode',path=str(home/rel)))
        for rel in ('.local/share/devin/cli/sessions.db','AppData/Roaming/devin/cli/sessions.db'):
            if (home/rel).exists():sources.append(dict(name=platform+' Devin',route='devin',kind='devin',path=str(home/rel)))
        inboxes.append(str(home/'.local/share/llm-usage/status'))
    atomic_json(path,dict(origin='https://'+node['DNSName'].rstrip('.')+':9444',allowed_logins=[login],secret_key=secrets.token_urlsafe(48),
        database=str(Path.home()/'.local/share/llm-usage/usage.db'),sources=sources,status_inboxes=inboxes,
        homes=[str(h) for h in homes],codex_binary=shutil.which('codex'),opencode_auth=str(Path.home()/'.local/share/opencode/auth.json'),
        devin_auth=str(Path.home()/'.local/share/devin/credentials.toml')))
    print(f'설정 생성: {path}')


def install_hooks(selected_windows_homes=None):
    import shlex
    import time
    from . import statusline
    # A saved collection path never authorizes writing another account's settings.
    current=Path.home();selected=windows_homes(selected_windows_homes)
    homes=([current] if not re.match(r'^/mnt/[A-Za-z]/',str(current)) else [])+selected
    for home in homes:
        windows=home in selected
        directory=home/'.local/share/llm-usage/status'
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        wrapper=directory/'statusline.py';shutil.copy2(statusline.__file__,wrapper)
        for route,relative in [('claude-code','.claude/settings.json'),('antigravity','.gemini/antigravity-cli/settings.json')]:
            config=home/relative
            if not config.exists():continue
            d=json.loads(config.read_text());old=d.get('statusLine',{})
            if 'llm-usage/status' in old.get('command','') or 'llm-usage\\status' in old.get('command',''):continue
            if old.get('type','command')!='command':raise ValueError(f'지원하지 않는 상태줄 형식: {config}')
            backup=config.with_name(config.name+'.llm-usage-backup-'+str(int(time.time())))
            shutil.copy2(config,backup)
            atomic_json(directory/(route+'-original.json'),{'command':old.get('command')})
            if windows:
                command=windows_hook_command(wrapper,route,directory)
            else:command=f'/usr/bin/python3 {shlex.quote(str(wrapper))} --route {route} --directory {shlex.quote(str(directory))}'
            d['statusLine']={**old,'type':'command','command':command}
            if route=='antigravity' and not old:d['statusLine']['stack_with_default']=True
            atomic_json(config,d)
            print(f'상태 표시줄 연결: {config}')


def main():
    parser=argparse.ArgumentParser(description='LLM Usage · 개인 구독 한도와 토큰 사용 기록')
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('usage');p.add_argument('--period',default='7d',choices=['today','7d','30d','all','custom'])
    p.add_argument('--start');p.add_argument('--end');p.add_argument('--granularity',choices=['day','hour','week','month'],default='day')
    p.add_argument('--group',choices=['provider','route','model','project','agent'],default='provider');p.add_argument('--cumulative',action='store_true')
    p.add_argument('--scope',help='kind:value (kind=route,provider,model,project)')
    sub.add_parser('limits');sub.add_parser('brief',help='현재 한도를 한 줄로 출력 (상태줄·프롬프트용)')
    p=sub.add_parser('collect');p.add_argument('--once',action='store_true')
    for command in ('init','install-hooks'):
        p=sub.add_parser(command)
        p.add_argument('--windows-home',action='append',default=[],metavar='PATH',
                       help='Explicit Windows home under /mnt/<drive>/... (repeatable; hooks require this on each run)')
    args=parser.parse_args()
    os.umask(0o077)
    if args.command in ('init','install-hooks'):
        try:
            if args.command=='init':initialize(args.windows_home)
            else:install_hooks(args.windows_home)
        except ValueError as exc:parser.error(str(exc))
        return
    config=settings()
    if args.command=='collect':
        from .collect import run
        run(config,args.once);return
    store=Store(config['database'],thresholds=config.get('thresholds'))
    try:
        if args.command=='brief':
            from .notify import brief_line
            print(brief_line(store.limits()));return
        if args.command=='limits':data=store.limits()
        else:
            from .pricing import load_pricing
            data=store.usage(**{k:v for k,v in vars(args).items() if k!='command'},pricing=load_pricing(config),
                             subscriptions=config.get('subscription_prices'))
    except (ValueError,TypeError,OverflowError) as exc:parser.error(str(exc))
    print(json.dumps(data,ensure_ascii=False,indent=2))


if __name__=='__main__':main()

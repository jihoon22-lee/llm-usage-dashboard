"""Documented Git/tag/editable install, with a temporary home and synthetic Tailscale.

Service-unit installation is covered separately by sandboxed installer tests.
No collector, provider API or host systemd command runs here.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='llm-checkout-') as temporary:
    temp = Path(temporary)
    home = temp / 'home'
    home.mkdir()
    env = {k:v for k,v in os.environ.items() if k not in {'LLM_USAGE_CONFIG', 'VIRTUAL_ENV'} and not k.startswith(('GIT_', 'UV_', 'PIP_', 'PYTHON'))}
    env.update(HOME=str(home), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
    source = temp / 'source'
    source.mkdir()
    if '--working-tree' in sys.argv:
        # Local review before a commit: copy only nonignored source files, never .git or .venv.
        names = subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=root).decode().split('\0')
        for name in filter(None,names):
            original = root / name
            if original.is_file():
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original,target)
    else:
        archive = temp / 'source.tar'
        with archive.open('wb') as output:
            subprocess.run(['git','archive','HEAD'],cwd=root,stdout=output,check=True)
        with tarfile.open(archive) as content:
            content.extractall(source, filter='data')
    version = tomllib.loads((source/'pyproject.toml').read_text())['project']['version']
    def run(command, cwd=source, **kwargs):
        return subprocess.run(command,cwd=cwd,env=env,check=True,**kwargs)
    # A disposable fixture repository provides a tag before a public release exists.
    run(['git','init','--quiet','-b','main'])
    run(['git','add','--all'])
    run(['git','-c','user.name=Install Fixture','-c','user.email=fixture@example.invalid',
         '-c','commit.gpgsign=false','-c','core.hooksPath=/dev/null','commit','--quiet','-m','Synthetic checkout fixture'])
    run(['git','tag','v'+version])
    checkout = temp / 'checkout'
    run(['git','clone','--quiet','--branch','v'+version,str(source),str(checkout)],cwd=temp)
    run(['git','switch','-c','main'],cwd=checkout)
    environment = checkout / '.venv'
    python = environment/'bin/python'
    run(['uv','sync','--locked','--no-dev','--python',sys.executable,'--no-python-downloads'],cwd=checkout)
    run(['uv','pip','check','--python',str(python)],cwd=checkout)
    binaries = temp/'bin'
    binaries.mkdir()
    tailscale = binaries/'tailscale'
    tailscale.write_text('#!'+str(python)+'\nimport json,sys\nassert sys.argv[1:]==["status","--json"]\n'
                        'print(json.dumps({"Self":{"DNSName":"fixture.example.","UserID":1},'
                        '"User":{"1":{"LoginName":"owner@example.invalid"}}}))\n')
    tailscale.chmod(0o700)
    env['PATH'] = str(binaries) + os.pathsep + str(environment/'bin') + os.pathsep + os.defpath
    run([str(environment/'bin/llm-usage'),'init'],cwd=checkout)
    config = json.loads((home/'.config/llm-usage/config.json').read_text())
    assert config['origin']=='https://fixture.example:9444'
    assert config['allowed_logins']==['owner@example.invalid']
    assert config['database']==str(home/'.local/share/llm-usage/usage.db')
    assert config['homes']==[str(home)] and config['sources']==[]
    assert not config.get('windows_homes')
    branch = subprocess.check_output(['git','branch','--show-current'],cwd=checkout,env=env,text=True).strip()
    assert branch=='main'
    run([str(python),'-c','from llm_usage.webapp import create_app; import llm_usage; print("Fresh Git install imports successfully")'],cwd=temp)
print('Tagged Git clone, local main, fresh venv, editable install and synthetic init passed')

"""Release environments: packaging/deploy.py makes one per release, system_update.py points the units at it.

uv, systemctl, sudo and the health checks are replaced by fakes; no service or network is touched."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_deploy_preflight import deploy
from test_public_install import update

ROOT=Path(__file__).resolve().parents[1]
FAKE_UV=f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
Path(os.environ['FAKE_UV_LOG']).write_text(json.dumps(dict(argv=sys.argv[1:], cwd=os.getcwd(),
    env={{k: os.environ.get(k) for k in ('UV_PROJECT_ENVIRONMENT', 'UV_PYTHON_DOWNLOADS', 'VIRTUAL_ENV')}})))
bin = Path(os.environ['UV_PROJECT_ENVIRONMENT'])/'bin'; bin.mkdir(parents=True)
if os.environ.get('FAKE_UV_FAIL'): sys.exit(1)
for name in ('python', 'gunicorn'):
    (bin/name).write_text('#!/bin/sh\\necho 3.14.0\\n'); (bin/name).chmod(0o755)
'''
# The web unit as packaging/install.py writes it (owner and paths generic).
WEB_UNIT='''[Unit]
Description=LLM Usage
Wants=llm-usage-collector.service
[Service]
Type=simple
User=owner
WorkingDirectory={project}
ExecStart={project}/.venv/bin/gunicorn --no-control-socket --workers 1 --threads 4 --timeout 120 --umask 0077 --bind 127.0.0.1:8766 --bind unix:/run/llm-usage/http.sock llm_usage.webapp:create_app()
ReadWritePaths=/home/example/.local/share/llm-usage
[Install]
WantedBy=multi-user.target
'''
COLLECTOR_UNIT='''[Unit]
Description=LLM Usage background collector
PartOf=llm-usage.service
[Service]
Type=simple
User=owner
WorkingDirectory={project}
Restart=always
WatchdogSec=1800
NotifyAccess=main
ExecStart={project}/.venv/bin/llm-usage collect
'''
GUNICORN_ARGUMENTS=('--no-control-socket --workers 1 --threads 4 --timeout 120 --umask 0077 --bind 127.0.0.1:8766 '
                    '--bind unix:/run/llm-usage/http.sock llm_usage.webapp:create_app()')


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.release=self.root/'releases'/'abc';self.release.mkdir(parents=True)
        self.uv=self.root/'uv';self.uv.write_text(FAKE_UV);self.uv.chmod(0o755);self.log=self.root/'uv.json'

    def prepare(self,**env):
        with patch.dict(os.environ,{'FAKE_UV_LOG':str(self.log),'VIRTUAL_ENV':'/somewhere/else',**env}):
            deploy.prepare_environment(self.release,python='/usr/bin/python3.x',uv=(str(self.uv),))

    def test_synced_once_at_the_final_path_from_the_release_lock_without_the_dev_group(self):
        self.prepare()
        call=json.loads(self.log.read_text())
        self.assertEqual(call['argv'],['sync','--locked','--no-dev','--no-install-project','--compile-bytecode','--python','/usr/bin/python3.x'])
        self.assertEqual(call['cwd'],str(self.release))
        self.assertEqual(call['env'],{'UV_PROJECT_ENVIRONMENT':str(self.release/'.venv'),'UV_PYTHON_DOWNLOADS':'never','VIRTUAL_ENV':None})
        self.assertEqual((self.release/deploy.ENVIRONMENT_MARKER).read_text(),f'uv {deploy.UV_VERSION}\npython /usr/bin/python3.x 3.14.0\n')
        # A finished environment may be the running one: it is never synced again.
        self.log.unlink();self.prepare()
        self.assertFalse(self.log.exists())

    def test_a_failed_or_interrupted_sync_leaves_no_environment_to_run(self):
        with self.assertRaises(subprocess.CalledProcessError):self.prepare(FAKE_UV_FAIL='1')
        self.assertFalse((self.release/'.venv').exists());self.assertFalse((self.release/deploy.ENVIRONMENT_MARKER).exists())
        # Left over from an interrupted run: no marker, so it is removed and made again.
        (self.release/'.venv/lib').mkdir(parents=True);(self.release/'.venv/lib/stale').write_text('x')
        self.prepare()
        self.assertFalse((self.release/'.venv/lib/stale').exists());self.assertTrue((self.release/deploy.ENVIRONMENT_MARKER).is_file())

    def test_deployer_stops_before_any_switch_when_the_environment_cannot_be_made(self):
        failure=subprocess.CalledProcessError(1,['uv'])
        with patch.object(deploy,'prepare_environment',side_effect=failure),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:deploy.environment(self.release)
        self.assertIn('current 링크는 바꾸지 않았습니다',str(stopped.exception.code))

    def test_units_count_as_moved_only_when_both_start_from_current_venv(self):
        moved=f'{{ path={self.root}/current/.venv/bin/gunicorn ; argv[]={self.root}/current/.venv/bin/gunicorn --bind x ; ignore_errors=no }}'
        checkout='{ path=/home/example/projects/llm-usage-dashboard/.venv/bin/python ; argv[]=python -m llm_usage.cli collect }'
        for programs,expected in (((moved,moved),True),((moved,checkout),False),(('',''),False)):
            with self.subTest(expected=expected),patch.object(deploy,'unit',side_effect=lambda name,prop:programs[name.endswith('collector.service')]):
                self.assertEqual(deploy.release_environment_mode(self.root),expected)

    def test_uv_version_and_marker_name_match_ci_and_the_unit_updater(self):
        versions=set(re.findall(r'version: "([0-9.]+)"',(ROOT/'.github/workflows/checks.yml').read_text()))
        self.assertEqual(versions,{deploy.UV_VERSION})
        self.assertEqual(deploy.ENVIRONMENT_MARKER,update.ENVIRONMENT_MARKER)


class DeployFlowTests(unittest.TestCase):
    """main() with every service and repository step replaced, to see what runs in which order."""
    def setUp(self):
        self.calls=[];self.target=Path('/r/releases/new');self.current=Path('/r/releases/old')

    def run_main(self,argv,release_mode=True,moved=True):
        record=lambda name:(lambda *a,**k:self.calls.append((name,*a)))
        out=io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sys,'argv',['deploy.py',*argv]))
            stack.enter_context(patch.object(deploy,'deployment_database',return_value=Path('/data/usage.db')))
            stack.enter_context(patch.object(deploy,'release_mode',return_value=release_mode))
            stack.enter_context(patch.object(deploy,'release_environment_mode',return_value=moved))
            stack.enter_context(patch.object(deploy,'run',return_value=SimpleNamespace(stdout='abcdef0123\n')))
            stack.enter_context(patch.object(deploy,'build_release',return_value=self.target))
            stack.enter_context(patch.object(deploy,'current_release',return_value=self.current))
            stack.enter_context(patch.object(deploy,'previous_release',return_value=self.current))
            for name in ('tree_checks','environment','code_checks','switch','deploy_release','checks','restart'):
                stack.enter_context(patch.object(deploy,name,side_effect=record(name)))
            stack.enter_context(contextlib.redirect_stdout(out))
            deploy.main()
        return out.getvalue()

    def test_moved_units_check_the_new_environment_and_keep_the_previous_one_ready(self):
        self.run_main([])
        self.assertEqual(self.calls,[('tree_checks',),('environment',self.target),('code_checks',str(self.target/'.venv/bin/python')),
                                     ('environment',self.current),('deploy_release',self.target,self.current,'abcdef0',Path('/data/usage.db'))])

    def test_units_still_on_the_checkout_venv_are_checked_with_it_and_told_how_to_move(self):
        printed=self.run_main([],moved=False)
        self.assertEqual(self.calls,[('tree_checks',),('code_checks',str(deploy.PROJECT/'.venv/bin/python')),
                                     ('deploy_release',self.target,self.current,'abcdef0',Path('/data/usage.db'))])
        self.assertIn('sudo .venv/bin/python packaging/system_update.py --owner $USER --release',printed)

    def test_prepare_makes_and_checks_the_environment_before_selecting_the_release(self):
        printed=self.run_main(['--prepare'],release_mode=False,moved=False)
        self.assertEqual(self.calls,[('tree_checks',),('environment',self.target),('code_checks',str(self.target/'.venv/bin/python')),
                                     ('switch',self.target)])
        self.assertNotIn('경고',printed)

    def test_prepare_warns_when_running_release_units_would_pick_up_other_code(self):
        self.assertIn('먼저 일반 배포한 뒤 --prepare',self.run_main(['--prepare'],moved=False))
        # The documented order: the commit is deployed first, so --prepare only adds its environment.
        self.current=self.target
        self.assertNotIn('경고',self.run_main(['--prepare'],moved=False))

    def test_rollback_gives_an_older_release_an_environment_only_for_moved_units(self):
        for moved in (True,False):
            with self.subTest(moved=moved):
                self.calls.clear();self.run_main(['--rollback'],moved=moved)
                expected=[('deploy_release',self.current,self.current,'롤백',Path('/data/usage.db'))]
                self.assertEqual(self.calls,([('environment',self.current)] if moved else [])+expected)


class UnitUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.home=self.root/'home';self.project=self.root/'project'
        self.units=self.root/'units';self.units.mkdir();self.project.mkdir()
        self.web=self.units/'llm-usage.service';self.collector=self.units/'llm-usage-collector.service'
        self.web.write_text(WEB_UNIT.format(project=self.project));self.collector.write_text(COLLECTOR_UNIT.format(project=self.project))
        self.originals=(self.web.read_text(),self.collector.read_text())
        self.release=self.home/'.local/lib/llm-usage/releases/abc';(self.release/'llm_usage').mkdir(parents=True)
        (self.release/'llm_usage/webapp.py').write_text('')
        os.symlink(self.release,self.home/'.local/lib/llm-usage/current')
        self.current=self.home/'.local/lib/llm-usage/current'

    def ready(self):
        (self.release/'.venv/bin').mkdir(parents=True)
        for name in ('python','gunicorn'):(self.release/'.venv/bin'/name).write_text('')
        (self.release/update.ENVIRONMENT_MARKER).write_text('uv\n')

    def run_update(self,argv,check=None):
        owner=SimpleNamespace(pw_name='owner',pw_uid=os.getuid() or 1000,pw_gid=os.getgid(),pw_dir=str(self.home))
        commands=[]
        def run(args,**kwargs):
            commands.append(args);return subprocess.CompletedProcess(args,0,stdout='')
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sys,'argv',['system_update.py','--owner','owner',*argv]))
            stack.enter_context(patch.object(os,'geteuid',return_value=0))
            stack.enter_context(patch.object(update.pwd,'getpwnam',return_value=owner))
            for name,value in (('PROJECT',self.project),('WEB',self.web),('COLLECTOR',self.collector),('BACKUPS',self.root/'backups')):
                stack.enter_context(patch.object(update,name,value))
            stack.enter_context(patch.object(update,'install_sudoers',return_value=False))
            stack.enter_context(patch.object(update.subprocess,'run',side_effect=run))
            stack.enter_context(patch.object(update,'restart_and_check',return_value=check))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()));stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            update.main()
        return commands

    def test_program_swap_keeps_the_arguments_and_refuses_anything_else(self):
        command=update.service_value(self.web.read_text(),'ExecStart')
        self.assertEqual(update.with_program(command,'/new/.venv/bin/gunicorn','gunicorn'),'/new/.venv/bin/gunicorn '+GUNICORN_ARGUMENTS)
        for command in ('/usr/bin/python -m gunicorn x','-/x/.venv/bin/gunicorn x','gunicorn x','',None):
            with self.subTest(command=command),self.assertRaises(ValueError):update.with_program(command,'/new/gunicorn','gunicorn')

    def test_release_without_a_finished_environment_changes_nothing(self):
        for partial in ('none','no-marker'):
            with self.subTest(partial=partial):
                if partial=='no-marker':
                    (self.release/'.venv/bin').mkdir(parents=True,exist_ok=True)
                    for name in ('python','gunicorn'):(self.release/'.venv/bin'/name).write_text('')
                with self.assertRaises(SystemExit):self.run_update(['--release'])
                self.assertEqual((self.web.read_text(),self.collector.read_text()),self.originals)
                self.assertFalse((self.root/'backups').exists())

    def test_release_and_checkout_point_both_units_at_their_environment(self):
        self.ready();self.run_update(['--release'])
        self.assertEqual(update.service_value(self.web.read_text(),'WorkingDirectory'),str(self.current))
        self.assertEqual(update.service_value(self.web.read_text(),'ExecStart'),f'{self.current}/.venv/bin/gunicorn '+GUNICORN_ARGUMENTS)
        self.assertEqual(update.service_value(self.collector.read_text(),'ExecStart'),f'{self.current}/.venv/bin/python -m llm_usage.cli collect')
        self.assertEqual(update.service_value(self.collector.read_text(),'WorkingDirectory'),str(self.current))
        self.run_update(['--checkout'])
        self.assertEqual((self.web.read_text(),self.collector.read_text()),self.originals)

    def test_failed_check_after_the_restart_puts_the_previous_units_back(self):
        self.ready()
        with self.assertRaises(SystemExit) as stopped:self.run_update(['--release'],check='웹 health')
        self.assertIn('되돌리고 다시 시작했습니다',str(stopped.exception.code))
        self.assertEqual((self.web.read_text(),self.collector.read_text()),self.originals)

    def test_collector_counts_as_running_only_while_the_same_process_stays_up(self):
        up='ActiveState=active\nSubState=running\nMainPID={}\n'
        for first,last,expected in ((up.format(41),up.format(41),True),
                                    (up.format(41),up.format(57),False),  # restarted by Restart=always in between
                                    (up.format(41),'ActiveState=activating\nSubState=auto-restart\nMainPID=0\n',False),
                                    (up.format(0),up.format(0),False)):
            replies=iter([first,last])
            with self.subTest(first=first,last=last),patch.object(update.time,'sleep'), \
                    patch.object(update.subprocess,'run',side_effect=lambda *a,**k:subprocess.CompletedProcess([],0,stdout=next(replies))):
                self.assertEqual(update.collector_running(),expected)


if __name__=='__main__':unittest.main()

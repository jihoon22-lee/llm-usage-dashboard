"""Installer tests use temp files and fake passwd/Tailscale, never system services."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PACKAGING=Path(__file__).resolve().parents[1]/'packaging'
sys.path.insert(0,str(PACKAGING))

def load_script(name):
    spec=importlib.util.spec_from_file_location('public_'+name,PACKAGING/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

install=load_script('install');update=load_script('system_update')


class InstallerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.home=self.root/'home';self.home.mkdir()
        self.owner=SimpleNamespace(pw_name='fixture',pw_uid=os.getuid() or 1000,pw_gid=os.getgid(),pw_dir=str(self.home))
        # A validation regression must fail the test before touching host directories,
        # including when the CI runner itself happens to be root.
        for method in ('mkdir','write_text'):
            original=getattr(Path,method)
            def guarded(path,*args,_original=original,**kwargs):
                if not path.is_relative_to(self.root):raise AssertionError('write outside installer fixture')
                return _original(path,*args,**kwargs)
            guard=patch.object(Path,method,guarded);guard.start();self.addCleanup(guard.stop)

    def test_missing_root_nonexistent_or_unsafe_owner_stops_before_service_commands(self):
        for module in (install,update):
            for name in (None,'root','bad name','bad%name','missing'):
                with self.subTest(script=module.__name__,name=name),contextlib.ExitStack() as stack:
                    stack.enter_context(patch.dict(os.environ,{},clear=True))
                    stack.enter_context(patch.object(sys,'argv',['script']+(['--owner',name] if name else [])))
                    stack.enter_context(patch.object(os,'geteuid',return_value=0))
                    account=SimpleNamespace(**{**vars(self.owner),'pw_name':name,'pw_uid':0 if name=='root' else self.owner.pw_uid})
                    stack.enter_context(patch.object(module.pwd,'getpwnam',side_effect=KeyError() if name=='missing' else None,return_value=account))
                    run=stack.enter_context(patch.object(module.subprocess,'run',side_effect=AssertionError('service side effect')))
                    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                    with self.assertRaises(SystemExit):module.main()
                    run.assert_not_called()

    def test_unsafe_home_or_project_stops_before_config_read_or_side_effects(self):
        for module in (install,update):
            for home,project in ((str(self.home)+' unsafe',self.root),(str(self.home),self.root/'unsafe%project')):
                with self.subTest(script=module.__name__,home=home,project=project),contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(sys,'argv',['script','--owner','fixture']))
                    stack.enter_context(patch.object(os,'geteuid',return_value=0))
                    stack.enter_context(patch.object(module.pwd,'getpwnam',return_value=SimpleNamespace(**{**vars(self.owner),'pw_dir':home})))
                    stack.enter_context(patch.object(module,'PROJECT',project))
                    stack.enter_context(patch.object(module.subprocess,'run',side_effect=AssertionError('service side effect')))
                    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                    with self.assertRaises(SystemExit):module.main()

    def test_sudo_user_selects_nonroot_owner(self):
        from install_common import resolve_owner
        with patch.dict(os.environ,{'SUDO_USER':'fixture'}),patch('pwd.getpwnam',return_value=self.owner):
            self.assertEqual(resolve_owner(None,self.root),self.owner)

    def test_data_directory_is_private_owned_and_rejects_symlinks(self):
        from install_common import prepare_data_directory
        owner=SimpleNamespace(pw_uid=os.getuid(),pw_gid=os.getgid())
        target=self.home/'.local/share/llm-usage'
        prepare_data_directory(target,owner)
        self.assertEqual(target.stat().st_uid,os.getuid())
        self.assertEqual(target.stat().st_mode&0o777,0o700)
        self.assertEqual((self.home/'.local').stat().st_mode&0o777,0o700)
        target.chmod(0o755);prepare_data_directory(target,owner)
        self.assertEqual(target.stat().st_mode&0o777,0o700)
        link=self.home/'link';link.symlink_to(target,target_is_directory=True)
        with self.assertRaises(ValueError):prepare_data_directory(link/'child',owner)
        self.assertFalse((target/'child').exists())

    def test_collector_optional_codex_path_does_not_require_codex_install(self):
        # Complete installation against a filesystem sandbox, replacing only external I/O.
        config_dir=self.home/'.config/llm-usage';config_dir.mkdir(parents=True)
        (config_dir/'config.json').write_text(json.dumps({'origin':'https://fixture.example:9444',
            'allowed_logins':['owner'],'database':str(self.home/'.local/share/llm-usage/usage.db')}))
        units=self.root/'units';units.mkdir();backups=self.root/'backups'
        def run(*args,**kwargs):
            if args[:2]==('tailscale','status'):return json.dumps({'Self':{'DNSName':'fixture.example.','UserID':1},'User':{'1':{'LoginName':'owner'}}})
            if args[:3]==('tailscale','serve','status'):return '{}'
        with patch.object(sys,'argv',['script','--owner','fixture']),patch.object(os,'geteuid',return_value=0),\
             patch.object(install.pwd,'getpwnam',return_value=self.owner),patch.object(install,'PROJECT',self.root),\
             patch.object(install,'UNITS',units,create=True),patch.object(install,'BACKUPS',backups,create=True),\
             patch.object(install,'run',side_effect=run),patch.object(install.socket,'socket'),\
             patch.object(install.urllib.request,'urlopen') as urlopen,patch('os.chown'):
            urlopen.return_value.__enter__.return_value.status=200
            install.main()
        text=(units/'llm-usage-collector.service').read_text()
        self.assertIn(' -'+str(self.home/'.codex'),text)
        self.assertTrue((self.home/'.local/share/llm-usage').is_dir())
        self.assertFalse((self.home/'.codex').exists())


if __name__=='__main__':unittest.main()

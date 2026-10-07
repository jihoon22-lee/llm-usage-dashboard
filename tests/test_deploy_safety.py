"""Deployment reads the service's DB and never signals a process group."""
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import pwd
import shlex
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from test_deploy_preflight import deploy

class DeploySafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.config=self.root/'config.json';self.database=self.root/'custom.db'
        self.config.write_text(json.dumps({'database':str(self.database)}))
        self.user=pwd.getpwuid(os.getuid()).pw_name

    def unit(self,name,prop):
        return {'Environment':'LLM_USAGE_CONFIG='+shlex.quote(str(self.config)),
                'EnvironmentFiles':'','User':self.user,'MainPID':'0'}.get(prop,'')

    def test_service_database_and_heartbeat_use_custom_configuration(self):
        import sqlite3,time
        with closing(sqlite3.connect(self.database)) as c,c:
            c.execute('CREATE TABLE state(key TEXT PRIMARY KEY,data TEXT)')
            c.execute('INSERT INTO state VALUES (?,?)',('collector',json.dumps({'checked':time.time()})))
        with patch.object(deploy,'unit',side_effect=self.unit),patch.dict(os.environ,{},clear=True):
            database=deploy.deployment_database()
        self.assertEqual(database,self.database)
        self.assertGreater(deploy.heartbeat(database),0)
        self.assertFalse((self.root/'usage.db').exists())

    def test_service_mismatch_invalid_config_and_shell_override_rejected(self):
        other=self.root/'other.json';other.write_text(json.dumps({'database':str(self.root/'other.db')}))
        for case in ('units','override','relative','invalid','envfile'):
            with self.subTest(case=case):
                self.config.write_text(json.dumps({'database':'relative.db' if case=='relative' else str(self.database)}))
                if case=='invalid':self.config.write_text('{bad')
                def unit(name,prop):
                    if case=='units' and name.endswith('collector.service') and prop=='Environment':return 'LLM_USAGE_CONFIG='+str(other)
                    if case=='envfile' and prop=='EnvironmentFiles':return '/some/file (ignore_errors=no)'
                    return self.unit(name,prop)
                with patch.object(deploy,'unit',side_effect=unit),patch.dict(os.environ,
                        {'LLM_USAGE_CONFIG':str(other)} if case=='override' else {},clear=True):
                    with self.assertRaises(ValueError):deploy.deployment_database()

    def test_preflight_failure_prevents_service_and_release_mutation(self):
        with patch.object(sys,'argv',['deploy.py']),patch.object(deploy,'deployment_database',side_effect=ValueError('fixture')), \
                patch.object(deploy,'switch') as switch,patch.object(deploy,'restart') as restart:
            with self.assertRaises(SystemExit):deploy.main()
            switch.assert_not_called();restart.assert_not_called()

    def test_default_config_uses_service_home_and_missing_database_is_not_created(self):
        directory=self.root/'.config/llm-usage';directory.mkdir(parents=True)
        (directory/'config.json').write_text(json.dumps({'database':str(self.database)}))
        def unit(name,prop):
            return 'HOME='+str(self.root) if prop=='Environment' else self.unit(name,prop)
        with patch.object(deploy,'unit',side_effect=unit),patch.dict(os.environ,{},clear=True):
            self.assertEqual(deploy.deployment_database(),self.database)
        self.assertEqual(deploy.heartbeat(self.database),0)
        self.assertFalse(self.database.exists())

    def test_nonpositive_or_invalid_pid_never_sends_a_signal(self):
        for pid in ('0','-1','','broken'):
            with self.subTest(pid=pid),ExitStack() as stack:
                stack.enter_context(patch.object(sys,'argv',['deploy.py']))
                stack.enter_context(patch.object(deploy,'checks'))
                stack.enter_context(patch.object(deploy,'deployment_database',return_value=self.database,create=True))
                stack.enter_context(patch.object(deploy,'run',return_value=SimpleNamespace(stdout='head')))
                stack.enter_context(patch.object(deploy,'release_mode',return_value=False))
                stack.enter_context(patch.object(deploy,'started',return_value=1))
                stack.enter_context(patch.object(deploy,'restart',return_value='denied'))
                stack.enter_context(patch.object(deploy,'unit',return_value=pid))
                stack.enter_context(patch.object(deploy,'wait',return_value=True))
                signal=stack.enter_context(patch.object(deploy.os,'kill'))
                with self.assertRaises(SystemExit):deploy.main()
                signal.assert_not_called()

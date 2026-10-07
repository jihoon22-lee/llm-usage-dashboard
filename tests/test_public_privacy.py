"""Public-install boundaries: no real credentials, accounts, services or usage DBs."""
import json
from pathlib import Path
import socket
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import SplitResult

from llm_usage import cli, notify
from llm_usage.store import Store, project_label
from llm_usage.webapp import create_app


class NotificationPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.origin='https://pc.example.ts.net:9444'
        self.config=dict(database=str(self.root/'usage.db'),origin=self.origin,secret_key='test',allowed_logins=['owner'],
                         config_file=str(self.root/'config.json'),notify={'webhook_url':'https://hooks.example/OLD_SECRET'})
        self.client=create_app(self.config).test_client()

    def post(self,value):
        token=self.client.get('/api/bootstrap',base_url=self.origin,headers={'Tailscale-User-Login':'owner'},
            environ_overrides={'REMOTE_ADDR':'','gunicorn.socket':SimpleNamespace(family=socket.AF_UNIX)}).json['csrf']
        return self.client.post('/api/config/notify',json=value,base_url=self.origin,
            headers={'Tailscale-User-Login':'owner','Origin':self.origin,'X-CSRF-Token':token},
            environ_overrides={'REMOTE_ADDR':'','gunicorn.socket':SimpleNamespace(family=socket.AF_UNIX)})

    def test_rejects_malformed_or_credentialed_https_without_echoing_secret(self):
        for url in ('https://user:SECRET@host.example/path','https:///SECRET','https://host.example:SECRET/',
                    'https://host.example:70000/SECRET','https://host.example:/SECRET',
                    'https://host.example\n/SECRET',' https://host.example/SECRET','https://host.example/SEC RET',
                    'https://host.example%40SECRET/path','https://host.example\\SECRET/path','https://[bad/SECRET',
                    'https://-bad.example/SECRET','https://host..example/SECRET','https://host.example../SECRET',
                    'https://[::1]SECRET/path','http://host.example/SECRET'):
            with self.subTest(url=url):
                result=self.post({'webhook_url':url})
                self.assertEqual(result.status_code,400)
                self.assertNotIn('SECRET',result.get_data(as_text=True))

    def test_masking_old_bad_values_never_exposes_credentials_or_path(self):
        for url in ('https://user:SECRET@host.example/path','https://host.example:SECRET/',
                    'https://host.example%40SECRET/path','https://[::1]SECRET/path','https://[bad/SECRET'):
            with self.subTest(url=url):
                self.assertEqual(notify.masked({'webhook_url':url})['channels']['webhook'],'설정됨')
        self.assertEqual(notify.masked({'webhook_url':'https://hooks.example:8443/SECRET?q=secret'})['channels']['webhook'],'hooks.example:8443')

    def test_old_urlsplit_ipv6_suffix_cannot_leak_authority_or_send(self):
        # Python 3.11's parser accepted this authority and returned hostname ::1.
        # Build the parsed result directly so newer Python cannot hide the regression.
        parts=SplitResult('https','[::1]SECRET','/path','','')
        with patch.object(notify,'urlsplit',return_value=parts):
            with self.assertRaises(ValueError):notify.notification_url('https://[::1]SECRET/path')
            self.assertEqual(notify.masked({'webhook_url':'https://[::1]SECRET/path'})['channels']['webhook'],'설정됨')
            with patch.object(notify,'_post') as post:
                self.assertEqual(notify.send({'webhook_url':'https://[::1]SECRET/path'},'t','b'),{'webhook':'ValueError'})
                post.assert_not_called()

    def test_valid_ipv6_and_omitted_keeps_empty_clears(self):
        self.assertEqual(self.post({'ntfy_url':'https://[::1]:443/topic'}).status_code,200)
        self.assertEqual(self.post({'events':{'low':False}}).status_code,200)
        saved=json.loads((self.root/'local.json').read_text())['notify']
        self.assertEqual(saved['webhook_url'],'https://hooks.example/OLD_SECRET')
        self.assertEqual(self.post({'webhook_url':''}).status_code,200)
        self.assertNotIn('webhook_url',json.loads((self.root/'local.json').read_text())['notify'])

    def test_send_rejects_invalid_legacy_url_without_network(self):
        with patch.object(notify,'_post') as post:
            self.assertEqual(notify.send({'webhook_url':'https://user:SECRET@host.example/'},'t','b'),{'webhook':'ValueError'})
        post.assert_not_called()


class ProjectPrivacyTests(unittest.TestCase):
    def test_labels_are_only_basename_for_posix_windows_unc_and_generic_folders(self):
        for path,expected in [('/private/person/projects','projects'),('/home','home'),
                ('C:\\Users\\Private\\app','app'),('D:/Private/app/','app'),
                ('\\\\server\\share\\Private\\app\\','app'),('/gone/repo/.claude/worktrees/agent','repo')]:
            with self.subTest(path=path):self.assertEqual(project_label(path),expected)
        for path in (None,'','/','C:\\','D:/','\\\\server\\share\\','//server/share/'):
            with self.subTest(path=path):self.assertIsNone(project_label(path))

    def test_mixed_legacy_and_new_labels_keep_totals_filters_and_budgets_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            database=Path(tmp)/'usage.db';store=Store(database);now=time.time()
            legacy=r'C:\Profiles\Private\app'
            with store.connect() as c:
                store.event(c,'old',now-60,'OpenAI','codex','gpt-test',dict(uncached_input=100,output=10),project=legacy)
                store.event(c,'new',now-30,'OpenAI','codex','gpt-test',dict(uncached_input=200,output=20),project=project_label(legacy))
            # Reopening must not migrate stored labels or alter the aggregate.
            store=Store(database)
            data=store.usage(period='all',group='project',now=now)
            self.assertEqual(data['totals']['uncached_input'],300)
            self.assertEqual(data['totals']['output'],30)
            for project,tokens in ((legacy,110),('app',220)):
                filtered=store.usage(period='all',scope='project:'+project,now=now)
                self.assertEqual(filtered['totals']['uncached_input']+filtered['totals']['output'],tokens)
                detail=store.project_detail(project,now=now,budget={'tokens':1000})
                self.assertAlmostEqual(detail['month']['budget_ratio'],tokens/1000)
            with store.connect() as c:
                self.assertEqual({row[0] for row in c.execute('SELECT project FROM events')},{legacy,'app'})


class WindowsOptInTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)/'linux';self.home.mkdir()
        self.config=self.home/'config.json'
        self.status=json.dumps({'Self':{'UserID':1,'DNSName':'test.example.'},'User':{'1':{'LoginName':'owner'}}})

    def test_init_discovers_only_current_home_and_preserves_existing_config(self):
        with patch.object(cli,'config_path',return_value=self.config),patch.object(Path,'home',return_value=self.home),\
             patch.object(cli.subprocess,'check_output',return_value=self.status),patch.object(Path,'glob',side_effect=AssertionError('cross-user scan')):
            cli.initialize()
            self.assertEqual(json.loads(self.config.read_text())['homes'],[str(self.home)])
            self.config.write_text('{"existing":true}')
            cli.initialize()
            self.assertEqual(self.config.read_text(),'{"existing":true}')

    def test_init_validates_and_deduplicates_explicit_drive_homes(self):
        windows='/mnt/d/Profiles/Example User'
        def output(argv,**kwargs):return 'D:\\Profiles\\Example User\n' if argv[0]=='wslpath' else self.status
        with patch.object(cli,'config_path',return_value=self.config),patch.object(Path,'home',return_value=self.home),\
             patch.object(cli.subprocess,'check_output',side_effect=output),patch.object(Path,'is_dir',return_value=True):
            cli.initialize([windows,windows])
        self.assertEqual(json.loads(self.config.read_text())['homes'],[str(self.home),windows])

    def test_invalid_windows_home_is_rejected_before_config_creation(self):
        for path in ('relative','/home/other','/mnt/c','/mnt/c/../other','/mnt/c/Profiles/%SECRET%','/mnt/c/Profiles/Bad"Name'):
            with self.subTest(path=path),patch.object(cli,'config_path',return_value=self.config):
                with self.assertRaises(ValueError):cli.initialize([path])
                self.assertFalse(self.config.exists())

    def test_hooks_legacy_homes_never_authorize_windows_writes(self):
        with patch.object(cli,'settings',return_value={'homes':[str(self.home),'/mnt/c/Profiles/Other']}),\
             patch.object(Path,'home',return_value=self.home),patch.object(Path,'mkdir') as mkdir,\
             patch.object(cli.shutil,'copy2'),patch.object(Path,'exists',return_value=False):
            cli.install_hooks()
        self.assertEqual(mkdir.call_count,1)

    def test_windows_mounted_current_home_also_requires_explicit_hook_selection(self):
        with patch.object(Path,'home',return_value=Path('/mnt/c/Profiles/Current')),\
             patch.object(Path,'mkdir',side_effect=AssertionError('implicit Windows write')):
            cli.install_hooks()

    def test_hook_settings_change_only_in_current_and_explicitly_selected_homes(self):
        selected=self.home.parent/'selected';unselected=self.home.parent/'unselected'
        for home in (self.home,selected,unselected):
            (home/'.claude').mkdir(parents=True)
            (home/'.claude/settings.json').write_text('{}')
        with patch.object(Path,'home',return_value=self.home),patch.object(cli,'windows_homes',return_value=[selected]),\
             patch.object(cli,'windows_hook_command',return_value='python "D:\\fixture\\statusline.py"'),\
             patch.object(cli,'settings',return_value={'homes':[str(self.home),str(selected),str(unselected)]}):
            cli.install_hooks(['/mnt/d/fixture'])
        for home in (self.home,selected):
            self.assertIn('statusLine',json.loads((home/'.claude/settings.json').read_text()))
            self.assertTrue((home/'.local/share/llm-usage/status/statusline.py').is_file())
        self.assertEqual((unselected/'.claude/settings.json').read_text(),'{}')
        self.assertFalse((unselected/'.local').exists())

    def test_missing_directory_or_mismatched_conversion_never_creates_config(self):
        with patch.object(cli,'config_path',return_value=self.config),patch.object(Path,'is_dir',return_value=False):
            with self.assertRaises(ValueError):cli.initialize(['/mnt/d/Profiles/Example'])
        with patch.object(cli,'config_path',return_value=self.config),patch.object(Path,'is_dir',return_value=True),\
             patch.object(cli.subprocess,'check_output',return_value='C:\\Users\\Other\n'):
            with self.assertRaises(ValueError):cli.initialize(['/mnt/d/Profiles/Example'])
        self.assertFalse(self.config.exists())

    def test_windows_command_supports_other_drive_and_always_quotes_spaces(self):
        # The converter is checked against the requested drive before use in a command.
        with patch.object(cli.subprocess,'check_output',return_value='D:\\Profiles\\Example User\n'),\
             patch.object(Path,'is_dir',return_value=True):
            homes=cli.windows_homes(['/mnt/d/Profiles/Example User'])
        self.assertEqual(homes,[Path('/mnt/d/Profiles/Example User')])
        command=cli.windows_hook_command(homes[0]/'.local/share/llm-usage/status/statusline.py','claude-code',homes[0]/'.local/share/llm-usage/status')
        self.assertIn('"D:\\Profiles\\Example User\\.local\\share\\llm-usage\\status\\statusline.py"',command)
        self.assertNotIn('/mnt/',command)


if __name__=='__main__':unittest.main()

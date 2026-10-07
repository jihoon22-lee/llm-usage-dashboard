"""Read-only source access, rewritten logs, project attribution and service plumbing."""
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from llm_usage.collect import collect_file, sd_notify
from llm_usage.insights import pace_lookback, quota_forecast, quota_plan
from llm_usage.sqlite_ro import open_readonly
from llm_usage.store import Store, project_label, repository_root, stamp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'packaging'))
from system_update import set_service_keys  # noqa: E402


def claude(message, output, cwd='/nowhere/project'):
    return {'type':'assistant','timestamp':'2026-09-08T10:00:00Z','requestId':'r-'+message,'sessionId':'s','cwd':cwd,
            'message':{'id':message,'model':'claude-test','usage':{'input_tokens':1,'output_tokens':output}}}


class ReadOnlySourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name)/'app';self.folder.mkdir()
        self.path=self.folder/'x.db'
        c=sqlite3.connect(self.path)
        c.execute('PRAGMA journal_mode=WAL');c.execute('CREATE TABLE steps(idx INTEGER PRIMARY KEY)');c.execute('INSERT INTO steps VALUES(1)')
        c.commit();c.close()  # closing the last connection removes -wal/-shm
        self.addCleanup(self.folder.chmod,0o755)

    def test_closed_wal_database_in_read_only_folder(self):
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()),['x.db'])
        self.folder.chmod(0o555)
        plain=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):plain.execute('SELECT 1 FROM steps').fetchall()
        finally:plain.close()
        con=open_readonly(self.path)
        try:self.assertEqual(con.execute('SELECT COUNT(*) FROM steps').fetchone()[0],1)
        finally:con.close()
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()),['x.db'])

    def test_open_wal_is_never_read_as_immutable(self):
        (self.folder/'x.db-wal').write_bytes(b'')
        self.folder.chmod(0o555)
        with self.assertRaises(sqlite3.OperationalError):open_readonly(self.path)


class RewrittenLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'usage.db');self.path=self.root/'s.jsonl'

    def write(self,records):
        self.path.write_text(''.join(json.dumps(r)+'\n' for r in records))

    def outputs(self):
        with self.store.connect() as c:
            return dict(c.execute("SELECT model||':'||id,output FROM events").fetchall()), \
                   c.execute('SELECT COUNT(*) FROM import_errors').fetchone()[0]

    def test_rewrite_that_grows_in_place_is_read_again_from_the_start(self):
        self.write([claude('a',10),claude('b',20)])
        self.assertEqual(collect_file(self.store,self.path,'claude-code'),0)
        inode=self.path.stat().st_ino
        # Same inode, a new first line shifts every offset (Claude rewrites its session files).
        with self.path.open('r+') as f:
            f.seek(0);f.write(''.join(json.dumps(r)+'\n' for r in [claude('z',5,'/x/y-long-path-padding'),claude('a',10),claude('b',20),claude('c',30)]))
        self.assertEqual(self.path.stat().st_ino,inode)
        self.assertEqual(collect_file(self.store,self.path,'claude-code'),0)
        events,errors=self.outputs()
        self.assertEqual(sorted(events.values()),[5,10,20,30]);self.assertEqual(errors,0)

    def test_errors_recorded_mid_line_trigger_one_full_reread(self):
        self.write([claude('a',10),claude('b',20)])
        collect_file(self.store,self.path,'claude-code')
        key='file:'+str(self.path)
        with self.store.connect() as c:
            # State left by the old reader: an error at an offset that is not a line start.
            self.store.import_error(c,key,7,'JSONDecodeError')
            c.execute("DELETE FROM events WHERE output=20")
        self.assertEqual(collect_file(self.store,self.path,'claude-code'),0)
        events,errors=self.outputs()
        self.assertEqual(sorted(events.values()),[10,20]);self.assertEqual(errors,0)

    def test_appends_continue_from_the_checkpoint(self):
        self.write([claude('a',10)])
        collect_file(self.store,self.path,'claude-code')
        with self.path.open('a') as f:f.write(json.dumps(claude('b',20))+'\n')
        with patch('llm_usage.collect.parse_line',wraps=__import__('llm_usage.collect',fromlist=['parse_line']).parse_line) as parse:
            collect_file(self.store,self.path,'claude-code')
        self.assertEqual(parse.call_count,1)


class ProjectLabelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();repository_root.cache_clear();self.addCleanup(repository_root.cache_clear)

    def git(self,*args,cwd):
        # A commit hook runs these tests with GIT_DIR/GIT_INDEX_FILE set for the outer repository.
        env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
        subprocess.run(['git',*args],cwd=cwd,check=True,capture_output=True,env=env)

    def test_worktree_and_subfolder_belong_to_the_main_repository(self):
        main=self.base/'tool';(main/'web').mkdir(parents=True)
        self.git('init','-q','-b','main',cwd=main)
        self.git('-c','user.email=t@t','-c','user.name=t','commit','-q','--allow-empty','-m','x',cwd=main)
        self.git('worktree','add','-q','-b','feature',str(self.base/'tool-feature'),cwd=main)
        self.assertEqual(project_label(str(main)),'tool')
        self.assertEqual(project_label(str(main/'web')),'tool')
        self.assertEqual(project_label(str(self.base/'tool-feature')),'tool')
        self.assertEqual(project_label('/gone/repo/.claude/worktrees/agent-1'),'repo')

    def test_paths_outside_a_repository_keep_their_folder_name(self):
        plain=self.base/'notes';plain.mkdir()
        self.assertEqual(project_label(str(plain)),'notes')
        self.assertEqual(project_label('/does/not/exist/thing'),'thing')
        self.assertEqual(project_label('E:\\recovery'),'recovery')
        self.assertEqual(project_label('/home'),'home')


class ForecastWindowTests(unittest.TestCase):
    def test_long_windows_need_a_longer_observation(self):
        self.assertEqual(pace_lookback('seven_day'),(3*3600,90))
        self.assertEqual(pace_lookback('five_hour'),(3600,15))
        now=1000000.0
        row=dict(bucket='seven_day',status='fresh',pace_per_hour=11.8,pace_minutes=15,remaining=82,resets=now+3*86400)
        self.assertIsNone(quota_forecast(row,now))
        self.assertTrue(quota_forecast({**row,'pace_minutes':120},now)['within_window'])
        self.assertIsNotNone(quota_forecast({**row,'bucket':'five_hour','resets':now+3600},now))

    def test_even_pace_plan(self):
        now=1000000.0
        plan=quota_plan(dict(bucket='five_hour',status='fresh',remaining=70,resets=now+3600*3),now)
        self.assertAlmostEqual(plan['elapsed_fraction'],0.4);self.assertAlmostEqual(plan['ahead'],10)
        self.assertIsNone(quota_plan(dict(bucket='3p-unknown',status='fresh',remaining=70,resets=now+60),now))
        self.assertIsNone(quota_plan(dict(bucket='five_hour',status='stale',remaining=70,resets=now+60),now))


class StatusPresentationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'usage.db')

    def test_status_line_without_receipt_for_a_day_is_unused_not_failing(self):
        import time
        with self.store.connect() as c:
            self.store.source(c,'claude-code','ok','',time.time()-3*86400)
            self.store.source(c,'claude-oauth','ok','',time.time()-60)
            self.store.source(c,'codex','ok','',time.time()-3*86400)
        sources={s['name']:s for s in self.store.usage(period='7d')['sources']}
        self.assertEqual(sources['claude-code']['status'],'idle')
        self.assertIn('터미널(TUI)',sources['claude-code']['detail'])
        self.assertEqual(sources['codex']['status'],'stale')

    def test_antigravity_window_not_reported_by_the_app_is_explained(self):
        import time
        now=time.time()
        with self.store.connect() as c:
            self.store.limit(c,'antigravity','3p-5h',100,now+3*3600,now-60,'antigravity-app')
            self.store.limit(c,'antigravity','3p-weekly',0,now+5*86400,now-3600,'antigravity-app')
            self.store.limit(c,'antigravity','gemini-5h',50,now+3*3600,now-60,'antigravity-app')
        rows={r['bucket']:r for r in self.store.limits(now)['limits'] if r['route']=='antigravity'}
        self.assertIn('5시간 창을 보고',rows['3p-weekly']['note'])
        self.assertIsNone(rows['3p-5h']['note']);self.assertIsNone(rows['gemini-5h']['note'])


class ServicePlumbingTests(unittest.TestCase):
    def test_sd_notify_sends_to_the_notify_socket_and_is_quiet_without_it(self):
        with tempfile.TemporaryDirectory() as folder:
            address=os.path.join(folder,'notify')
            with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as server:
                server.bind(address);server.settimeout(2)
                with patch.dict(os.environ,{'NOTIFY_SOCKET':address}):sd_notify('WATCHDOG=1')
                self.assertEqual(server.recv(64),b'WATCHDOG=1')
        with patch.dict(os.environ,{},clear=True):sd_notify('WATCHDOG=1')

    def test_unit_keys_change_only_inside_service_and_are_idempotent(self):
        text='[Unit]\nRestart=no\n[Service]\nType=simple\nRestart=on-failure\nRestart=no\n[Install]\nWantedBy=x\n'
        updated=set_service_keys(text,{'Restart':'always','WatchdogSec':'1800'})
        self.assertEqual(updated,'[Unit]\nRestart=no\n[Service]\nType=simple\nRestart=always\nWatchdogSec=1800\n[Install]\nWantedBy=x\n')
        self.assertEqual(set_service_keys(updated,{'Restart':'always','WatchdogSec':'1800'}),updated)


if __name__=='__main__':unittest.main()

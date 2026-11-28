"""Regression tests for code-review security, concurrency, ops, and Python/JS parity fixes."""

from datetime import datetime
import gzip
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from llm_usage import backup, collect, devin, notify
from llm_usage.store import KST, Store, stamp
from llm_usage.webapp import create_app


class FakeUnixSocket:
    family = socket.AF_UNIX


class SecurityAndConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'usage.db'
        self.cfg_file = self.root / 'config.json'
        self.cfg = {
            'database': str(self.db),
            'config_file': str(self.cfg_file),
            'pricing_file': str(self.root / 'pricing.json'),
            'secret_key': 'test-secret-key',
            'origin': 'https://MyDash.Example.test',
            'allowed_logins': ['owner@example.test'],
            'thresholds': {'low_percent': 15},
            'refresh_seconds': 300,
        }
        self.cfg_file.write_text(json.dumps(self.cfg))
        self.app = create_app(dict(self.cfg))
        self.client = self.app.test_client()

    def _headers(self, host='mydash.example.test', csrf=None):
        h = {'Host': host, 'Tailscale-User-Login': 'owner@example.test'}
        if csrf is not None:
            h.update({'Origin': 'https://MyDash.Example.test', 'X-CSRF-Token': csrf})
        return h

    def _get(self, path, host='mydash.example.test'):
        return self.client.get(
            path, headers=self._headers(host=host), environ_base={'gunicorn.socket': FakeUnixSocket()}
        )

    def _post(self, path, body):
        csrf = self._get('/api/bootstrap').get_json()['csrf']
        return self.client.post(
            path, json=body, headers=self._headers(csrf=csrf), environ_base={'gunicorn.socket': FakeUnixSocket()}
        )

    def test_origin_host_matching_is_case_insensitive(self):
        resp = self._get('/api/bootstrap', host='MYDASH.EXAMPLE.TEST')
        self.assertEqual(resp.status_code, 200)

    def test_quiet_hours_rejects_booleans(self):
        resp = self._post('/api/config/notify', {'quiet': [True, False]})
        self.assertEqual(resp.status_code, 400)
        resp2 = self._post('/api/config/notify', {'quiet': [23, 7]})
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.get_json()['quiet'], [23, 7])

    def test_external_local_json_edit_is_reloaded_on_next_request(self):
        self.assertEqual(self._get('/api/config').get_json()['refresh_seconds'], 300)
        (self.root / 'local.json').write_text(json.dumps({'refresh_seconds': 120, 'thresholds': {'low_percent': 25}}))
        cfg = self._get('/api/config').get_json()
        self.assertEqual(cfg['refresh_seconds'], 120)
        self.assertEqual(cfg['thresholds']['low_percent'], 25)

    def test_set_pricing_uses_file_lock_sidecar(self):
        resp = self._post('/api/config/pricing', {'model': 'custom-model', 'input': 1.5, 'output': 3.0})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue((self.root / 'pricing.lock').exists())
        saved = json.loads((self.root / 'pricing.json').read_text())
        self.assertEqual(saved['custom-model']['input'], 1.5)

    def test_notify_open_blocks_private_dns_resolution(self):
        req = notify.urllib.request.Request('https://rebind.example/hook', data=b'{}')
        for ip in ('127.0.0.1', '169.254.169.254', '10.1.2.3', '100.64.1.2', '::1', 'fc00::1'):
            with self.subTest(ip=ip):
                fake = [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))]
                with patch('llm_usage.util.socket.getaddrinfo', return_value=fake):
                    with self.assertRaises(ValueError):
                        notify._open(req, timeout=5)

    def test_notify_send_rejects_malformed_telegram_token_and_bool_chat(self):
        with patch.object(notify, '_post') as post:
            for bad in ('123:abc/../../evil', '123:abc?x=1', '123:abc\r\n', 'bad token'):
                with self.subTest(token=bad):
                    res = notify.send({'telegram_token': bad, 'telegram_chat': '12345'}, 't', 'b')
                    self.assertEqual(res, {'telegram': 'InvalidURL'})
            res_bool = notify.send({'telegram_token': '12345:valid_tok', 'telegram_chat': True}, 't', 'b')
            self.assertEqual(res_bool, {'telegram': 'ValueError'})
            post.assert_not_called()


class StoreAndCollectorFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'usage.db')

    def test_usage_live_uses_explicit_now_parameter(self):
        base = stamp('2026-11-24T09:00:00Z')
        with self.store.connect() as c:
            self.store.save_state(c, 'collector', {'status': 'idle', 'checked': base})
            self.store.save_state(c, 'backup', {'ok': True, 'ts': base, 'file': 'usage-2026-11-24.db.gz', 'size': 1000})
            self.store.save_state(c, 'verify', {'ok': True, 'ts': base})
            sources_ok, _ = self.store._usage_live(c, [], datetime.fromtimestamp(base + 60, KST))
            sources_stale, _ = self.store._usage_live(c, [], datetime.fromtimestamp(base + 200000, KST))
        by_ok = {s['name']: s['status'] for s in sources_ok}
        by_stale = {s['name']: s['status'] for s in sources_stale}
        self.assertEqual((by_ok['수집기'], by_ok['일일 백업'], by_ok['원본 대조']), ('ok', 'ok', 'ok'))
        self.assertEqual(
            (by_stale['수집기'], by_stale['일일 백업'], by_stale['원본 대조']), ('stale', 'stale', 'stale')
        )

    def test_multi_account_claude_has_price_segments(self):
        t1 = stamp('2026-11-20T03:00:00Z')
        cut = stamp('2026-11-22T00:00:00Z')
        t2 = stamp('2026-11-23T03:00:00Z')
        now = stamp('2026-11-24T09:00:00Z')
        with self.store.connect() as c:
            self.store.event(
                c,
                'c1',
                t1,
                'Anthropic',
                'claude-code',
                'claude-sonnet-5-5',
                {'uncached_input': 1000},
                account='claude-code',
            )
            self.store.event(
                c,
                'c2',
                t1,
                'Anthropic',
                'claude-code',
                'claude-sonnet-5-5',
                {'uncached_input': 2000},
                account='claude-code:second',
            )
            self.store.event(
                c,
                'c3',
                t2,
                'Anthropic',
                'claude-code',
                'claude-sonnet-5-5',
                {'uncached_input': 3000},
                account='claude-code:second',
            )
            self.store.save_state(
                c,
                'claude_accounts',
                [{'route': 'claude-code', 'label': 'Main'}, {'route': 'claude-code:second', 'label': 'Sub'}],
            )
        usage = self.store.usage(
            period='7d',
            now=now,
            subscriptions={'claude-code': 100, 'claude-code:second': 200},
            price_history={'claude-code:second': [{'until': cut, 'usd': 100}]},
        )
        subs = {s['route']: s for s in usage['subscriptions']}
        segs = subs['claude-code:second']['price_segments']
        self.assertIsNotNone(segs)
        self.assertEqual(len(segs), 2)
        self.assertEqual([s['tokens'] for s in segs], [2000, 3000])

    def test_devin_open_db_handles_closed_wal_and_hash_in_path(self):
        folder = self.root / 'dir#1'
        folder.mkdir()
        db_path = folder / 'sessions.db'
        con = sqlite3.connect(db_path)
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('CREATE TABLE t (x INT)')
        con.execute('INSERT INTO t VALUES (42)')
        con.commit()
        con.close()
        self.assertFalse(Path(str(db_path) + '-wal').exists())
        ro, tmp = devin.open_db(db_path)
        try:
            self.assertIsNone(tmp)
            self.assertEqual(ro.execute('SELECT x FROM t').fetchone()[0], 42)
            self.assertFalse(Path(str(db_path) + '-shm').exists())
        finally:
            ro.close()

    def test_collect_local_avoids_like_underscore_wildcard_and_deleted_file_errors(self):
        dir_a = self.root / 'dir_a'
        dir_x = self.root / 'dirXa'
        dir_a.mkdir()
        dir_x.mkdir()
        (dir_a / 'ok.jsonl').write_text('')
        (dir_x / 'ok.jsonl').write_text('')
        old_ts = 1000.0
        with self.store.connect() as c:
            c.execute(
                'INSERT INTO import_errors VALUES (?,?,?,?,?)',
                ('file:' + str(dir_a / 'gone.jsonl'), '1', 'ValueError', 1, old_ts),
            )
            c.execute(
                'INSERT INTO import_errors VALUES (?,?,?,?,?)',
                ('file:' + str(dir_x / 'still_there.jsonl'), '1', 'ValueError', 1, old_ts),
            )
            c.execute(
                'INSERT INTO import_errors VALUES (?,?,?,?,?)',
                ('file:' + str(dir_a / 'deleted_recent.jsonl'), '1', 'ValueError', 1, 9_999_999_999.0),
            )
        sources = [{'name': 'test-a', 'route': 'claude-code', 'kind': 'jsonl', 'path': str(dir_a)}]
        with patch.object(collect, 'local_sources', return_value=sources):
            collect.collect_local(self.store, {}, include_derived=False)
        with self.store.connect() as c:
            # dirXa's old error must NOT have been deleted by dir_a's '_' character.
            self.assertEqual(
                c.execute(
                    'SELECT COUNT(*) FROM import_errors WHERE source=?', ('file:' + str(dir_x / 'still_there.jsonl'),)
                ).fetchone()[0],
                1,
            )
            # Deleted file's error must not inflate dir_a's active error count after aging.
            src = dict(c.execute("SELECT status,detail FROM sources WHERE name='test-a'").fetchone())
        self.assertEqual(src['status'], 'ok')
        self.assertIn('해석 실패 0건', src['detail'])

    def test_prune_stale_file_state_and_hook_version_check(self):
        root = self.root / 'claude-logs'
        root.mkdir()
        kept = root / 'keep.jsonl'
        kept.write_text('')
        gone = root / 'gone.jsonl'
        unmounted = self.root / 'unmounted-mnt'
        with self.store.connect() as c:
            self.store.save_state(c, 'file:' + str(kept), {'size': 0})
            self.store.save_state(c, 'file:' + str(gone), {'size': 10})
            self.store.save_state(c, 'file:' + str(unmounted / 'old.jsonl'), {'size': 10})
        sources = [
            {'name': 'claude', 'route': 'claude-code', 'kind': 'jsonl', 'path': str(root)},
            {'name': 'win', 'route': 'codex', 'kind': 'jsonl', 'path': str(unmounted)},
        ]
        with patch.object(collect, 'local_sources', return_value=sources):
            removed = collect.prune_stale_file_state(self.store, {})
        self.assertEqual(removed, 1)
        with self.store.connect() as c:
            self.assertIsNotNone(self.store.state(c, 'file:' + str(kept)))
            self.assertIsNone(self.store.state(c, 'file:' + str(gone)))
            self.assertIsNotNone(self.store.state(c, 'file:' + str(unmounted / 'old.jsonl')))
        status_dir = self.root / 'status'
        status_dir.mkdir()
        (status_dir / 'statusline.py').write_text('# old script without version\n')
        collect.check_hook_versions(self.store, {'status_dir': str(status_dir)})
        with self.store.connect() as c:
            row = dict(c.execute("SELECT status,detail FROM sources WHERE name='hook-scripts'").fetchone())
        self.assertEqual(row['status'], 'partial')
        self.assertIn('statusline.py', row['detail'])
        (status_dir / 'statusline.py').write_text('HOOK_VERSION = 1\n')
        collect.check_hook_versions(self.store, {'status_dir': str(status_dir)})
        with self.store.connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM sources WHERE name='hook-scripts'").fetchone())
        win_inbox = self.root / 'win-status'
        win_inbox.mkdir()
        (win_inbox / 'statusline.py').write_text('# outdated windows copy\n')
        collect.check_hook_versions(
            self.store, {'status_dir': str(status_dir), 'status_inboxes': [str(status_dir), str(win_inbox)]}
        )
        with self.store.connect() as c:
            row = dict(c.execute("SELECT status,detail FROM sources WHERE name='hook-scripts'").fetchone())
        self.assertEqual(row['status'], 'partial')
        self.assertIn('statusline.py', row['detail'])

    def test_collect_status_empty_event_preserves_prior_ok_source(self):
        inbox = self.root / 'inbox'
        inbox.mkdir()
        now = 1_700_000_000.0
        # Initial empty event with no prior ok still records unavailable.
        (inbox / 'antigravity.json').write_text(json.dumps({'checked': now - 300, 'data': {'quota': {}}}))
        with patch('llm_usage.store.time.time', return_value=now):
            collect.collect_status(self.store, {'status_inboxes': [str(inbox)]})
        with self.store.connect() as c:
            src = dict(c.execute("SELECT status,checked FROM sources WHERE name='antigravity'").fetchone())
        self.assertEqual(src['status'], 'unavailable')
        # Valid quota event transitions source to ok.
        (inbox / 'antigravity.json').write_text(
            json.dumps(
                {
                    'checked': now - 100,
                    'data': {'quota': {'gemini-5h': {'remaining_fraction': 0.9, 'reset_in_seconds': 3600}}},
                }
            )
        )
        with patch('llm_usage.store.time.time', return_value=now):
            collect.collect_status(self.store, {'status_inboxes': [str(inbox)]})
        # Subsequent empty quota event must NOT clobber ok or advance source.checked.
        (inbox / 'antigravity.json').write_text(json.dumps({'checked': now - 10, 'data': {'quota': {}}}))
        with patch('llm_usage.store.time.time', return_value=now):
            collect.collect_status(self.store, {'status_inboxes': [str(inbox)]})
        with self.store.connect() as c:
            src = dict(c.execute("SELECT status,checked FROM sources WHERE name='antigravity'").fetchone())
            ckpt = self.store.state(c, 'status:' + str(inbox / 'antigravity.json'))
        self.assertEqual(src['status'], 'ok')
        self.assertEqual(src['checked'], now - 100)
        self.assertEqual(ckpt, now - 10)
        api_rows = [r for r in self.store.limits(now)['limits'] if r['route'] == 'antigravity']
        self.assertEqual(api_rows[0]['status'], 'fresh')

    def test_backup_uses_kst_date_and_handles_hash_in_path(self):
        folder = self.root / 'db#backup'
        folder.mkdir()
        db_path = folder / 'usage.db'
        Store(db_path)
        res = backup.backup_now(db_path)
        self.assertTrue(res['ok'], res)
        today_kst = datetime.now(KST).strftime('%Y%m%d')
        self.assertEqual(res['file'], f'usage-{today_kst}.db.gz')
        out_gz = folder / 'backups' / res['file']
        self.assertTrue(out_gz.exists())
        raw = folder / 'restored.db'
        with gzip.open(out_gz, 'rb') as src, open(raw, 'wb') as dst:
            shutil.copyfileobj(src, dst)
        con = sqlite3.connect(raw)
        try:
            self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            con.close()


class PythonJsParityTests(unittest.TestCase):
    def test_notify_labels_and_model_quota_match_format_js(self):
        rows = [
            {'route': 'antigravity', 'bucket': 'gemini-5h'},
            {'route': 'antigravity', 'bucket': 'gemini-weekly'},
            {'route': 'antigravity', 'bucket': '3p-5h'},
            {'route': 'antigravity', 'bucket': '3p-weekly'},
            {'route': 'antigravity', 'bucket': '3p-weekly-2'},
            {'route': 'antigravity', 'bucket': 'gemini-argon-wtu-weekly'},
            {'route': 'codex', 'bucket': 'codex · 300분', 'display_name': '5시간 한도'},
            {'route': 'codex', 'bucket': 'gpt-6-spark · 300분', 'display_name': 'Spark 5시간'},
            {'route': 'claude-code', 'bucket': 'five_hour'},
            {'route': 'claude-code', 'bucket': 'seven_day_sonnet'},
            {'route': 'claude-code:alt', 'bucket': 'seven_day', 'account_label': 'Max 5x'},
            {'route': 'devin', 'bucket': 'daily'},
            {'route': 'devin', 'bucket': 'weekly'},
            {'route': 'opencode-go', 'bucket': 'monthly'},
            {'route': 'codex', 'bucket': 'custom', 'scope': {'role': 'model'}},
        ]
        format_js = Path(__file__).resolve().parent.parent / 'llm_usage/web/format.js'
        script = (
            "const fs=require('fs'),vm=require('vm');"
            f"const src=fs.readFileSync({json.dumps(str(format_js))},'utf8');"
            "const f=vm.runInNewContext(src+';({quotaLabel,modelQuota})',"
            "{Intl,Date,Math,Number,String,Set,Map,JSON,RegExp,Object,Array});"
            f"const rows={json.dumps(rows)};"
            "console.log(JSON.stringify(rows.map(r=>({label:f.quotaLabel(r),model:f.modelQuota(r)}))));"
        )
        proc = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
        js_results = json.loads(proc.stdout)
        for row, js in zip(rows, js_results):
            with self.subTest(row=row):
                self.assertEqual(notify._label(row), js['label'])
                self.assertEqual(notify.model_quota(row), js['model'])


if __name__ == '__main__':
    unittest.main()

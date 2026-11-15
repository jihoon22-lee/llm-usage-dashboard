"""Follow-ups of the 2026-11-15 review: what the collector and the page say when a step
fails, and work the idle collector no longer repeats."""

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from llm_usage import collect, notify, overview
from llm_usage.backup import BackupCheckFailed, backup_now, error_text
from llm_usage.config import local_problem, with_local
from llm_usage.devin import model_name
from llm_usage.store import Store
from llm_usage.webapp import create_app

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging'))
import deploy  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'usage.db')
        self.settings = {'database': self.store.path}

    def source(self, name):
        with self.store.connect() as c:
            row = c.execute('SELECT status,detail FROM sources WHERE name=?', (name,)).fetchone()
        return tuple(row) if row else None


class CollectorLoopTests(Base):
    def loop(self, seconds, **patches):
        """Run the collector loop for `seconds` of its own clock, 2 seconds a turn."""
        with self.store.connect() as c:
            self.store.save_state(c, 'normalized_version', 4)
        clock = [1_800_000_000.0]
        end = clock[0] + seconds
        beats = []
        save = Store.save_state

        def counted(store, c, key, data):
            if key == 'collector' and data.get('status') == 'idle':
                beats.append(clock[0])
            return save(store, c, key, data)

        def sleep(_):
            clock[0] += 2
            if clock[0] >= end:
                raise StopIteration

        with contextlib.ExitStack() as stack:
            for target in (
                'llm_usage.collect.collect_status',
                'llm_usage.collect.poll_limits',
                'llm_usage.collect.collect_local',
            ):
                stack.enter_context(patch(target))
            stack.enter_context(
                patch('llm_usage.backup.backup_now', side_effect=lambda database: dict(ts=clock[0], ok=True))
            )
            stack.enter_context(patch('llm_usage.verify.reconcile', return_value={}))
            stack.enter_context(patch('llm_usage.collect.time.time', side_effect=lambda: clock[0]))
            stack.enter_context(patch('llm_usage.collect.time.sleep', side_effect=sleep))
            stack.enter_context(patch.object(Store, 'save_state', counted))
            mocks = {name: stack.enter_context(patch(name, **options)) for name, options in patches.items()}
            with self.assertRaises(StopIteration):
                collect.run(self.settings)
        return beats, mocks

    def test_the_idle_loop_writes_its_heartbeat_twice_a_minute(self):
        beats, _ = self.loop(80)
        start = beats[0]
        # 40 turns of the loop: after the first pass, then every 30 seconds.
        self.assertEqual([t - start for t in beats], [0, 30, 60])

    def test_a_failed_weekly_report_is_named_and_the_monthly_settlement_still_runs(self):
        _, mocks = self.loop(
            2,
            **{
                'llm_usage.reports.ensure_report': dict(side_effect=RuntimeError('broken')),
                'llm_usage.monthly.ensure_months': dict(return_value=[]),
            },
        )
        self.assertEqual(mocks['llm_usage.monthly.ensure_months'].call_count, 1)
        self.assertEqual(self.source('외부 알림'), ('error', '주간 리포트 생성 실패 (RuntimeError)'))

    def test_a_failed_alert_check_still_builds_the_report_and_the_settlement(self):
        _, mocks = self.loop(
            2,
            **{
                'llm_usage.notify.evaluate': dict(side_effect=RuntimeError('broken')),
                'llm_usage.reports.ensure_report': dict(return_value=None),
                'llm_usage.monthly.ensure_months': dict(return_value=[]),
            },
        )
        self.assertEqual(
            (mocks['llm_usage.reports.ensure_report'].call_count, mocks['llm_usage.monthly.ensure_months'].call_count),
            (1, 1),
        )
        self.assertEqual(self.source('외부 알림'), ('error', '알림 판단 실패 (RuntimeError)'))


class SettingsFileTests(Base):
    def test_an_unreadable_local_file_is_said_and_a_repaired_one_clears_it(self):
        local = self.root / 'local.json'
        self.assertIsNone(local_problem(self.settings))  # no file yet
        for text, problem in (('{broken', 'JSONDecodeError'), ('5', 'NotAnObject'), ('[1]', 'NotAnObject')):
            local.write_text(text)
            self.assertEqual(local_problem(self.settings), problem)
            # Reading falls back to the base settings instead of failing the collector.
            self.assertEqual(with_local(self.settings), self.settings)
            collect.report_local_settings(self.store, self.settings)
            status, detail = self.source('웹 설정')
            self.assertEqual(status, 'error')
            self.assertIn(problem, detail)
            self.assertNotIn(str(self.root), detail)
        local.write_text(json.dumps({'refresh_seconds': 60}))
        self.assertIsNone(local_problem(self.settings))
        self.assertEqual(with_local(self.settings)['refresh_seconds'], 60)
        collect.report_local_settings(self.store, self.settings)
        self.assertIsNone(self.source('웹 설정'))


class StatusTextTests(Base):
    def test_a_failed_backup_names_the_reason_without_a_path(self):
        self.assertEqual(
            error_text(OSError(28, 'No space left on device', '/data/private/usage.db')),
            'OSError: No space left on device',
        )
        self.assertEqual(error_text(BackupCheckFailed('quick_check failed')), 'BackupCheckFailed')
        self.assertEqual(error_text(ValueError('/data/private')), 'ValueError')
        with patch(
            'llm_usage.backup.gzip.open',
            side_effect=OSError(28, 'No space left on device', str(self.root / 'backups' / 'x.gz')),
        ):
            result = backup_now(self.store.path)
        self.assertEqual((result['ok'], result['error']), (False, 'OSError: No space left on device'))


class NotifySendTests(Base):
    def test_a_redirect_is_a_failed_send_not_a_followed_one(self):
        hits = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                hits.append('POST ' + self.path)
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                self.send_response(302)
                self.send_header('Location', '/moved')
                self.send_header('Content-Length', '0')
                self.end_headers()

            def do_GET(self):
                hits.append('GET ' + self.path)
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with self.assertRaises(HTTPError) as raised:
                notify._post('http://127.0.0.1:%d/hook' % server.server_address[1], {'text': 'x'}, 3)
            self.assertEqual(raised.exception.code, 302)
            raised.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
        self.assertEqual(hits, ['POST /hook'])

    def test_a_failed_step_is_named(self):
        notify.record_failure(self.store, KeyError('x'))
        self.assertEqual(self.source('외부 알림'), ('error', '알림 판단 실패 (KeyError)'))
        notify.record_failure(self.store, KeyError('x'), '월 정산 생성')
        self.assertEqual(self.source('외부 알림'), ('error', '월 정산 생성 실패 (KeyError)'))


class OverviewFactTests(unittest.TestCase):
    ROW = dict(route='claude-code', uncached_input=900, cached_input=0, output=100, cache_creation=0)

    def idle(self, usage, prices):
        return [
            f['route'] for f in overview._subscription_facts(prices, usage, None) if f['kind'] == 'idle_subscription'
        ]

    def test_an_idle_original_claude_account_does_not_take_the_other_accounts_use(self):
        other = 'claude-code:0123456789ab'
        prices = {'claude-code': 100, other: 100}
        # Several accounts and every request is the second account's.
        self.assertEqual(
            self.idle(dict(rows=[self.ROW], claude_split=[dict(account=other, tokens=1000)]), prices), ['claude-code']
        )
        self.assertEqual(
            self.idle(dict(rows=[self.ROW], claude_split=[dict(account='claude-code', tokens=1000)]), prices), [other]
        )
        # One account: the service's use is its own.
        self.assertEqual(self.idle(dict(rows=[self.ROW], claude_split=None), {'claude-code': 100}), [])


class WebRefusalTests(Base):
    def test_a_refused_request_leaves_where_it_was_raised_in_the_log(self):
        origin = 'https://pc.example.ts.net:9444'
        app = create_app(
            dict(database=self.store.path, origin=origin, secret_key='test-secret', allowed_logins=['owner'])
        )
        with self.assertLogs(app.logger, 'WARNING') as logs:
            r = app.test_client().get(
                '/api/usage?granularity=fortnight&scope=PRIVATE-INPUT',
                base_url=origin,
                headers={'Tailscale-User-Login': 'owner'},
                environ_overrides={'REMOTE_ADDR': '', 'gunicorn.socket': SimpleNamespace(family=socket.AF_UNIX)},
            )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(logs.output), 1)
        self.assertRegex(logs.output[0], r'usage request refused: ValueError at store\.py:\d+$')
        self.assertNotIn('PRIVATE-INPUT', logs.output[0])


class DeployRollbackTests(unittest.TestCase):
    def outcome(self, restarts, rollback_to=Path('/r/releases/0123456789abcdef')):
        with (
            patch.object(deploy, 'switch') as switch,
            patch.object(deploy, 'started', return_value=1),
            patch.object(deploy, 'restart', side_effect=restarts),
            self.assertRaises(SystemExit) as stopped,
        ):
            deploy.deploy_release(Path('/r/releases/new'), rollback_to, 'abcdef0', Path('/data/usage.db'))
        return str(stopped.exception.code), switch.call_count

    def test_the_rollback_says_whether_the_previous_release_came_back(self):
        message, switches = self.outcome(['unhealthy', None])
        self.assertIn('이전 릴리스 0123456789ab로 되돌렸습니다', message)
        self.assertEqual(switches, 2)
        message, _ = self.outcome(['unhealthy', 'no heartbeat'])
        self.assertIn('재시작도 확인되지 않았습니다(no heartbeat)', message)
        self.assertNotIn('로 되돌렸습니다.', message)
        message, switches = self.outcome(['unhealthy'], rollback_to=None)
        self.assertIn('되돌릴 이전 릴리스가 없습니다', message)
        self.assertEqual(switches, 1)


class DevinNameTests(unittest.TestCase):
    def test_every_effort_word_is_split_from_the_model_alike(self):
        self.assertEqual(
            [
                model_name(m)
                for m in ('claude-opus-5-xhigh', 'claude-opus-5-high', 'swe-2-minimal', 'swe-1-6-fast', 'swe-2', None)
            ],
            ['claude-opus-5 (xhigh)', 'claude-opus-5 (high)', 'swe-2 (minimal)', 'swe-1-6 (fast)', 'swe-2', None],
        )


if __name__ == '__main__':
    unittest.main()

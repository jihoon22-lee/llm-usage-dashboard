"""Findings of the second review pass: statuses that outlived their cause, hand-written
prices that broke a response, one unreadable original that failed a whole comparison,
and alerts lost when the collector stops while sending."""

from contextlib import closing
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from llm_usage import claude_attribution, notify, pricing, verify
from llm_usage.store import Store
from llm_usage.telemetry import handler

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging'))
import install  # noqa: E402

NOW = 1_800_000_000.0
NTFY = {'ntfy_url': 'https://ntfy.example/topic'}


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, n=-1):
        return b''


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'usage.db')

    def source(self, name):
        with self.store.connect() as c:
            row = c.execute('SELECT status,detail FROM sources WHERE name=?', (name,)).fetchone()
        return tuple(row) if row else None


class AccountStatusTests(Base):
    def refresh(self, errors, accounts):
        claude_attribution._summarized.clear()
        with (
            patch.object(claude_attribution, 'collect_evidence', return_value=errors),
            patch.object(claude_attribution, '_resolve', return_value=(0, accounts)),
        ):
            claude_attribution.refresh(self.store, {})

    def test_a_failure_with_one_account_does_not_outlive_its_cause(self):
        self.refresh(errors=1, accounts=1)
        self.assertEqual(self.source('claude-accounts')[0], 'error')
        self.refresh(errors=0, accounts=1)
        self.assertIsNone(self.source('claude-accounts'))

    def test_an_unreadable_hook_file_is_not_also_recorded_as_a_success(self):
        # With hook records and nothing unattributed the summary alone would read 'ok'.
        with self.store.connect() as c:
            claude_attribution.add_evidence(c, [('session', NOW, 'a' * 64, 'hook:SessionStart')])
        self.refresh(errors=1, accounts=2)
        self.assertEqual(self.source('claude-accounts')[0], 'error')
        with self.store.connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM source_health WHERE name='claude-accounts'").fetchone())
        self.refresh(errors=0, accounts=2)
        self.assertIn(self.source('claude-accounts')[0], ('ok', 'partial'))


class PricingFileTests(Base):
    def settings(self, table):
        return dict(pricing=table, pricing_file=str(self.root / 'none.json'))

    def test_a_dated_entry_with_an_unusable_start_is_dropped_instead_of_failing(self):
        good = {'since': '2026-09-01', 'input': 4, 'output': 20}
        for bad in (20260901, None):
            table = pricing.load_pricing(
                self.settings(
                    {
                        'model-x': [good, {'since': bad, 'input': 9, 'output': 90}],
                        'model-y': [{'since': bad, 'input': 1, 'output': 2}],
                    }
                )
            )
            self.assertEqual(table['model-x'], [good])
            self.assertNotIn('model-y', table)
            self.assertEqual(pricing.rate_for('model-x', table, '2026-10-01')['input'], 4)

    def test_an_ignored_override_is_not_called_the_users_price(self):
        model = next(iter(pricing.BUILTIN))
        origins = pricing.pricing_origins(
            self.settings({model: {'input': '5', 'output': 25}, 'mine': {'input': 1, 'output': 2}})
        )
        self.assertEqual((origins[model], origins['mine']), ('builtin', 'user'))


class VerifyRowTests(Base):
    def test_one_unreadable_devin_row_is_counted_and_the_rest_compared(self):
        db = self.root / 'sessions.db'
        rows = [
            (
                1,
                {
                    'role': 'assistant',
                    'metadata': {
                        'request_id': 'ok',
                        'generation_model': 'swe-2',
                        'metrics': {'input_tokens': 5, 'output_tokens': 2},
                    },
                },
            ),
            (
                2,
                {
                    'role': 'assistant',
                    'metadata': {'request_id': 'text', 'metrics': {'input_tokens': 'many', 'output_tokens': 2}},
                },
            ),
            (3, {'role': 'assistant', 'metadata': ['request_id']}),
        ]
        with closing(sqlite3.connect(db)) as c, c:
            c.execute('CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY, chat_message TEXT)')
            c.executemany('INSERT INTO message_nodes VALUES (?,?)', [(n, json.dumps(m)) for n, m in rows])
        unreadable = []
        expected = verify._devin_expected(
            {'sources': [{'kind': 'devin', 'path': str(db)}]}, {'devin:' + str(db): 10}, unreadable
        )
        self.assertEqual(expected, {'ok': ('swe-2', 5, 0, 2, 0)})
        self.assertEqual(unreadable, ['text'])


class InstallerWaitTests(unittest.TestCase):
    def wait(self, refused_seconds):
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        def open_(url, timeout):
            if clock[0] < refused_seconds:
                raise ConnectionRefusedError
            return Response()

        with (
            patch.object(install.time, 'monotonic', side_effect=lambda: clock[0]),
            patch.object(install.time, 'sleep', side_effect=sleep),
            patch.object(install.urllib.request, 'build_opener') as opener,
        ):
            opener.return_value.open.side_effect = open_
            return install.healthy(), clock[0]

    def test_a_slow_first_start_is_waited_for_by_the_clock(self):
        # 60 refused attempts used to end the wait after 15 seconds.
        self.assertEqual(self.wait(refused_seconds=40), (True, 40.0))
        ok, waited = self.wait(refused_seconds=1000)
        self.assertFalse(ok)
        self.assertEqual(waited, 60.0)


class TelemetryIdleTests(Base):
    def test_a_connection_that_sends_nothing_is_closed(self):
        made = handler(self.store, 'fixture-token')
        self.assertEqual(made.timeout, 10)
        made.timeout = 0.3  # the same mechanism, without waiting ten seconds here
        server = ThreadingHTTPServer(('127.0.0.1', 0), made)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with socket.create_connection(server.server_address, timeout=5) as idle:
                started = time.monotonic()
                self.assertEqual(idle.recv(1), b'')  # closed by the server, not by our 5 seconds
                self.assertLess(time.monotonic() - started, 3)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


class Stopped(BaseException):
    """The collector being stopped in the middle of a send."""


class OutboxTests(Base):
    def outbox(self):
        with self.store.connect() as c:
            return [m['title'] for m in self.store.state(c, notify.OUTBOX_KEY, []) or []]

    def test_a_stop_while_sending_leaves_the_alert_for_the_next_start(self):
        with patch('llm_usage.notify._open', side_effect=Stopped), self.assertRaises(Stopped):
            notify.deliver(self.store, NTFY, [('low', '잔여 적음', '본문')], NOW)
        self.assertEqual(self.outbox(), ['잔여 적음'])
        sent = []
        with patch(
            'llm_usage.notify._open',
            side_effect=lambda r, timeout: (sent.append(json.loads(r.data)['title']), Response())[1],
        ):
            notify.deliver(self.store, NTFY, [], NOW + 60)
        self.assertEqual((sent, self.outbox()), (['잔여 적음'], []))

    def test_a_sent_alert_leaves_the_outbox_and_a_failed_one_waits_once(self):
        with patch('llm_usage.notify._open', side_effect=lambda r, timeout: Response()):
            notify.deliver(self.store, NTFY, [('low', '보냄', '본문')], NOW)
        self.assertEqual(self.outbox(), [])
        with patch('llm_usage.notify._open', side_effect=OSError('down')):
            notify.deliver(self.store, NTFY, [('low', '실패', '본문')], NOW + 60)
        self.assertEqual(self.outbox(), ['실패'])


if __name__ == '__main__':
    unittest.main()

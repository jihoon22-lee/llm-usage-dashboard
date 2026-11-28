"""Tests for PR 1: dead field removal, zstd/gzip compression, ETag, builtin pricing JSON, and parallel polling."""

import gzip
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from compression import zstd as _zstd
except ImportError:
    from backports import zstd as _zstd

from llm_usage.compress import choose_encoding
from llm_usage.limits import poll_limits
from llm_usage.pricing import BUILTIN
from llm_usage.store import Store, stamp
from llm_usage.webapp import create_app


class CompressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'usage.db')
        self.app = create_app(
            dict(
                database=self.store.path,
                origin='https://dashboard.example',
                secret_key='synthetic-only',
                allowed_logins=['owner'],
            )
        )
        self.client = self.app.test_client()
        self.env = {'gunicorn.socket': SimpleNamespace(family=socket.AF_UNIX)}
        self.headers = {'Tailscale-User-Login': 'owner'}

    def test_choose_encoding_prefers_zstd_over_gzip(self):
        self.assertEqual(choose_encoding('gzip, deflate, br, zstd'), 'zstd')
        self.assertEqual(choose_encoding('gzip, deflate'), 'gzip')
        self.assertEqual(choose_encoding('zstd;q=0.5, gzip;q=0.9'), 'gzip')
        self.assertEqual(choose_encoding('zstd;q=0, gzip;q=1'), 'gzip')
        self.assertEqual(choose_encoding('br, deflate'), None)
        self.assertEqual(choose_encoding(''), None)

    def test_zstd_and_gzip_compression_and_etag_on_assets(self):
        res_zstd = self.client.get(
            '/assets/format.js',
            headers={**self.headers, 'Accept-Encoding': 'gzip, zstd'},
            environ_overrides=self.env,
        )
        self.assertEqual(res_zstd.status_code, 200)
        self.assertEqual(res_zstd.headers.get('Content-Encoding'), 'zstd')
        self.assertIn('Accept-Encoding', res_zstd.headers.get('Vary', ''))
        decompressed = _zstd.decompress(res_zstd.data)
        self.assertIn(b'const compact=', decompressed)

        res_gzip = self.client.get(
            '/assets/format.js',
            headers={**self.headers, 'Accept-Encoding': 'gzip'},
            environ_overrides=self.env,
        )
        self.assertEqual(res_gzip.status_code, 200)
        self.assertEqual(res_gzip.headers.get('Content-Encoding'), 'gzip')
        self.assertEqual(gzip.decompress(res_gzip.data), decompressed)

        # Static manifest supports weak ETag and 304 revalidation.
        res_manifest = self.client.get('/manifest.json', headers=self.headers, environ_overrides=self.env)
        self.assertEqual(res_manifest.status_code, 200)
        etag = res_manifest.headers.get('ETag')
        self.assertTrue(etag and etag.startswith('W/"'))
        res_304 = self.client.get(
            '/manifest.json',
            headers={**self.headers, 'If-None-Match': etag},
            environ_overrides=self.env,
        )
        self.assertEqual(res_304.status_code, 304)
        self.assertEqual(res_304.data, b'')

    def test_api_json_keeps_no_store_and_compresses_with_zstd(self):
        res = self.client.get(
            '/api/config',
            headers={**self.headers, 'Accept-Encoding': 'zstd, gzip'},
            environ_overrides=self.env,
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get('Cache-Control'), 'no-store')
        self.assertEqual(res.headers.get('Content-Encoding'), 'zstd')
        payload = json.loads(_zstd.decompress(res.data))
        self.assertIn('pricing', payload)
        self.assertNotIn('ETag', res.headers)

    def test_index_injects_refresh_seconds_meta(self):
        res = self.client.get('/', headers=self.headers, environ_overrides=self.env)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'<meta name="llm-refresh" content="300">', res.data)


class BuiltinPricingJsonTests(unittest.TestCase):
    def test_builtin_pricing_loads_from_json(self):
        path = Path(__file__).resolve().parents[1] / 'llm_usage/data/builtin_pricing.json'
        self.assertTrue(path.is_file())
        raw = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(BUILTIN, raw)
        self.assertIn('gemini-4-neon', BUILTIN)
        self.assertIn('claude-opus-5-5', BUILTIN)
        self.assertIn('gpt-6.2-sol', BUILTIN)


class ParallelPollingAndCleanLimitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'usage.db')

    def test_limits_omit_removed_forecast_and_capacity_fields(self):
        now = stamp('2026-11-28T03:00:00Z')
        with self.store.connect() as c:
            self.store.limit(c, 'codex', 'codex · 300분', 70.0, now + 3600, now - 60, 'codex')
            self.store.source(c, 'codex', 'ok', '', now - 60)
        row = self.store.limits(now)['limits'][0]
        for removed in ('forecast', 'plan', 'capacity', 'capacity_history', 'paces', 'pace_change', 'pace_per_hour'):
            self.assertNotIn(removed, row)
        self.assertEqual(row['scope']['role'], 'common')

    def test_poll_limits_runs_providers_concurrently_and_isolates_failures(self):
        started = []
        barrier = threading.Barrier(3, timeout=3.0)
        cloud_called = []

        def slow_provider(name):
            def _fn(*_args, **_kwargs):
                started.append(name)
                barrier.wait()

            return _fn

        with (
            patch('llm_usage.limits.claude_contexts', return_value=[None]),
            patch('llm_usage.limits.poll_claude', side_effect=slow_provider('claude')),
            patch('llm_usage.limits.poll_claude_resources'),
            patch('llm_usage.limits.collect_claude_cloud', side_effect=lambda *a, **k: cloud_called.append(1)),
            patch('llm_usage.limits.poll_devin', side_effect=slow_provider('devin')),
            patch('llm_usage.limits.poll_antigravity_app', side_effect=slow_provider('antigravity')),
            patch('llm_usage.limits._poll_codex_or_opencode', side_effect=RuntimeError('boom')),
        ):
            t0 = time.monotonic()
            poll_limits(self.store, {}, manual=True, include_slow=False)
            elapsed = time.monotonic() - t0
        self.assertEqual(set(started), {'claude', 'devin', 'antigravity'})
        self.assertLess(elapsed, 2.5)
        self.assertEqual(cloud_called, [])

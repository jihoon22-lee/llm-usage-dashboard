"""Tests for PR 3 backend capabilities:
1. Session deep analysis (tree, efforts/speed, cache_hours)
2. Project aliases & non-destructive merging across usage, overview, project detail, budgets
3. Cost & subscription what-if simulator (/api/simulate)
4. Diagnostics operational controls (/api/diagnostics/system, /api/diagnostics/backup, selective /api/refresh)
5. Config & pricing JSON export/import (/api/config/export, /api/config/import) without secret leaks
"""

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from llm_usage.aliases import raw_names_for, resolve_project, validate_aliases
from llm_usage.store import Store
from llm_usage.webapp import create_app


class FakeUnixSocket:
    family = 1  # socket.AF_UNIX


def _make_app(root: Path, extra_config=None):
    db = root / 'usage.db'
    cfg = {
        'database': str(db),
        'config_file': str(root / 'config.json'),
        'pricing_file': str(root / 'pricing.json'),
        'origin': 'https://test.example',
        'allowed_logins': ['user@example.com'],
        'secret_key': 'super-secret-test-key-do-not-export',
        'subscription_prices': {'claude-code': 200, 'codex': 20},
        'notify': {
            'webhook_url': 'https://hooks.example/secret-token-123',
            'telegram_token': '123456:SECRET_BOT_TOKEN',
            'telegram_chat': '99999',
            'events': {'low': True, 'source': False},
            'quiet': [23, 7],
        },
        **(extra_config or {}),
    }
    app = create_app(cfg)
    return app, Store(db)


def _client_helpers(app):
    client = app.test_client()
    base_env = {'gunicorn.socket': FakeUnixSocket(), 'HTTP_HOST': 'test.example'}
    headers = {'Tailscale-User-Login': 'user@example.com'}
    boot = client.get('/api/bootstrap', headers=headers, environ_base=base_env)
    assert boot.status_code == 200
    csrf = boot.get_json()['csrf']
    post_headers = {**headers, 'Origin': 'https://test.example', 'X-CSRF-Token': csrf}

    def get(path):
        return client.get(path, headers=headers, environ_base=base_env)

    def post(path, payload=None):
        return client.post(
            path, json=payload if payload is not None else {}, headers=post_headers, environ_base=base_env
        )

    return get, post


class TestSessionDeepAnalysis(unittest.TestCase):
    def test_session_detail_tree_efforts_and_cache_hours(self):
        with tempfile.TemporaryDirectory() as td:
            app, store = _make_app(Path(td))
            now = int(time.time() // 3600) * 3600
            with store.connect() as c:
                # Main request in hour 0
                store.event(
                    c,
                    'ev-main-1',
                    now + 60,
                    'anthropic',
                    'claude-code',
                    'claude-sonnet-4-6',
                    dict(
                        uncached_input=2000,
                        cached_input=8000,
                        output=500,
                        cache_creation=0,
                        reasoning=100,
                        advisor_calls=1,
                    ),
                    session='sess-deep',
                    project='alpha',
                    agent_kind='main',
                    effort='high',
                    speed='fast',
                )
                # Advisor child attached to ev-main-1
                store.event(
                    c,
                    'ev-adv-1',
                    now + 65,
                    'anthropic',
                    'claude-code',
                    'claude-opus-4-6',
                    dict(uncached_input=1000, cached_input=4000, output=300, cache_creation=0, reasoning=50),
                    session='sess-deep',
                    project='alpha',
                    agent_kind='advisor',
                    parent_id='ev-main-1',
                    requests=0,
                )
                # Subagent request in hour 1
                store.event(
                    c,
                    'ev-sub-1',
                    now + 3700,
                    'anthropic',
                    'claude-code',
                    'claude-haiku-4-5',
                    dict(uncached_input=500, cached_input=1500, output=200, cache_creation=0),
                    session='sess-deep',
                    project='alpha',
                    agent_kind='sub',
                    effort='low',
                    speed='standard',
                )
            get, _ = _client_helpers(app)
            resp = get('/api/session?id=sess-deep')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            # Tree should have main root with advisor child, plus sub root
            self.assertEqual(len(data['tree']), 2)
            main_node = next(n for n in data['tree'] if n['kind'] == 'main')
            self.assertEqual(main_node['model'], 'claude-sonnet-4-6')
            self.assertEqual(len(main_node['children']), 1)
            self.assertEqual(main_node['children'][0]['kind'], 'advisor')
            self.assertEqual(main_node['children'][0]['model'], 'claude-opus-4-6')
            self.assertAlmostEqual(main_node['cache_share'], 80.0)
            # Efforts should carry both effort and speed
            self.assertTrue(any(e['effort'] == 'high' and e['speed'] == 'fast' for e in data['efforts']))
            self.assertTrue(any(e['effort'] == 'low' and e['speed'] == 'standard' for e in data['efforts']))
            # Cache hours should compute cache_share and avg_context
            self.assertEqual(len(data['cache_hours']), 2)
            h0 = data['cache_hours'][0]
            self.assertAlmostEqual(h0['cache_share'], 80.0)
            self.assertEqual(h0['avg_context'], 15000)

            # Empty session keeps schema keys
            empty = get('/api/session?id=nonexistent-session').get_json()
            self.assertEqual(empty['tree'], [])
            self.assertEqual(empty['efforts'], [])
            self.assertEqual(empty['cache_hours'], [])


class TestProjectAliases(unittest.TestCase):
    def test_alias_validation_rejects_bad_inputs(self):
        self.assertEqual(validate_aliases({'proj-a': 'Main', 'proj-b': 'Main'}), {'proj-a': 'Main', 'proj-b': 'Main'})
        for bad in (
            {'a': 'a'},  # self-map
            {'a': 'b', 'b': 'a'},  # cycle
            {'a': 'b', 'b': 'c'},  # chain
            {'미분류': 'Main'},  # unclassified cannot be mapped
            {'bad\nname': 'Main'},  # control char
            {'': 'Main'},
        ):
            with self.assertRaises(ValueError):
                validate_aliases(bad)
        self.assertEqual(resolve_project('proj-a', {'proj-a': 'Main'}), 'Main')
        self.assertEqual(raw_names_for('Main', {'proj-a': 'Main', 'proj-b': 'Main'}), ['Main', 'proj-a', 'proj-b'])

    def test_aliases_merge_across_usage_overview_project_and_month(self):
        with tempfile.TemporaryDirectory() as td:
            app, store = _make_app(Path(td))
            now = time.time() - 120
            with store.connect() as c:
                store.event(
                    c,
                    'p1',
                    now,
                    'anthropic',
                    'claude-code',
                    'claude-sonnet-4-6',
                    dict(uncached_input=1000, cached_input=0, output=200),
                    session='s1',
                    project='repo-old',
                )
                store.event(
                    c,
                    'p2',
                    now + 10,
                    'anthropic',
                    'claude-code',
                    'claude-sonnet-4-6',
                    dict(uncached_input=3000, cached_input=0, output=300),
                    session='s2',
                    project='repo-new',
                )
            get, post = _client_helpers(app)
            # Reject invalid alias via API
            bad = post('/api/config/project-aliases', {'aliases': {'a': 'a'}})
            self.assertEqual(bad.status_code, 400)
            # Set alias repo-old -> repo-new
            ok = post('/api/config/project-aliases', {'aliases': {'repo-old': 'repo-new'}})
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok.get_json()['project_aliases'], {'repo-old': 'repo-new'})

            # /api/config lists raw_projects and project_aliases
            cfg = get('/api/config').get_json()
            self.assertEqual(cfg['project_aliases'], {'repo-old': 'repo-new'})
            self.assertIn('repo-old', cfg['raw_projects'])
            self.assertIn('repo-new', cfg['raw_projects'])

            # /api/usage merges projects & sessions & group=project
            usage = get('/api/usage?period=7d&group=project').get_json()
            self.assertEqual([p['project'] for p in usage['projects']], ['repo-new'])
            self.assertEqual(usage['projects'][0]['sessions'], 2)
            self.assertEqual(usage['projects'][0]['uncached_input'], 4000)
            self.assertEqual(usage['labels'], ['repo-new'])
            self.assertEqual(usage['project_month']['projects']['repo-new']['tokens'], 4500)

            # /api/usage?scope=project:repo-new includes both raw projects
            scoped = get('/api/usage?period=7d&scope=project:repo-new').get_json()
            self.assertEqual(scoped['totals']['uncached_input'], 4000)

            # /api/overview merges projects
            ov = get('/api/overview').get_json()
            self.assertEqual([p['name'] for p in ov['projects']], ['repo-new'])
            self.assertEqual(ov['projects'][0]['tokens'], 4500)

            # /api/project returns merged total and lists aliases
            pd = get('/api/project?name=repo-new').get_json()
            self.assertEqual(pd['aliases'], ['repo-old'])
            self.assertEqual(pd['month']['tokens'], 4500)
            self.assertEqual(len(pd['sessions']), 2)


class TestSimulator(unittest.TestCase):
    def test_partial_substitution_and_subscription_comparison(self):
        with tempfile.TemporaryDirectory() as td:
            app, store = _make_app(Path(td))
            now = time.time() - 3600
            with store.connect() as c:
                store.event(
                    c,
                    'sim-1',
                    now,
                    'anthropic',
                    'claude-code',
                    'claude-opus-4-6',
                    dict(uncached_input=1_000_000, cached_input=0, output=200_000),
                    project='work',
                )
            _, post = _client_helpers(app)
            resp = post(
                '/api/simulate',
                {
                    'period': '30d',
                    'substitutions': [{'from_model': 'claude-opus-4-6', 'to_model': 'claude-sonnet-4-6', 'ratio': 0.5}],
                    'subscriptions': {'claude-code': 20},
                },
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertGreater(data['baseline_api_period'], data['simulated_api_period'])
            self.assertGreater(data['savings_period'], 0)
            self.assertEqual(len(data['breakdown']), 1)
            self.assertEqual(data['breakdown'][0]['shifted_tokens'], 600_000)
            self.assertEqual(data['subscription_monthly'], 20.0)
            self.assertIn(data['baseline_vs_subscription']['verdict'], ('subscription_cheaper', 'api_cheaper', 'equal'))

            # Ratio sum > 1.0 rejected
            bad = post(
                '/api/simulate',
                {
                    'period': '30d',
                    'substitutions': [
                        {'from_model': 'claude-opus-4-6', 'to_model': 'claude-sonnet-4-6', 'ratio': 0.7},
                        {'from_model': 'claude-opus-4-6', 'to_model': 'claude-haiku-4-5', 'ratio': 0.5},
                    ],
                },
            )
            self.assertEqual(bad.status_code, 400)


class TestDiagnosticsAndSelectiveRefresh(unittest.TestCase):
    def test_diagnostics_system_backup_and_selective_refresh(self):
        with tempfile.TemporaryDirectory() as td:
            app, store = _make_app(Path(td))
            get, post = _client_helpers(app)

            sys_resp = get('/api/diagnostics/system')
            self.assertEqual(sys_resp.status_code, 200)
            sys_data = sys_resp.get_json()
            self.assertGreater(sys_data['database']['bytes'], 0)
            self.assertGreaterEqual(sys_data['database']['schema_version'], 4)
            self.assertEqual(sys_data['hooks']['repair_command'], 'llm-usage install-hooks')
            self.assertNotIn(td, str(sys_data))

            # Trigger manual backup
            b1 = post('/api/diagnostics/backup')
            self.assertEqual(b1.status_code, 200)
            self.assertTrue(b1.get_json()['ok'])
            # Immediate second trigger hits 429 cooldown
            b2 = post('/api/diagnostics/backup')
            self.assertEqual(b2.status_code, 429)

            # Selective refresh stores targets and rejects unknown targets
            bad_ref = post('/api/refresh', {'targets': ['unknown-provider']})
            self.assertEqual(bad_ref.status_code, 400)
            ok_ref = post('/api/refresh', {'targets': ['codex']})
            self.assertEqual(ok_ref.status_code, 202)
            self.assertEqual(ok_ref.get_json()['targets'], ['codex'])
            with store.connect() as c:
                self.assertEqual(store.state(c, 'refresh_targets'), ['codex'])

            # poll_limits respects targets filter
            from llm_usage import limits

            called = []
            with (
                patch.object(limits, 'poll_devin', side_effect=lambda *a, **kw: called.append('devin')),
                patch.object(limits, 'poll_antigravity_app', side_effect=lambda *a, **kw: called.append('antigravity')),
                patch.object(limits, '_poll_codex_or_opencode', side_effect=lambda _s, _c, r, **kw: called.append(r)),
                patch.object(limits, 'claude_contexts', return_value=[]),
            ):
                limits.poll_limits(store, {}, manual=True, include_slow=False, targets=['devin'])
            self.assertEqual(called, ['devin'])


class TestConfigExportImport(unittest.TestCase):
    def test_export_excludes_secrets_and_import_preserves_notify_channels(self):
        with tempfile.TemporaryDirectory() as td:
            app, _store = _make_app(Path(td))
            get, post = _client_helpers(app)

            # Set an alias and a custom model price first
            self.assertEqual(post('/api/config/project-aliases', {'aliases': {'old': 'new'}}).status_code, 200)
            self.assertEqual(
                post('/api/config/pricing', {'model': 'custom-model-1', 'input': 2.0, 'output': 8.0}).status_code, 200
            )

            exp = get('/api/config/export')
            self.assertEqual(exp.status_code, 200)
            bundle = exp.get_json()
            raw_text = exp.get_data(as_text=True)
            # Secrets MUST NEVER appear in exported JSON
            self.assertNotIn('super-secret-test-key', raw_text)
            self.assertNotIn('secret-token-123', raw_text)
            self.assertNotIn('SECRET_BOT_TOKEN', raw_text)
            self.assertNotIn('webhook_url', raw_text)
            self.assertEqual(bundle['format'], 'llm-usage-config-v1')
            self.assertEqual(bundle['settings']['project_aliases'], {'old': 'new'})
            self.assertIn('custom-model-1', bundle['pricing_overrides'])

            # Reject bad format or forbidden keys
            self.assertEqual(post('/api/config/import', {'format': 'wrong'}).status_code, 400)
            self.assertEqual(
                post(
                    '/api/config/import', {'format': 'llm-usage-config-v1', 'settings': {'secret_key': 'hacked'}}
                ).status_code,
                400,
            )

            # Import modified bundle into same app and verify notify credentials remain intact
            bundle['settings']['refresh_seconds'] = 180
            bundle['settings']['project_aliases'] = {'legacy': 'modern'}
            bundle['settings']['notify_preferences'] = {'events': {'low': False, 'source': True}, 'quiet': [22, 6]}
            bundle['pricing_overrides']['custom-model-2'] = {'input': 1.5, 'output': 6.0}
            imp = post('/api/config/import', bundle)
            self.assertEqual(imp.status_code, 200)
            imp_data = imp.get_json()
            self.assertTrue(imp_data['imported'])
            self.assertEqual(imp_data['applied_pricing'], 2)

            cfg = get('/api/config').get_json()
            self.assertEqual(cfg['refresh_seconds'], 180)
            self.assertEqual(cfg['project_aliases'], {'legacy': 'modern'})
            self.assertIn('custom-model-2', cfg['pricing'])
            # Existing secret webhook/telegram channels stayed configured!
            self.assertTrue(cfg['notify']['channels']['webhook'])
            self.assertTrue(cfg['notify']['channels']['telegram'])
            self.assertEqual(cfg['notify']['quiet'], [22, 6])
            self.assertEqual(cfg['notify']['events']['source'], True)


if __name__ == '__main__':
    unittest.main()

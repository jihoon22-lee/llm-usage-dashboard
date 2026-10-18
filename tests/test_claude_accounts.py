"""Several Claude accounts (config homes) under one Claude Code service.

All homes, logins, transcripts and process registries are synthetic files in a
disposable HOME; no provider is contacted and no real account file is read.
"""
from contextlib import ExitStack, closing
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from llm_usage import cli, claudehook, collect, limits, statusline
from llm_usage.claude_accounts import (FAMILY, UNKNOWN, account_key, accounts, describe, home_candidates, inspect,
                                       labels, plan_label, registry, route_for)
from llm_usage.claude_attribution import account_for, collect_evidence, policies, refresh, resolve
from llm_usage.resources import key, observe_account
from llm_usage.store import Store

NOW = time.time()
MAIN = ('main-user', 'main-org')
SECOND = ('second-user', 'second-org')
K_MAIN, K_SECOND = account_key(MAIN), account_key(SECOND)
R_SECOND = 'claude-code:' + K_SECOND[:12]


class Homes:
    """A disposable HOME with the default Claude home and one zerosub-managed home
    whose projects folder links to the default one, as zerosub creates it."""

    def __init__(self, root):
        self.home = root / 'home'
        self.default = self.home / '.claude'
        self.default.mkdir(parents=True)
        (self.default / 'projects').mkdir()
        (self.default / 'sessions').mkdir()
        self.write_account(self.home / '.claude.json', MAIN)
        self.write_credentials(self.default / '.credentials.json', 'pro', 'default_claude_ai')
        self.managed = self.home / '.paseo/zerosub/homes/claude-0123abcd'
        self.managed.mkdir(parents=True)
        (self.managed / 'sessions').mkdir()
        self.write_account(self.managed / '.claude.json', SECOND)
        self.write_credentials(self.managed / '.credentials.json', 'max', 'default_claude_max_5x')
        self.joined = NOW - 3 * 3600
        os.symlink(self.default / 'projects', self.managed / 'projects')
        os.utime(self.managed / 'projects', (self.joined, self.joined), follow_symlinks=False)
        self.inbox = self.home / '.local/share/llm-usage/status'
        self.inbox.mkdir(parents=True)
        self.project = self.default / 'projects' / '-home-user-repo'
        self.project.mkdir()

    @staticmethod
    def write_account(path, identity):
        path.write_text(json.dumps({'oauthAccount': {'accountUuid': identity[0], 'organizationUuid': identity[1],
                                                     'emailAddress': 'PRIVATE-EMAIL', 'displayName': 'PRIVATE-NAME'}}))

    @staticmethod
    def write_credentials(path, plan, tier, expires=None, token='TEST-ONLY-TOKEN'):
        path.write_text(json.dumps({'claudeAiOauth': {'accessToken': token, 'refreshToken': 'TEST-ONLY-REFRESH',
                                                      'expiresAt': ((expires or NOW + 3600) * 1000),
                                                      'subscriptionType': plan, 'rateLimitTier': tier}}))

    def settings(self, database, **extra):
        return dict(database=str(database), claude_auth=str(self.default / '.credentials.json'),
                    paseo_home=str(self.home / '.paseo'), status_inboxes=[str(self.inbox)],
                    sources=[dict(name='WSL .claude/projects', route='claude-code', kind='jsonl', path=str(self.default / 'projects'))],
                    **extra)

    def transcript(self, session, stamps, subagent=False):
        """Assistant requests of one session at the given times."""
        folder = self.project / session / 'subagents' if subagent else self.project
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (('agent-x' if subagent else session) + '.jsonl')
        with path.open('a') as f:
            for i, ts in enumerate(stamps):
                f.write(json.dumps({'type': 'assistant', 'sessionId': session, 'cwd': '/home/user/repo', 'isSidechain': subagent,
                                    'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(ts)) + f'.{int(ts*1000)%1000:03d}Z',
                                    'message': {'id': f'msg-{session}-{subagent}-{ts:.3f}-{i}', 'model': 'claude-test',
                                                'usage': {'input_tokens': 10, 'output_tokens': 5}}}) + '\n')
        return path

    def registry(self, home, pid, session, started, status='idle', status_at=None):
        (home / 'sessions' / f'{pid}.json').write_text(json.dumps(
            {'pid': pid, 'sessionId': session, 'startedAt': started * 1000, 'status': status,
             'statusUpdatedAt': (status_at or started) * 1000, 'kind': 'interactive'}))


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.h = Homes(self.root)
        self.home_patch = patch.object(Path, 'home', return_value=self.h.home); self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.store = Store(self.root / 'usage.db')
        self.cfg = self.h.settings(self.root / 'usage.db')

    def accounts_by_route(self):
        return {ctx['route']: ctx for ctx in accounts(self.store, self.cfg, NOW)}

    def events(self):
        with self.store.connect() as c:
            return {r['id']: dict(r) for r in c.execute("SELECT id,ts,session,account FROM events WHERE route='claude-code'")}

    def accounts_of(self, session):
        return [e['account'] for e in sorted(self.events().values(), key=lambda e: e['ts']) if e['session'] == session]

    # --- identity, discovery and the hook script -------------------------------------------------

    def test_the_hook_status_line_and_resource_store_derive_the_same_opaque_key(self):
        self.assertEqual(claudehook.account_key(MAIN), key('claude-code', MAIN))
        with self.store.connect() as c:
            owner = observe_account(c, 'claude-code', MAIN, NOW)
        self.assertEqual(owner['account_key'], K_MAIN)
        self.assertEqual(claudehook.native_account(self.h.managed / '.claude.json'), K_SECOND)
        self.assertIsNone(claudehook.native_account(self.root / 'missing.json'))

    def test_discovery_keeps_one_entry_per_real_home_and_never_reads_windows_tokens(self):
        windows = self.root / 'mnt/c/Users/Example'
        found = home_candidates({**self.cfg, 'homes': [str(self.h.home), '/mnt/c/Users/Example'],
                                 'claude_homes': [str(self.h.managed), 'relative/home']},
                                observed=[str(self.h.default), '/outside/home'])
        kinds = [(h['kind'], Path(h['path']).name) for h in found]
        self.assertEqual(kinds, [('default', '.claude'), ('configured', 'claude-0123abcd'), ('windows', '.claude')])
        self.assertIsNone(found[-1]['credentials'])
        self.assertFalse(windows.exists())

    def test_plan_labels_and_duplicate_names(self):
        self.assertEqual(plan_label('max', 'default_claude_max_5x'), 'Max 5x')
        self.assertEqual(plan_label('max', 'default_claude_max_20x'), 'Max 20x')
        self.assertEqual(plan_label('pro', 'default_claude_ai'), 'Pro')
        self.assertIsNone(plan_label(None, None))
        rows = [dict(route=FAMILY, plan_label='Max 5x'), dict(route=R_SECOND, plan_label='Max 5x'), dict(route='claude-code:aaaaaaaaaaaa', plan_label=None)]
        self.assertEqual(labels(rows), {FAMILY: 'Max 5x · 기본', R_SECOND: 'Max 5x · ' + R_SECOND[-4:], 'claude-code:aaaaaaaaaaaa': '계정'})
        self.assertEqual(labels(rows, {R_SECOND: '  개인 Max ', FAMILY: ''})[R_SECOND], '개인 Max')

    def test_existing_identified_account_keeps_the_original_route(self):
        with self.store.connect() as c:
            observe_account(c, 'claude-code', MAIN, NOW - 100)
        contexts = self.accounts_by_route()
        self.assertEqual(set(contexts), {FAMILY, R_SECOND})
        self.assertEqual(contexts[FAMILY]['home']['kind'], 'default')
        self.assertEqual(contexts[R_SECOND]['home']['kind'], 'zerosub')
        with self.store.connect() as c:
            rows = {r['route']: r for r in registry(c)}
            self.assertEqual(rows[R_SECOND]['plan_label'], 'Max 5x')
            self.assertEqual(rows[FAMILY]['plan_label'], 'Pro')
            dump = '\n'.join(c.iterdump())
        for private in ('PRIVATE-EMAIL', 'PRIVATE-NAME', 'TEST-ONLY-TOKEN', 'TEST-ONLY-REFRESH', 'main-user', 'second-org'):
            self.assertNotIn(private, dump)

    def test_a_previously_identified_account_stays_original_even_when_the_default_home_changes(self):
        with self.store.connect() as c:
            observe_account(c, 'claude-code', SECOND, NOW - 100)
        contexts = self.accounts_by_route()
        self.assertEqual(contexts[FAMILY]['key'], K_SECOND)
        self.assertEqual(contexts['claude-code:' + K_MAIN[:12]]['home']['kind'], 'default')
        # The original account signed in nowhere: its route is not polled with another login.
        import shutil
        shutil.rmtree(self.h.home / '.paseo')
        contexts = self.accounts_by_route()
        self.assertEqual(set(contexts), {'claude-code:' + K_MAIN[:12]})

    def test_freshest_valid_login_is_polled_and_unreadable_homes_fall_back_to_legacy(self):
        # The same account in two homes: the expired one is never chosen.
        other = self.h.home / 'other-home'
        other.mkdir()
        Homes.write_account(other / '.claude.json', SECOND)
        Homes.write_credentials(other / '.credentials.json', 'max', 'default_claude_max_5x', expires=NOW - 60)
        contexts = {ctx['route']: ctx for ctx in accounts(self.store, {**self.cfg, 'claude_homes': [str(other)]}, NOW)}
        self.assertEqual(contexts[R_SECOND]['home']['kind'], 'zerosub')
        # No readable login anywhere: the original route still polls the default home.
        (self.h.home / '.claude.json').write_text('{}')
        (self.h.managed / '.claude.json').write_text('{}')
        contexts = accounts(Store(self.root / 'fresh.db'), self.cfg, NOW)
        self.assertEqual([(c['route'], c['home']['kind']) for c in contexts], [(FAMILY, 'default')])

    # --- per-account polling --------------------------------------------------------------------

    def usage_response(self, five, seven):
        return BytesIO(json.dumps({'five_hour': {'utilization': five, 'resets_at': '2099-01-01T00:00:00Z'},
                                   'seven_day': {'utilization': seven, 'resets_at': '2099-01-05T00:00:00Z'}}).encode())

    def test_each_account_is_polled_with_its_own_login_into_its_own_route(self):
        tokens = []
        def opener(*handlers):
            class Open:
                def open(self, request, timeout=None):
                    tokens.append(request.get_header('Authorization'))
                    return self_.usage_response(10 if len(tokens) == 1 else 60, 20)
            return Open()
        self_ = self
        Homes.write_credentials(self.h.managed / '.credentials.json', 'max', 'default_claude_max_5x', token='TEST-SECOND-TOKEN')
        with ExitStack() as stack:
            stack.enter_context(patch.object(limits.urllib.request, 'build_opener', side_effect=opener))
            for name in ('poll_claude_resources', 'collect_claude_cloud', 'poll_devin', 'poll_antigravity_app', 'read_codex'):
                stack.enter_context(patch.object(limits, name, side_effect=OSError('offline') if name == 'read_codex' else None))
            limits.poll_limits(self.store, {**self.cfg, 'opencode_auth': str(self.root / 'none.json')})
        self.assertEqual(tokens, ['Bearer TEST-ONLY-TOKEN', 'Bearer TEST-SECOND-TOKEN'])
        rows = {(r['route'], r['bucket']): r for r in self.store.limits(NOW)['limits']}
        self.assertEqual(rows[(FAMILY, 'five_hour')]['remaining'], 90)
        self.assertEqual(rows[(R_SECOND, 'five_hour')]['remaining'], 40)
        self.assertEqual(rows[(R_SECOND, 'five_hour')]['source'], 'claude-oauth' + R_SECOND[len(FAMILY):])
        self.assertEqual(rows[(R_SECOND, 'five_hour')]['family'], FAMILY)
        self.assertEqual(rows[(R_SECOND, 'five_hour')]['account_label'], 'Max 5x')
        self.assertEqual(rows[(FAMILY, 'five_hour')]['account_label'], 'Pro')
        with self.store.connect() as c:
            self.assertNotIn('TEST-SECOND-TOKEN', '\n'.join(c.iterdump()))

    def test_per_model_weekly_limits_from_the_limits_list(self):
        from llm_usage.limits import claude_limits
        data = {'five_hour': {'utilization': 46, 'resets_at': '2099-01-01T00:00:00Z'},
                'seven_day': {'utilization': 3, 'resets_at': '2099-01-02T00:00:00Z'}, 'seven_day_opus': None,
                'limits': [{'kind': 'session', 'percent': 46, 'scope': None},
                           {'kind': 'weekly_scoped', 'percent': 30, 'resets_at': '2099-01-02T00:00:00+00:00',
                            'scope': {'model': {'id': None, 'display_name': 'Fable'}, 'surface': None}},
                           {'kind': 'weekly_scoped', 'percent': 10, 'scope': {'model': None, 'surface': {'display_name': 'Claude Code'}}},
                           {'kind': 'weekly_scoped', 'percent': 120, 'scope': {'model': {'display_name': 'Bad'}}},
                           {'kind': 'weekly_scoped', 'percent': 5, 'scope': {'model': {'display_name': '<script>'}}},
                           'not a row']}
        with self.store.connect() as c:
            self.assertEqual(claude_limits(self.store, c, data, NOW, R_SECOND), 4)
        rows = {r['bucket']: r for r in self.store.limits(NOW)['limits'] if r['route'] == R_SECOND}
        self.assertEqual(set(rows), {'five_hour', 'seven_day', 'seven_day_fable', 'seven_day_claude_code'})
        fable = rows['seven_day_fable']
        self.assertEqual((fable['remaining'], fable['display_name'], fable['scope']['role'], fable['scope']['label']), (70, 'Fable 주간', 'model', 'Fable'))
        self.assertEqual(fable['source'], 'claude-oauth' + R_SECOND[len(FAMILY):])
        self.assertEqual(rows['seven_day_claude_code']['scope']['role'], 'unknown')
        # A later response without the scoped limit marks it as not current.
        with self.store.connect() as c:
            claude_limits(self.store, c, {k: v for k, v in data.items() if k != 'limits'}, NOW + 1, R_SECOND)
        self.assertTrue(next(r for r in self.store.limits(NOW + 2)['limits'] if r['bucket'] == 'seven_day_fable')['omitted_latest'])

    def test_resource_sources_of_every_account_keep_their_fifteen_minute_cadence(self):
        with self.store.connect() as c:
            for name in ('claude-credits', 'claude-credits:' + K_SECOND[:12], 'claude-resets:' + K_SECOND[:12], 'claude-oauth:' + K_SECOND[:12]):
                self.store.source(c, name, 'ok', '', NOW - 700)
        sources = {s['name']: s['status'] for s in self.store.usage(period='7d', now=NOW)['sources']}
        self.assertEqual(sources['claude-credits'], 'ok')
        self.assertEqual(sources['claude-credits:' + K_SECOND[:12]], 'ok')
        self.assertEqual(sources['claude-resets:' + K_SECOND[:12]], 'ok')
        self.assertEqual(sources['claude-oauth:' + K_SECOND[:12]], 'stale')

    def test_ended_account_is_not_polled(self):
        seen = []
        with self.store.connect() as c:
            self.store.source(c, 'claude-oauth' + R_SECOND[len(FAMILY):], 'ok', '', NOW - 60)
        with ExitStack() as stack:
            stack.enter_context(patch.object(limits, 'poll_claude', side_effect=lambda s, cfg, account=None: seen.append(account['route'])))
            for name in ('poll_claude_resources', 'collect_claude_cloud', 'poll_devin', 'poll_antigravity_app'):
                stack.enter_context(patch.object(limits, name))
            stack.enter_context(patch.object(limits, 'read_codex', side_effect=OSError('offline')))
            limits.poll_limits(self.store, {**self.cfg, 'opencode_auth': 'none', 'ended_subscriptions': {R_SECOND: {}}})
        self.assertEqual(seen, [FAMILY])
        with self.store.connect() as c:
            status = c.execute('SELECT status FROM sources WHERE name=?', ('claude-oauth' + R_SECOND[len(FAMILY):],)).fetchone()
        self.assertEqual(status[0], 'ended')

    def test_quota_windows_and_parents_stay_within_each_account(self):
        accounts(self.store, self.cfg, NOW)
        with self.store.connect() as c:
            for route, five, seven in ((FAMILY, 50, 0), (R_SECOND, 80, 100)):
                self.store.limit(c, route, 'five_hour', five, NOW + 3600, NOW - 5, 'claude-oauth' + route[len(FAMILY):])
                self.store.limit(c, route, 'seven_day', seven, NOW + 86400, NOW - 5, 'claude-oauth' + route[len(FAMILY):])
                self.store.source(c, 'claude-oauth' + route[len(FAMILY):], 'ok', '', NOW - 5)
            for i, account in enumerate((FAMILY, R_SECOND, R_SECOND, None)):
                self.store.event(c, f'w{i}', NOW - 600, 'Anthropic', 'claude-code', 'claude-test', dict(uncached_input=100, output=10),
                                 session='s', account=account)
        rows = {(r['route'], r['bucket']): r for r in self.store.limits(NOW)['limits']}
        # Pending (unresolved) requests count for the original account until resolved.
        self.assertEqual(rows[(FAMILY, 'five_hour')]['window']['requests'], 2)
        self.assertEqual(rows[(R_SECOND, 'five_hour')]['window']['requests'], 2)
        # The original account's exhausted week blocks only its own 5-hour window.
        self.assertEqual(rows[(FAMILY, 'five_hour')]['blocked_by']['bucket'], 'seven_day')
        self.assertIsNone(rows[(R_SECOND, 'five_hour')]['blocked_by'])

    # --- attribution ----------------------------------------------------------------------------

    def test_account_decision_rules(self):
        shared = ('shared', FAMILY, 1000)
        alternating = [(100, FAMILY), (200, R_SECOND), (300, FAMILY)]
        self.assertEqual([account_for(t, alternating, shared, FAMILY) for t in (150, 250, 350, 197)],
                         [FAMILY, R_SECOND, FAMILY, FAMILY])
        # Within the clock tolerance the new turn's evidence already applies.
        self.assertEqual(account_for(199, [(100, FAMILY), (200.5, R_SECOND)], shared, FAMILY), R_SECOND)
        self.assertEqual(account_for(50, alternating, shared, FAMILY), FAMILY)      # before cutover, no evidence
        self.assertEqual(account_for(1500, [], shared, FAMILY), UNKNOWN)            # after cutover, no evidence
        self.assertEqual(account_for(1500, [], ('single', R_SECOND), FAMILY), R_SECOND)
        self.assertEqual(account_for(1500, [], None, FAMILY), UNKNOWN)

    def test_folder_policy_uses_the_symlink_time_as_the_second_account_start(self):
        homes = [inspect(h) for h in home_candidates(self.cfg)]
        with self.store.connect() as c:
            observe_account(c, 'claude-code', MAIN, NOW)
        rules = policies(self.store, self.cfg, homes, K_MAIN)
        rule = rules[str(self.h.default / 'projects')]
        self.assertEqual(rule[:2], ('shared', FAMILY))
        self.assertAlmostEqual(rule[2], self.h.joined, places=3)
        # Once shared, the folder stays shared from the recorded time on.
        (self.h.managed / 'projects').unlink(); (self.h.managed / 'projects').mkdir()
        rules = policies(self.store, self.cfg, [inspect(h) for h in home_candidates(self.cfg)], K_MAIN)
        self.assertEqual(rules[str(self.h.default / 'projects')], ('shared', FAMILY, rule[2]))
        # A folder only one account ever wrote is that account's, before and after.
        fresh = Store(self.root / 'other.db')
        self.assertEqual(policies(fresh, self.cfg, [inspect(h) for h in home_candidates(self.cfg)], K_MAIN)[str(self.h.default / 'projects')],
                         ('single', FAMILY))

    def collect(self):
        collect.collect_local(self.store, self.cfg, include_derived=False)
        return refresh(self.store, self.cfg)

    def hook(self, event, session, ts, home):
        env = {'CLAUDE_CONFIG_DIR': str(home)} if home != self.h.default else {}
        self.assertTrue(claudehook.record(self.h.inbox, event, session, now=ts, environ=env))

    def test_history_hooks_registry_and_unknown_requests_are_attributed(self):
        with self.store.connect() as c:
            observe_account(c, 'claude-code', MAIN, NOW - 86400)
        before = self.h.joined - 600
        self.h.transcript('old-session', [before - 60, before])
        # One session moving between accounts, as zerosub reloads it, with hooks.
        self.hook('UserPromptSubmit', 'moving', NOW - 3000, self.h.default)
        self.hook('Stop', 'moving', NOW - 2500, self.h.default)
        self.hook('SessionStart', 'moving', NOW - 2000, self.h.managed)
        self.hook('UserPromptSubmit', 'moving', NOW - 1990, self.h.managed)
        self.hook('UserPromptSubmit', 'moving', NOW - 1000, self.h.default)
        self.h.transcript('moving', [NOW - 2990, NOW - 2600, NOW - 1980, NOW - 1500, NOW - 990])
        self.h.transcript('moving', [NOW - 1970], subagent=True)
        # A session seen only in the second home's live process registry.
        self.h.registry(self.h.managed, 4242, 'registry-only', NOW - 900, status='busy', status_at=NOW - 300)
        self.h.transcript('registry-only', [NOW - 800, NOW - 200])
        # After the second account appeared, no evidence at all.
        self.h.transcript('no-evidence', [NOW - 100])
        changed = self.collect()
        self.assertGreater(changed, 0)
        self.assertEqual(self.accounts_of('old-session'), [FAMILY, FAMILY])
        self.assertEqual(self.accounts_of('moving'), [FAMILY, FAMILY, R_SECOND, R_SECOND, R_SECOND, FAMILY])
        self.assertEqual(self.accounts_of('registry-only'), [R_SECOND, R_SECOND])
        self.assertEqual(self.accounts_of('no-evidence'), [UNKNOWN])
        with self.store.connect() as c:
            self.assertTrue(self.store.rollup_matches())
            hooked = c.execute("SELECT COUNT(*) FROM claude_evidence WHERE kind LIKE 'hook:%'").fetchone()[0]
            self.assertEqual(hooked, 5)
            source = dict(c.execute("SELECT status,detail FROM sources WHERE name='claude-accounts'").fetchone())
        self.assertIn('계정 미확인 요청 1건', source['detail'])

    def test_late_evidence_moves_only_requests_after_it_and_bumps_the_data_version(self):
        with self.store.connect() as c:
            observe_account(c, 'claude-code', MAIN, NOW - 86400)
        self.h.transcript('late', [NOW - 700, NOW - 400, NOW - 100])
        self.collect()
        self.assertEqual(self.accounts_of('late'), [UNKNOWN] * 3)
        with self.store.connect() as c:
            version = self.store.state(c, 'data_version')
        self.h.registry(self.h.managed, 77, 'late', NOW - 450)
        refresh(self.store, self.cfg)
        self.assertEqual(self.accounts_of('late'), [UNKNOWN, R_SECOND, R_SECOND])
        with self.store.connect() as c:
            self.assertGreater(self.store.state(c, 'data_version'), version)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM claude_dirty').fetchone()[0], 0)
        # Nothing new: a second pass changes nothing.
        self.assertEqual(refresh(self.store, self.cfg), 0)

    def test_concurrent_processes_follow_the_latest_turn(self):
        """A remote-control process of the first account idles while the second
        account's process works the same session; each busy turn decides."""
        with self.store.connect() as c:
            observe_account(c, 'claude-code', MAIN, NOW - 86400)
        self.h.registry(self.h.default, 11, 'shared-session', NOW - 5000, status='idle', status_at=NOW - 4000)
        self.h.registry(self.h.managed, 22, 'shared-session', NOW - 2000, status='busy', status_at=NOW - 1500)
        self.h.transcript('shared-session', [NOW - 4500, NOW - 1900, NOW - 1400])
        self.collect()
        self.assertEqual(self.accounts_of('shared-session'), [FAMILY, R_SECOND, R_SECOND])

    def test_cloud_copies_keep_the_account_that_read_them(self):
        from llm_usage.reconciliation import link
        with self.store.connect() as c:
            observe_account(c, 'claude-code', MAIN, NOW - 86400)
            self.store.event(c, 'cloud-1', NOW - 50, 'Anthropic', 'claude-code', 'claude-test', dict(output=5), session='cloud', account=R_SECOND)
            self.store.event(c, 'cloud-old', NOW - 40, 'Anthropic', 'claude-code', 'claude-test', dict(output=5), session='cloud')
            link(c, 'claude:cloud', 'cloud-1', 'claude-cloud:teleport-events')
            link(c, 'claude:cloud', 'cloud-old', 'claude-cloud:teleport-events')
        self.collect()
        events = self.events()
        self.assertEqual(events['cloud-1']['account'], R_SECOND)
        # Before this release every cloud session was read with the original login.
        self.assertEqual(events['cloud-old']['account'], FAMILY)

    def test_single_account_installs_attribute_everything_to_the_original_route(self):
        (self.h.managed / 'projects').unlink()
        import shutil
        shutil.rmtree(self.h.home / '.paseo')
        self.h.transcript('solo', [NOW - 100, NOW - 50])
        self.collect()
        self.assertEqual(self.accounts_of('solo'), [FAMILY, FAMILY])
        with self.store.connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM sources WHERE name='claude-accounts'").fetchone())

    # --- status lines, usage and the hook installer ---------------------------------------------

    def test_status_lines_of_two_homes_never_overwrite_each_other(self):
        accounts(self.store, self.cfg, NOW)
        with self.store.connect() as c:
            observe_account(c, 'claude-code', MAIN, NOW)
        payload = json.dumps({'rate_limits': {'five_hour': {'used_percentage': 30, 'resets_at': NOW + 3600}}}).encode()
        for home in (None, self.h.managed):
            env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_CONFIG_DIR'}
            env['HOME'] = str(self.h.home)
            if home:
                env['CLAUDE_CONFIG_DIR'] = str(home)
            subprocess.run([sys.executable, statusline.__file__, '--route', 'claude-code', '--directory', str(self.h.inbox)],
                           input=payload, capture_output=True, env=env, check=True)
        names = sorted(p.name for p in self.h.inbox.glob('claude-code*.json'))
        self.assertEqual(len(names), 2)
        self.assertIn('claude-code.json', names)
        collect.collect_status(self.store, self.cfg)
        rows = {(r['route'], r['source']) for r in self.store.limits(NOW)['limits'] if r['bucket'] == 'five_hour'}
        self.assertEqual(rows, {(FAMILY, FAMILY), (R_SECOND, R_SECOND)})

    def test_usage_groups_scopes_and_values_each_account(self):
        accounts(self.store, self.cfg, NOW)
        with self.store.connect() as c:
            for i, account in enumerate((FAMILY, R_SECOND, R_SECOND, UNKNOWN)):
                self.store.event(c, f'u{i}', NOW - 3600, 'Anthropic', 'claude-code', 'claude-test',
                                 dict(uncached_input=1_000_000, output=0), session='mixed', account=account)
            self.store.event(c, 'codex-1', NOW - 3600, 'OpenAI', 'codex', 'gpt-test', dict(uncached_input=1_000_000), session='cx')
        pricing = {'claude-test': dict(input=1, output=1), 'gpt-test': dict(input=1, output=1)}
        data = self.store.usage(period='7d', group='account', pricing=pricing,
                                subscriptions={'claude-code': 20, 'codex': 200}, names={R_SECOND: '두 번째'})
        self.assertEqual(set(data['labels']), {FAMILY, R_SECOND, UNKNOWN, 'codex'})
        self.assertEqual(set(data['scope_options']['account']), {FAMILY, R_SECOND, UNKNOWN})
        subs = {s['route']: s for s in data['subscriptions']}
        self.assertEqual(subs['claude-code']['period_cost'], 1)
        self.assertEqual(subs[R_SECOND]['period_cost'], 2)
        self.assertIsNone(subs[R_SECOND]['monthly_usd'])
        self.assertEqual(subs['codex']['period_cost'], 1)
        self.assertEqual((subs['claude-code']['period_tokens'], subs[R_SECOND]['period_tokens']), (1_000_000, 2_000_000))
        # Without list prices each account still reports whether it was used in the period.
        unpriced = {s['route']: s for s in self.store.usage(period='7d', subscriptions={'claude-code': 20}, names={R_SECOND: '두 번째'})['subscriptions']}
        self.assertEqual((unpriced[R_SECOND]['period_cost'], unpriced[R_SECOND]['period_tokens']), (None, 2_000_000))
        session = next(s for s in data['sessions'] if s['session'] == 'mixed')
        self.assertEqual(session['accounts'], sorted([FAMILY, R_SECOND, UNKNOWN]))
        self.assertEqual({a['route']: a['label'] for a in data['claude_accounts']}, {FAMILY: 'Pro', R_SECOND: '두 번째'})
        scoped = self.store.usage(period='7d', group='model', scope='account:' + R_SECOND, pricing=pricing)
        self.assertEqual(scoped['totals']['requests'], 2)
        # The route view still shows one Claude Code service.
        by_route = self.store.usage(period='7d', group='route', pricing=pricing)
        self.assertEqual(sorted(by_route['labels']), ['claude-code', 'codex'])
        # Its total split by account, 계정 미확인 included, adds up to the service.
        split = {e['account']: e for e in by_route['claude_split']}
        self.assertEqual({k: (v['tokens'], v['requests'], v['cost']) for k, v in split.items()},
                         {FAMILY: (1_000_000, 1, 1), R_SECOND: (2_000_000, 2, 2), UNKNOWN: (1_000_000, 1, 1)})
        claude = sum(r['uncached_input'] + r['cached_input'] + r['output'] + r['cache_creation'] for r in by_route['rows'] if r['route'] == 'claude-code')
        self.assertEqual(sum(e['tokens'] for e in by_route['claude_split']), claude)
        # A cloud supplement read with the second login joins that account's part.
        from llm_usage.reconciliation import snapshot, total
        with self.store.connect() as c:
            self.store.save_state(c, 'claude_cloud_account:k-second', R_SECOND)
            snapshot(self.store, c, source='claude-cloud', scope_id='claude:cloud-only', route='claude-code', provider='Anthropic',
                     model='claude-test', session='cloud-only', start=NOW - 7200, end=NOW - 7100, tokens=500,
                     parts=dict(uncached_input=0, cached_input=0, cache_creation=0, output=500), account_key='k-second')
        supplemented = self.store.usage(period='7d', group='route', pricing=pricing)
        split = {e['account']: e['tokens'] for e in supplemented['claude_split']}
        self.assertEqual(split[R_SECOND], 2_000_500)
        claude = sum(total(r) for r in supplemented['rows'] if r['route'] == 'claude-code')
        self.assertEqual(sum(split.values()), claude)
        # One account has no split (nothing to tell apart).
        self.assertIsNone(Store(Path(self.tmp.name) / 'single.db').usage(period='7d')['claude_split'])

    def test_weekly_report_splits_claude_code_by_account(self):
        from llm_usage import reports
        accounts(self.store, self.cfg, NOW)
        _, this_monday = reports.last_week(NOW)
        with self.store.connect() as c:
            # NOW minus a week always lies in the last finished week.
            for i, account in enumerate((FAMILY, R_SECOND, R_SECOND, UNKNOWN)):
                self.store.event(c, f'w{i}', NOW - 7 * 86400, 'Anthropic', 'claude-code', 'claude-test',
                                 dict(uncached_input=1_000_000), account=account)
            self.store.event(c, 'this-week', this_monday.timestamp(), 'Anthropic', 'claude-code', 'claude-test', dict(output=10), account=R_SECOND)
        report = reports.build(self.store, NOW)
        self.assertEqual([(a['account'], a['tokens']) for a in report['accounts']],
                         [(R_SECOND, 2_000_000), (FAMILY, 1_000_000), (UNKNOWN, 1_000_000)])
        # 계정 미확인 is one of the parts, so they add up to the service.
        service = next(r['tokens'] for r in report['routes'] if r['route'] == 'claude-code')
        self.assertEqual(sum(a['tokens'] for a in report['accounts']), service)
        self.assertEqual(report['accounts'][2]['label'], '계정 미확인')
        # The notification names accounts as set in the dashboard, else the stored plan name.
        text = reports.summary(report, names={R_SECOND: '두 번째'})
        self.assertIn(f"Claude 계정별 두 번째 2.0M · {report['accounts'][1]['label']} 1.0M · 계정 미확인 1.0M", text)
        week = reports.this_week(self.store, NOW)
        self.assertEqual([(a['account'], a['tokens']) for a in week['accounts']], [(R_SECOND, 10)])

    def test_monthly_settlement_names_accounts_and_skips_ones_not_seen_yet(self):
        from llm_usage import monthly
        accounts(self.store, self.cfg, NOW)
        last = monthly.shift(monthly.month_start(NOW), -1)
        with self.store.connect() as c:
            self.store.event(c, 'm1', last.timestamp() + 86400, 'Anthropic', 'claude-code', 'claude-test',
                             dict(uncached_input=1_000_000), account=FAMILY)
        settled = monthly.build(self.store, last, NOW, {'claude-test': dict(input=1, output=1)}, {'claude-code': 20, R_SECOND: 100}, {})
        # The second account was first seen this month: last month has nothing to settle for it.
        self.assertEqual([(s['route'], s.get('label'), s['value']) for s in settled['subscriptions']], [(FAMILY, 'Pro', 1.0)])

    def test_monthly_rebuild_settles_the_same_accounts_as_the_first_time(self):
        from llm_usage import monthly
        accounts(self.store, self.cfg, NOW)
        last = monthly.shift(monthly.month_start(NOW), -1)
        with self.store.connect() as c:
            self.store.event(c, 'm1', last.timestamp() + 86400, 'Anthropic', 'claude-code', 'claude-test',
                             dict(uncached_input=1_000_000), account=FAMILY)
        # The original account's subscription was ended before last month began.
        first = monthly.build(self.store, last, NOW, None, {'claude-code': 20}, {FAMILY: {'since': last.timestamp() - 1}})
        self.assertEqual(first['subscriptions'], [])
        # A rebuild has no end dates to go by; it settles what the first settlement did.
        self.assertEqual(monthly.build(self.store, last, NOW + 60, None, stored=first)['subscriptions'], [])

    def test_hook_installer_merges_with_existing_hooks_once(self):
        settings_file = self.h.default / 'settings.json'
        existing = {'statusLine': {'type': 'command', 'command': 'echo x'},
                    'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'paseo hooks claude Stop'}]}]}}
        settings_file.write_text(json.dumps(existing))
        script = self.h.inbox / 'claudehook.py'
        self.assertTrue(cli.install_account_hooks(settings_file, script, self.h.inbox))
        saved = json.loads(settings_file.read_text())
        self.assertEqual(saved['statusLine'], existing['statusLine'])
        self.assertEqual(saved['hooks']['Stop'][0], existing['hooks']['Stop'][0])
        for event in ('SessionStart', 'UserPromptSubmit', 'Stop'):
            commands = [h['command'] for g in saved['hooks'][event] for h in g['hooks']]
            self.assertEqual(sum('llm-usage/status/claudehook.py' in c for c in commands), 1, event)
        self.assertEqual(len(list(self.h.default.glob('settings.json.llm-usage-backup-*'))), 1)
        self.assertFalse(cli.install_account_hooks(settings_file, script, self.h.inbox))
        settings_file.write_text(json.dumps({'hooks': []}))
        with self.assertRaises(ValueError):
            cli.install_account_hooks(settings_file, script, self.h.inbox)

    def test_hook_script_is_silent_and_never_fails(self):
        env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_CONFIG_DIR'}
        env['HOME'] = str(self.h.home); env['CLAUDE_CONFIG_DIR'] = str(self.h.managed)
        for body in (b'{"hook_event_name":"UserPromptSubmit","session_id":"abc-1","prompt":"PRIVATE PROMPT"}', b'not json', b''):
            result = subprocess.run([sys.executable, claudehook.__file__, '--directory', str(self.h.inbox)], input=body,
                                    capture_output=True, env=env, timeout=20)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'', b''))
        with closing(sqlite3.connect(self.h.inbox / 'claude-accounts.db')) as c:
            rows = c.execute('SELECT session,event,account,home FROM markers').fetchall()
            dump = '\n'.join(c.iterdump())
        self.assertEqual(rows, [('abc-1', 'UserPromptSubmit', K_SECOND, str(self.h.managed))])
        self.assertNotIn('PRIVATE', dump)
        self.assertFalse(claudehook.record(self.h.inbox, 'PreToolUse', 'abc-1'))
        self.assertFalse(claudehook.record(self.h.inbox, 'Stop', '../bad'))

    def test_describe_lists_accounts_without_keys(self):
        accounts(self.store, self.cfg, NOW)
        with self.store.connect() as c:
            rows = describe(c, {R_SECOND: 'Max'}, ended={R_SECOND})
        self.assertEqual([r['route'] for r in rows], [FAMILY, R_SECOND])
        self.assertTrue(rows[1]['ended']); self.assertEqual(rows[1]['custom_name'], 'Max')
        self.assertNotIn('account_key', json.dumps(rows))
        self.assertEqual(rows[1]['homes'], [{'kind': 'zerosub', 'name': 'claude-0123abcd'}])


if __name__ == '__main__':
    unittest.main()

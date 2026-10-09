"""Resource/decision scenarios, isolated from credentials, billing and live APIs."""
import copy
import json
from io import BytesIO
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_usage.limits import codex_limits, claude_limits, poll_claude_resources
from llm_usage.planning import decide, plan
from llm_usage.resources import (Conflict, account, amount, claude_balance, claude_resets,
                                 observe_account, save_auto, write_manual)
from llm_usage.store import Store
from llm_usage.webapp import create_app


class ResourcesPlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'usage.db')
        self.now = time.time()

    def codex(self, checked, remaining=40, weekly=12, owner='A', extra=None, reset=None, credits=None):
        groups = {'codex': {'primary': dict(usedPercent=100-remaining, windowDurationMins=300, resetsAt=self.now+14400),
                            'secondary': dict(usedPercent=100-weekly, windowDurationMins=10080, resetsAt=self.now+3*86400)}}
        if credits is not None:
            groups['codex']['credits'] = credits
        groups.update(extra or {})
        data = dict(accountId=owner, rateLimitsByLimitId=groups, rateLimitResetCredits=reset)
        with self.store.connect() as c:
            codex_limits(self.store, c, data, checked)
            self.store.source(c, 'codex', 'ok', checked=checked)

    def paced(self):
        for minute in range(180, -1, -5):
            self.codex(self.now-minute*60, 40+20*min(minute, 60)/60, 12+8*minute/60)
        return self.store.limits(self.now)

    def manual(self, **overrides):
        return dict(route='codex', kind='usage_credit', label='보유 크레딧', amount=20, unit='credit',
                    scope='subscription', checked=self.now, enabled=None, **overrides)

    def item(self, pool):
        return next(r for r in self.store.limits(self.now)['resources']['items'] if r['pool_key'] == pool)

    def test_workspace_credits_are_not_duplicated_per_model(self):
        credits = dict(balance='12.50', hasCredits=True, unlimited=False)
        self.codex(self.now, credits=credits, extra={'model-A': {'credits': credits}})
        items = [r for r in self.store.limits(self.now)['resources']['items'] if r['kind'] == 'usage_credit']
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]['amount'], items[0]['unit']), (12.5, 'credit'))

    def test_reset_authoritative_count_not_detail_length(self):
        self.codex(self.now, reset={'availableCount': 3, 'credits': [{'id': 'private-grant', 'status': 'available', 'expiresAt': self.now+3600}]})
        item = self.item('reset-grants')
        self.assertEqual(item['amount'], 3)
        self.assertEqual(len(item['grants']), 1)
        self.assertNotIn('private-grant', json.dumps(item))

    def test_missing_resource_preserves_value_but_invalidates_it(self):
        self.codex(self.now-60, reset={'availableCount': 2, 'credits': None})
        self.codex(self.now)
        item = self.item('reset-grants')
        self.assertEqual(item['amount'], 2)
        self.assertEqual(item['status'], 'unavailable')
        self.assertEqual(item['value_checked'], self.now-60)

    def test_null_zero_and_empty_details_are_distinct(self):
        self.codex(self.now-60, reset={'availableCount': 0, 'credits': []})
        item = self.item('reset-grants')
        self.assertEqual(item['amount'], 0); self.assertTrue(item['details_known'])
        self.codex(self.now, reset={'availableCount': 2, 'credits': None})
        item = self.item('reset-grants')
        self.assertEqual(item['amount'], 2); self.assertFalse(item['details_known'])

    def test_account_switch_breaks_pace_and_manual_ownership(self):
        for minute in range(20, 0, -5):
            self.codex(self.now-minute*60, 90, 90)
        manual = write_manual(self.store, self.manual())
        self.codex(self.now, 30, 30, owner='B')
        data = self.store.limits(self.now)
        row = next(r for r in data['limits'] if r['bucket'] == 'codex · 300분')
        self.assertIsNone(row['paces']['recent']['per_hour'])
        self.assertEqual(len(row['history']), 1)
        item = next(r for r in data['resources']['items'] if r['id'] == manual['id'])
        self.assertEqual(item['status'], 'previous_account')
        with self.store.connect() as c:
            self.assertNotIn('"accountId"', '\n'.join(c.iterdump()))

    def test_canonical_account_value_and_pace_stay_together(self):
        self.paced()
        with self.store.connect() as c:
            self.store.limit(c, 'codex', 'codex · 300분', 2, self.now+14400, self.now+1, 'codex-local')
        row = next(r for r in self.store.limits(self.now+1)['limits'] if r['bucket'] == 'codex · 300분')
        self.assertEqual((row['source'], row['history_source'], row['remaining']), ('codex', 'codex', 40))

    def test_future_observation_does_not_block_current_account_read(self):
        with self.store.connect() as c:
            self.store.limit(c, 'codex', 'codex · 300분', 80, self.now+7200, self.now+3600, 'codex-local')
        self.codex(self.now, 10)
        row = next(r for r in self.store.limits(self.now)['limits'] if r['bucket'] == 'codex · 300분')
        self.assertEqual(row['remaining'], 10)

    def test_plan_change_starts_a_new_observation_epoch(self):
        with self.store.connect() as c:
            first=observe_account(c,'codex','A',self.now-300,plan_revision='plus')
            second=observe_account(c,'codex','A',self.now,plan_revision='pro')
            self.assertEqual(first['account_key'],second['account_key'])
            self.assertNotEqual(first['epoch'],second['epoch'])

    def test_old_observation_cannot_change_account_or_current_value(self):
        self.codex(self.now, 10, owner='B')
        self.codex(self.now-60, 90, owner='A')
        row = next(r for r in self.store.limits(self.now)['limits'] if r['bucket'] == 'codex · 300분')
        self.assertEqual(row['remaining'], 10)
        with self.store.connect() as c:
            self.assertEqual(account(c, 'codex')['since'], self.now)

    def test_two_hour_job_binds_on_weekly_before_session(self):
        result = plan(self.paced(), 'codex', hours=2)
        self.assertEqual(result['state'], 'shortage')
        self.assertEqual(result['bottleneck'], 'codex · 10080분')
        self.assertAlmostEqual(result['seconds'], 5400)

    def test_partial_stale_required_quota_does_not_recommend_service(self):
        data = self.paced()
        next(r for r in data['limits'] if r['bucket'] == 'codex · 10080분')['status'] = 'stale'
        self.assertEqual(decide(data, 'codex')['state'], 'unknown')

    def test_partial_response_does_not_leave_missing_window_fresh(self):
        self.paced()
        with self.store.connect() as c:
            codex_limits(self.store,c,{'accountId':'A','rateLimits':{'primary':dict(usedPercent=20,windowDurationMins=300,resetsAt=self.now+14400)}},self.now+1)
        data=self.store.limits(self.now+1)
        weekly=next(r for r in data['limits'] if r['bucket']=='codex · 10080분')
        self.assertEqual(weekly['status'],'unavailable')
        self.assertEqual(weekly['remaining'],12)
        self.assertEqual(decide(data,'codex')['state'],'unknown')

    def test_credit_only_response_is_a_successful_resource_read(self):
        with self.store.connect() as c:
            count=codex_limits(self.store,c,{'accountId':'A','rateLimits':{'credits':{'balance':'10','hasCredits':True}}},self.now)
            self.store.source(c,'codex','ok' if count else 'unavailable',checked=self.now)
        self.assertEqual(self.item('workspace-credits')['status'],'fresh')

    def test_expired_snapshot_cannot_recommend_even_if_status_says_fresh(self):
        data = self.paced()
        self.assertEqual(decide(data, 'codex', now=self.now+7200)['state'], 'unknown')

    def test_idle_is_not_unlimited_capacity(self):
        for minute in range(30, -1, -5):
            self.codex(self.now-minute*60, 80, 80)
        data = self.store.limits(self.now)
        self.assertEqual(decide(data, 'codex')['state'], 'unknown')
        self.assertIsNone(next(r for r in data['limits'] if r['route'] == 'codex')['capacity'])

    def test_recent_burst_is_not_hidden_by_three_hour_average(self):
        for minute in range(180, -1, -5):
            value = 50 if minute >= 15 else 40+10*minute/15
            self.codex(self.now-minute*60, value, value)
        row = next(r for r in self.store.limits(self.now)['limits'] if r['bucket'] == 'codex · 10080분')
        self.assertEqual(row['pace_change'], 'faster')
        self.assertAlmostEqual(row['paces']['recent']['per_hour'], 20)
        self.assertAlmostEqual(row['paces']['baseline']['per_hour'], 10/3)

    def test_reset_inside_job_does_not_invent_next_window(self):
        data = self.paced()
        for row in data['limits']:
            if row['route'] == 'codex':
                row['remaining'] = 90; row['resets'] = self.now+1800
        self.assertEqual(decide(data, 'codex')['state'], 'reset_pending')

    def test_specific_model_zero_does_not_hide_usable_other_model(self):
        for minute in range(30, -1, -5):
            with self.store.connect() as c:
                claude_limits(self.store, c, {
                    'five_hour': dict(utilization=20-minute/30, resets_at=self.now+14400),
                    'seven_day': dict(utilization=20-minute/30, resets_at=self.now+86400),
                    'seven_day_opus': dict(utilization=100, resets_at=self.now+86400)}, self.now-minute*60)
        data = self.store.limits(self.now)
        self.assertEqual(decide(data, 'claude-code', 'opus')['state'], 'shortage')
        self.assertEqual(decide(data, 'claude-code', 'sonnet')['state'], 'room')
        self.assertEqual(decide(data, 'claude-code', 'common')['state'], 'unknown')

    def test_explicitly_absent_optional_model_window_is_not_a_permanent_block(self):
        for minute in range(30,-1,-5):
            with self.store.connect() as c:
                claude_limits(self.store,c,{'five_hour':dict(utilization=20-minute/30,resets_at=self.now+14400),
                    'seven_day':dict(utilization=20-minute/30,resets_at=self.now+86400),
                    'seven_day_opus':dict(utilization=100,resets_at=self.now+86400) if minute else None},self.now-minute*60)
        data=self.store.limits(self.now)
        self.assertEqual(decide(data,'claude-code','opus')['state'],'room')
        old=next(r for r in data['limits'] if r['bucket']=='seven_day_opus')
        self.assertTrue(old['not_applicable']);self.assertEqual(old['remaining'],0)

    def test_codex_additional_model_quota_participates_in_decision(self):
        data = self.paced()
        row = copy.deepcopy(next(r for r in data['limits'] if r['bucket'] == 'codex · 300분'))
        row.update(bucket='special · 300분', remaining=0, scope={'role': 'model', 'group': 'special', 'label': 'Special'})
        data['limits'].append(row)
        self.assertEqual(decide(data, 'codex', 'special')['seconds'], 0)

    def test_provider_block_overrides_positive_percent_without_changing_it(self):
        data=self.paced()
        row=next(r for r in data['limits'] if r['bucket']=='codex · 300분')
        row['scope']['locked']=True
        decision=decide(data,'codex')
        self.assertEqual(decision['state'],'shortage');self.assertTrue(decision['provider_blocked'])
        self.assertEqual(row['remaining'],40)

    def test_claude_money_and_balance_are_separate_and_zero_is_known(self):
        with self.store.connect() as c:
            claude_limits(self.store, c, {'spend': {'enabled': True,
                'used': {'amount_minor': 3000, 'currency': 'USD', 'exponent': 2},
                'limit': {'amount_minor': 3000, 'currency': 'USD', 'exponent': 2}}}, self.now)
            claude_balance(c, {'amount': 5000, 'currency': 'USD'}, self.now)
        limit, balance = self.item('spending-allowance'), self.item('prepaid-credits')
        self.assertEqual((limit['amount'], limit['spent'], balance['amount']), (0, 30, 50))
        self.assertTrue(limit['allowance'])

    def test_null_currency_is_not_assumed_usd(self):
        with self.store.connect() as c:
            claude_balance(c, {'amount': 0, 'currency': None}, self.now)
        item = self.item('prepaid-credits')
        self.assertEqual((item['amount'], item['unit'], item['status']), (0, 'minor', 'fresh'))

    def test_scoped_credits_are_not_all_treated_as_subscription_balance(self):
        with self.store.connect() as c:
            claude_balance(c, {'amount': 5000, 'amount_without_scoped_credits': 1000, 'currency': 'USD'}, self.now)
        item = self.item('prepaid-credits')
        self.assertEqual((item['amount'], item['reported_total']), (10, 50))

    def test_resource_poll_is_get_only_and_has_independent_cooldowns(self):
        folder = self.root/'.claude'; folder.mkdir()
        credentials = folder/'.credentials.json'
        credentials.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'TEST-PRIVATE', 'expiresAt': (self.now+3600)*1000}}))
        (self.root/'.claude.json').write_text(json.dumps({'oauthAccount': {'accountUuid': 'test-user', 'organizationUuid': 'test-org', 'emailAddress': 'PRIVATE'}}))
        responses = [BytesIO(json.dumps({'amount': 0, 'currency': None}).encode()),
                     BytesIO(json.dumps({'cedar_ember': {'eligible': False, 'grants': []}}).encode())]
        with patch('llm_usage.limits.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = responses
            poll_claude_resources(self.store, {'claude_auth': str(credentials)})
            poll_claude_resources(self.store, {'claude_auth': str(credentials)})
            self.assertEqual(opener.return_value.open.call_count, 2)
            for call in opener.return_value.open.call_args_list:
                request = call.args[0]
                self.assertEqual(request.get_method(), 'GET')
                self.assertIsNone(request.data)
        with self.store.connect() as c:
            dump = '\n'.join(c.iterdump())
            self.assertNotIn('PRIVATE', dump)
            self.assertNotIn('test-user', dump)
            self.assertNotIn('test-org', dump)

    def test_missing_native_profile_recovers_without_credential_change(self):
        folder=self.root/'.claude';folder.mkdir();credentials=folder/'.credentials.json'
        credentials.write_text(json.dumps({'claudeAiOauth':{'accessToken':'TEST-PRIVATE','expiresAt':(self.now+3600)*1000}}))
        cfg={'claude_auth':str(credentials)}
        with patch('llm_usage.limits.time.time',return_value=self.now),patch('llm_usage.limits.urllib.request.build_opener') as opener:
            poll_claude_resources(self.store,cfg);opener.assert_not_called()
        (self.root/'.claude.json').write_text(json.dumps({'oauthAccount':{'accountUuid':'u','organizationUuid':'o'}}))
        with patch('llm_usage.limits.time.time',return_value=self.now+61),patch('llm_usage.limits.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=[BytesIO(b'{"amount":0}'),BytesIO(b'{"cedar_ember":{"eligible":false,"grants":[]}}')]
            poll_claude_resources(self.store,cfg)
            self.assertEqual(opener.return_value.open.call_count,2)

    def test_credit_balance_cannot_bypass_disabled_or_spent_cap(self):
        from llm_usage.planning import fallback_resources
        for enabled, remaining in ((False, 30), (True, 0)):
            with self.subTest(enabled=enabled, remaining=remaining):
                data = {'resources': {'items': [dict(id='balance', route='claude-code', label='크레딧', kind='usage_credit', origin='auto',
                    status='fresh', active_account=True, amount=50, unit='USD', scope='subscription'),
                    dict(id='cap', route='claude-code', label='지출', kind='usage_credit', origin='auto', status='fresh', active_account=True,
                    allowance=True, amount=remaining, unit='USD', enabled=enabled, spend_remaining=remaining)]}}
                result = fallback_resources(data, 'claude-code', 'sonnet', ['seven_day'], self.now)
                self.assertFalse(result[0]['can_resolve'])

    def test_api_credit_is_not_subscription_fallback(self):
        write_manual(self.store, {**self.manual(), 'kind': 'api_credit', 'scope': 'api', 'unit': 'USD'})
        from llm_usage.planning import fallback_resources
        self.assertEqual(fallback_resources(self.store.limits(self.now), 'codex', 'common', ['weekly'], self.now), [])

    def test_partial_reset_cannot_clear_other_exhausted_window(self):
        with self.store.connect() as c:
            claude_resets(c, {'cedar_ember': {'eligible': True, 'grants': [dict(id='g', resets_left=1,
                clears=['five_hour'], usable_now=True, ends_at=self.now+3600)]}}, self.now)
        from llm_usage.planning import fallback_resources
        result = fallback_resources(self.store.limits(self.now), 'claude-code', 'sonnet', ['five_hour', 'seven_day'], self.now)
        self.assertIsNone(result[0]['can_resolve'])

    def test_expired_or_paused_grants_are_not_recommended(self):
        with self.store.connect() as c:
            claude_resets(c, {'cedar_ember': {'eligible': True, 'grants': [dict(id='g', resets_left=1,
                clears=['five_hour'], usable_now=True, ends_at=self.now-1)]}}, self.now)
        from llm_usage.planning import fallback_resources
        result = fallback_resources(self.store.limits(self.now), 'claude-code', 'sonnet', ['five_hour'], self.now)
        self.assertFalse(result[0]['can_resolve'])

    def test_linked_manual_record_is_not_a_second_balance(self):
        self.codex(self.now, credits=dict(balance='20', hasCredits=True))
        target = self.item('workspace-credits')
        write_manual(self.store, {**self.manual(), 'amount': 30, 'linked_id': target['id']})
        item = next(r for r in self.store.limits(self.now)['resources']['items'] if r['origin'] == 'manual')
        self.assertEqual(item['duplicate_of'], target['id'])
        self.assertTrue(item['conflicts_with_auto'])

    def test_manual_edit_conflict_keeps_committed_record(self):
        created = write_manual(self.store, self.manual())
        write_manual(self.store, {**self.manual(), 'amount': 25, 'revision': created['revision']}, created['id'])
        with self.assertRaises(Conflict):
            write_manual(self.store, {**self.manual(), 'amount': 2, 'revision': created['revision']}, created['id'])
        item = next(r for r in self.store.limits(self.now)['resources']['items'] if r['id'] == created['id'])
        self.assertEqual(item['amount'], 25)

    def test_timed_out_create_can_be_retried_without_duplicate_balance(self):
        body={**self.manual(), 'request_id': 'a'*32}
        first=write_manual(self.store,body)
        second=write_manual(self.store,body)
        self.assertEqual(first['id'],second['id']); self.assertTrue(second['replayed'])
        with self.assertRaises(Conflict):write_manual(self.store,{**body,'amount':30})
        self.assertEqual(len(self.store.limits(self.now)['resources']['items']),1)

    def test_create_replay_does_not_accept_a_later_concurrent_edit(self):
        body={**self.manual(),'request_id':'b'*32}
        first=write_manual(self.store,body)
        write_manual(self.store,{**self.manual(),'amount':22,'revision':1},first['id'])
        with self.assertRaises(Conflict):write_manual(self.store,body)

    def test_bad_reset_does_not_erase_valid_credits_or_invent_reset_time(self):
        with self.store.connect() as c:
            codex_limits(self.store,c,{'accountId':'A','rateLimits':{'credits':{'balance':'10','hasCredits':True},
                'primary':{'usedPercent':20,'windowDurationMins':300,'resetsAt':'not-a-time'}}},self.now)
        row=next(r for r in self.store.limits(self.now)['limits'] if r['route']=='codex')
        self.assertEqual(row['remaining'],80);self.assertIsNone(row['resets'])
        self.assertTrue(row['scope']['reset_unparsed']);self.assertEqual(self.item('workspace-credits')['amount'],10)

    def test_independent_api_record_survives_subscription_account_switch(self):
        self.codex(self.now-60,owner='A')
        record=write_manual(self.store,{**self.manual(),'kind':'api_credit','scope':'api','unit':'USD'})
        self.codex(self.now,owner='B')
        item=next(r for r in self.store.limits(self.now)['resources']['items'] if r['id']==record['id'])
        self.assertEqual(item['status'],'manual')
        self.assertTrue(item['active_account']);self.assertFalse(item['identity_verified'])

    def test_manual_validation_rejects_invalid_values_and_units(self):
        for change in ({'amount': float('nan')}, {'amount': True}, {'amount': -1}, {'scope': 'model'},
                       {'kind': 'reset', 'unit': 'USD'}, {'expires': '2026-10-11T10:00'}, {'checked': self.now+7200},
                       {'kind': 'api_credit', 'scope': 'subscription'}, {'enabled': 'yes'}, {'secret': 'no'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                write_manual(self.store, {**self.manual(), **change})

    def test_manual_age_and_expiry_do_not_turn_into_live_balance(self):
        old = write_manual(self.store, {**self.manual(), 'checked': self.now-8*86400})
        expired = write_manual(self.store, {**self.manual(), 'expires': self.now-1})
        rows = {r['id']: r for r in self.store.limits(self.now)['resources']['items']}
        self.assertEqual(rows[old['id']]['status'], 'stale')
        self.assertEqual(rows[expired['id']]['status'], 'expired')

    def test_credit_topup_is_not_a_negative_consumption_rate(self):
        with self.store.connect() as c:
            for minute, value in ((30, 10), (15, 5), (0, 100)):
                save_auto(c, 'codex', 'credits', 'usage_credit', self.now-minute*60,
                          dict(amount=value, unit='credit', label='크레딧', scope='subscription'), 'codex')
        self.assertIsNone(self.item('credits')['rate_per_hour'])

    def test_explicit_spend_counter_needs_same_period(self):
        with self.store.connect() as c:
            for minute in range(30, -1, -5):
                save_auto(c, 'codex', 'metered', 'usage_credit', self.now-minute*60,
                    dict(amount=20, spent=30-minute, period_reset=self.now+3600, unit='credit', label='지출', scope='subscription'), 'codex')
        self.assertAlmostEqual(self.item('metered')['rate_per_hour'], 60)

    def test_migration_can_run_twice_without_losing_old_usage(self):
        with self.store.connect() as c:
            self.store.limit(c, 'opencode-go', 'weekly', 50, self.now+3600, self.now, 'opencode-go')
        second = Store(self.root/'usage.db')
        self.assertEqual(next(r for r in second.limits(self.now)['limits'] if r['route'] == 'opencode-go')['remaining'], 50)

    def test_nonfinite_provider_credit_does_not_become_zero(self):
        for value in ('NaN', '-1', 'Infinity', True, None):
            self.assertIsNone(amount(value))
        self.assertEqual(amount('0'), 0)


class ResourceApiTests(unittest.TestCase):
    manual = ResourcesPlanningTests.manual
    codex = ResourcesPlanningTests.codex
    paced = ResourcesPlanningTests.paced

    def setUp(self):
        ResourcesPlanningTests.setUp(self)
        self.app = create_app(dict(database=str(self.root/'usage.db'), origin='https://fixture.test',
                                   secret_key='fixture-only', allowed_logins=['fixture']))
        self.client = self.app.test_client()

    def request(self, path, method='GET', data=None, token=True):
        headers = {'Host': 'fixture.test', 'Tailscale-User-Login': 'fixture', 'Origin': 'https://fixture.test'}
        if token and method != 'GET':
            headers['X-CSRF-Token'] = self.request('/api/bootstrap').get_json()['csrf']
        return self.client.open(path, method=method, json=data, headers=headers,
                                environ_base={'gunicorn.socket': type('Sock', (), {'family': socket.AF_UNIX})()})

    def test_crud_requires_csrf_and_revisions(self):
        self.assertEqual(self.request('/api/resources/manual', 'POST', self.manual(), False).status_code, 403)
        created = self.request('/api/resources/manual', 'POST', self.manual()).get_json()
        endpoint = '/api/resources/manual/' + created['id']
        self.assertEqual(self.request(endpoint, 'PATCH', {**self.manual(), 'revision': 0}).status_code, 409)
        self.assertEqual(self.request(endpoint, 'DELETE', {'revision': 1}).status_code, 200)
        self.assertEqual(self.request(endpoint, 'DELETE', {'revision': 1}).status_code, 404)

    def test_limits_and_decision_share_one_observation(self):
        self.paced()
        data = self.request('/api/limits?route=codex&hours=2').get_json()
        self.assertEqual(data['now'], data['planning']['now'])
        self.assertEqual(data['planning']['state'], 'shortage')
        for query in ('hours=nan', 'hours=-1', 'pace=wrong', 'today_hours=40', 'week_hours=inf'):
            self.assertEqual(self.request('/api/limits?'+query).status_code, 400)
        changed=self.request('/api/limits?route=codex&model=removed').get_json()['planning']
        self.assertTrue(changed['selection_changed']);self.assertEqual(changed['state'],'unknown')


if __name__ == '__main__':
    unittest.main()

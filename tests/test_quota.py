"""Quota trend boundaries and OAuth cooldowns; no live credentials or requests."""
from datetime import datetime,timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler,HTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from llm_usage.insights import quota_trends,quota_decreases,quota_pace
from llm_usage.limits import poll_claude,poll_limits,read_codex,retry_delay,NoRedirect
from llm_usage.store import Store,stamp

NOW=stamp('2026-09-09T03:00:00Z')


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'usage.db')
        self.auth=self.root/'credentials.json'
        self.auth.write_text(json.dumps({'claudeAiOauth':{'accessToken':'TEST-ONLY-PRIVATE','expiresAt':(NOW+3600)*1000}}))
        self.config={'claude_auth':str(self.auth)}

    def response(self):
        return BytesIO(json.dumps({'five_hour':{'utilization':18,'resets_at':'2026-09-09T08:00:00Z'},
          'seven_day':{'utilization':2,'resets_at':'2026-09-16T03:00:00Z'}}).encode())

    def test_oauth_success_is_throttled_and_never_persists_credentials(self):
        with patch('llm_usage.limits.time.time',return_value=NOW),patch('llm_usage.limits.urllib.request.build_opener') as factory:
            factory.return_value.open.return_value=self.response()
            poll_claude(self.store,self.config);poll_claude(self.store,self.config)
            self.assertEqual(factory.return_value.open.call_count,1)
        rows=[r for r in self.store.limits(NOW)['limits'] if r['route']=='claude-code']
        self.assertEqual({r['bucket']:r['remaining'] for r in rows},{'five_hour':82,'seven_day':98})
        self.assertTrue(all(r['source']=='claude-oauth' and r['status']=='fresh' for r in rows))
        with self.store.connect() as c:
            self.assertNotIn('TEST-ONLY-PRIVATE','\n'.join(c.iterdump()))
            self.assertEqual(self.store.state(c,'claude_oauth_poll')['next_attempt'],NOW+300)

    def test_429_persists_cooldown_and_preserves_last_observation(self):
        with self.store.connect() as c:self.store.limit(c,'claude-code','seven_day',40,NOW+3600,NOW-30,'claude-code')
        clock=[NOW]
        with patch('llm_usage.limits.time.time',side_effect=lambda:clock[0]),patch('llm_usage.limits.urllib.request.build_opener') as factory:
            for delay in (600,1200,2400,3600):
                factory.return_value.open.side_effect=HTTPError('https://api.anthropic.com/api/oauth/usage',429,'rate limited',{'Retry-After':'0'},BytesIO(b'PRIVATE ERROR BODY'))
                poll_claude(self.store,self.config)
                with self.store.connect() as c:state=self.store.state(c,'claude_oauth_poll')
                self.assertEqual(state['next_attempt'],clock[0]+delay)
                before=factory.return_value.open.call_count
                poll_claude(self.store,self.config)
                self.assertEqual(factory.return_value.open.call_count,before)
                # Keep the original credential valid while testing hours of cooldown.
                self.auth.write_text(json.dumps({'claudeAiOauth':{'accessToken':'TEST-ONLY-PRIVATE','expiresAt':(NOW+86400)*1000}}))
                clock[0]+=delay
        row=next(r for r in self.store.limits(NOW)['limits'] if r['route']=='claude-code')
        self.assertEqual(row['remaining'],40);self.assertEqual(row['status'],'error')
        self.assertNotIn('PRIVATE ERROR BODY',row['detail'])

    def test_auth_error_requires_credential_change_and_respects_retry_after(self):
        with patch('llm_usage.limits.time.time',return_value=NOW),patch('llm_usage.limits.urllib.request.build_opener') as factory:
            factory.return_value.open.side_effect=HTTPError('url',401,'denied',{},None)
            poll_claude(self.store,self.config);poll_claude(self.store,self.config)
            self.assertEqual(factory.return_value.open.call_count,1)
            stat=self.auth.stat();os.utime(self.auth,ns=(stat.st_atime_ns,stat.st_mtime_ns+1000))
            factory.return_value.open.side_effect=None;factory.return_value.open.return_value=self.response()
            poll_claude(self.store,self.config)
            self.assertEqual(factory.return_value.open.call_count,2)
        self.assertEqual(retry_delay('0',NOW),0)
        self.assertEqual(retry_delay('inf',NOW),0)
        self.assertEqual(retry_delay(format_datetime(datetime.fromtimestamp(NOW+7200,timezone.utc)),NOW),7200)
        self.assertIsNone(NoRedirect().redirect_request(Request('https://api.anthropic.com'),None,302,'',{},'https://elsewhere.test'))

    def test_expired_auth_and_missing_fields_do_not_create_full_quota(self):
        self.auth.write_text(json.dumps({'claudeAiOauth':{'accessToken':'TEST-ONLY-PRIVATE','expiresAt':(NOW-1)*1000}}))
        with patch('llm_usage.limits.time.time',return_value=NOW),patch('llm_usage.limits.urllib.request.build_opener') as factory:
            poll_claude(self.store,self.config);factory.assert_not_called()
        row=next(r for r in self.store.limits(NOW)['limits'] if r['route']=='claude-code')
        self.assertIsNone(row['remaining']);self.assertEqual(row['status'],'error')
        self.auth.write_text(json.dumps({'claudeAiOauth':{'accessToken':'TEST-ONLY-PRIVATE','expiresAt':(NOW+3600)*1000}}))
        with patch('llm_usage.limits.time.time',return_value=NOW),patch('llm_usage.limits.urllib.request.build_opener') as factory:
            factory.return_value.open.return_value=BytesIO(b'{}');poll_claude(self.store,self.config)
        row=next(r for r in self.store.limits(NOW)['limits'] if r['route']=='claude-code')
        self.assertIsNone(row['remaining']);self.assertEqual(row['status'],'unavailable')

    def test_history_uses_one_stream_and_preserves_one_second_reset_jitter(self):
        with patch('llm_usage.store.time.time',return_value=NOW):
            with self.store.connect() as c:
                for i in range(13):
                    ts=NOW-3600+i*300
                    self.store.limit(c,'codex','codex · 10080분',94-2*i,NOW+86400+(i%2),ts-2,'codex-local')
                    self.store.limit(c,'codex','codex · 10080분',94-2*i,NOW+86400+(i%2),ts,'codex')
        row=next(r for r in self.store.limits(NOW)['limits'] if r['route']=='codex')
        self.assertEqual(len(row['history']),13)
        self.assertEqual(sum(p['break_before'] for p in row['history']),1)
        self.assertEqual(row['history_source'],'codex')
        self.assertEqual([r['decrease_pp'] for r in row['trends']],[12,24])
        self.assertEqual(row['trends'][0]['start_remaining'],82)
        self.assertEqual(row['trends'][0]['end_remaining'],70)
        self.assertEqual(row['trends'][0]['per_hour'],24)
        self.assertEqual(sum(r['decrease_pp'] or 0 for r in row['decreases']),24)
        self.assertTrue(any(r['decrease_pp'] is None for r in row['decreases']))

    def test_trends_never_bridge_resets_gaps_or_stale_values(self):
        points=[dict(checked=NOW-900,remaining=20,resets=NOW+1000,break_before=True),
                dict(checked=NOW-600,remaining=10,resets=NOW+1000,break_before=False),
                dict(checked=NOW-300,remaining=100,resets=NOW+2000,break_before=True),
                dict(checked=NOW,remaining=98,resets=NOW+2000,break_before=False)]
        trends=quota_trends(points,NOW,'fresh')
        self.assertEqual(trends[0]['decrease_pp'],2);self.assertEqual(trends[0]['observed_minutes'],5)
        self.assertFalse(quota_trends(points,NOW,'stale')[0]['available'])
        points[-1]['break_before']=True
        self.assertFalse(quota_trends(points,NOW,'fresh')[0]['available'])
        self.assertEqual(sum(r['decrease_pp'] or 0 for r in quota_decreases(points,NOW)),10)
        self.assertFalse(quota_trends([],NOW,'fresh')[0]['available'])

    def test_pace_averages_the_last_hour_of_the_current_segment(self):
        # Providers report integer percentages, so two adjacent samples are
        # either flat or a whole point apart. The trailing-hour average stays
        # at 2%/h here while a two-sample pace would alternate 12%/h and 0.
        points=[dict(checked=NOW-3600+i*300,remaining=90-(2 if i*300>=1800 else 0),
                     resets=NOW+86400,break_before=(i==0),source='codex') for i in range(13)]
        pace=quota_pace(points,NOW,'fresh')
        self.assertEqual(pace,(2.0,60.0))
        # A segment shorter than an hour still reports what was observed.
        short=points[-2:]
        self.assertEqual(quota_pace(short,NOW,'fresh'),(0.0,5.0))

    def test_pace_never_bridges_breaks_stale_or_expired_values(self):
        points=[dict(checked=NOW-600,remaining=20,resets=NOW+1000,break_before=True,source='codex'),
                dict(checked=NOW-300,remaining=15,resets=NOW+1000,break_before=False,source='codex'),
                dict(checked=NOW,remaining=10,resets=NOW+1000,break_before=False,source='codex')]
        self.assertEqual(quota_pace(points,NOW,'fresh'),(60.0,10.0))
        self.assertIsNone(quota_pace(points,NOW,'stale'))
        self.assertIsNone(quota_pace(points,NOW+700,'fresh'))
        points[-1]['resets']=NOW-1
        self.assertIsNone(quota_pace(points,NOW,'fresh'))
        points[-1]['resets']=NOW+1000;points[-1]['break_before']=True
        self.assertIsNone(quota_pace(points,NOW,'fresh'))
        self.assertIsNone(quota_pace([],NOW,'fresh'))


    def fake_app_server(self,reply):
        server=self.root/'fake-codex'
        server.write_text('#!'+sys.executable+'\n'+textwrap.dedent('''
            import json,sys,time
            for line in sys.stdin:
                msg=json.loads(line)
                if msg.get('id')==1:
                    print(json.dumps({'id':1,'result':{}}),flush=True)
                elif msg.get('id')==2:
                    REPLY
            ''').replace('REPLY',reply))
        server.chmod(0o755)
        return str(server)

    def test_codex_reply_sharing_a_read_with_a_notification_is_not_stranded(self):
        server=self.fake_app_server("sys.stdout.write(json.dumps({'method':'account/rateLimits/updated'})+'\\n'"
                                    "+json.dumps({'id':2,'result':{'rateLimits':{'limitId':'codex'}}})+'\\n');sys.stdout.flush()")
        started=time.monotonic()
        self.assertEqual(read_codex(server,timeout=5),{'rateLimits':{'limitId':'codex'}})
        self.assertLess(time.monotonic()-started,4)

    def test_codex_reply_split_across_reads_is_reassembled(self):
        server=self.fake_app_server("reply=json.dumps({'id':2,'result':{'rateLimits':{}}})+'\\n';"
                                    "sys.stdout.write(reply[:10]);sys.stdout.flush();time.sleep(.2);sys.stdout.write(reply[10:]);sys.stdout.flush()")
        self.assertEqual(read_codex(server,timeout=5),{'rateLimits':{}})

    def test_opencode_go_credentials_never_follow_redirects(self):
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path=='/usage':
                    self.send_response(302);self.send_header('Location','/elsewhere');self.end_headers()
                else:
                    seen.append(self.headers.get('Authorization'))
                    self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
                    self.wfile.write(b'{"usage":{}}')
            def log_message(self,*args):pass
        server=HTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        auth=self.root/'opencode.json';auth.write_text(json.dumps({'opencode-go':{'type':'api','key':'TEST-ONLY-PRIVATE'}}))
        url=f'http://127.0.0.1:{server.server_port}/usage'
        with patch('llm_usage.limits.OPENCODE_GO_USAGE_URL',url),patch('llm_usage.limits.poll_claude'),\
             patch('llm_usage.limits.poll_devin'),patch('llm_usage.limits.read_codex',side_effect=OSError('offline')):
            poll_limits(self.store,{'opencode_auth':str(auth),'codex_binary':'codex'})
        self.assertEqual(seen,[])
        with self.store.connect() as c:
            row=c.execute("SELECT status,detail FROM sources WHERE name='opencode-go'").fetchone()
        self.assertEqual((row['status'],row['detail']),('error','한도 수집 실패 (HTTP 302)'))

    def test_entitlement_error_backs_off_but_manual_still_polls(self):
        calls=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(1)
                self.send_response(403);self.send_header('Content-Type','application/json');self.end_headers()
                self.wfile.write(b'{"error":{"type":"EntitlementError"}}')
            def log_message(self,*args):pass
        server=HTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        auth=self.root/'opencode.json';auth.write_text(json.dumps({'opencode-go':{'type':'api','key':'TEST-ONLY-PRIVATE'}}))
        url=f'http://127.0.0.1:{server.server_port}/usage'
        settings={'opencode_auth':str(auth),'codex_binary':'codex'}
        with patch('llm_usage.limits.OPENCODE_GO_USAGE_URL',url),patch('llm_usage.limits.poll_claude'),\
             patch('llm_usage.limits.poll_devin'),patch('llm_usage.limits.read_codex',side_effect=OSError('offline')):
            poll_limits(self.store,settings)
            self.assertEqual(len(calls),1)
            with self.store.connect() as c:
                row=c.execute("SELECT status FROM sources WHERE name='opencode-go'").fetchone()
                until=self.store.state(c,'opencode_go_poll')['next_attempt']
            self.assertEqual(row['status'],'ended')
            self.assertGreater(until,time.time()+5*3600)
            poll_limits(self.store,settings)
            self.assertEqual(len(calls),1)
            poll_limits(self.store,settings,manual=True)
            self.assertEqual(len(calls),2)

    def test_devin_ide_prefers_newest_installed_version(self):
        from llm_usage.limits import devin_ide
        root=self.root/'devin'
        for version in ('3000.10.27','3000.11.3','not-a-version'):
            (root/'cli'/'_versions'/version).mkdir(parents=True)
        self.assertEqual(devin_ide(str(root/'credentials.toml'))['ide_version'],'3000.11.3')
        self.assertEqual(devin_ide(str(self.root/'missing'/'credentials.toml'))['ide_version'],'3000.10.27')
        self.assertEqual(devin_ide()['ide_version'],'3000.10.27')

    def test_limits_history_payload_is_last_24_hours_of_display_fields(self):
        with patch('llm_usage.store.time.time',return_value=NOW):
            with self.store.connect() as c:
                for hours in (30,23,1):
                    self.store.limit(c,'codex','codex · 10080분',90-hours,NOW+86400,NOW-hours*3600,'codex')
        row=next(r for r in self.store.limits(NOW)['limits'] if r['bucket']=='codex · 10080분')
        self.assertEqual([round((NOW-p['checked'])/3600) for p in row['history']],[23,1])
        self.assertEqual(set(row['history'][0]),{'checked','remaining','resets','break_before','break_reason'})
        self.assertEqual(row['history_source'],'codex')

    @staticmethod
    def agy(*models):
        return {'userStatus':{'cascadeModelConfigData':{'clientModelConfigs':list(models)}}}

    @staticmethod
    def no_summary(_):
        raise ConnectionError('summary unsupported')

    @staticmethod
    def iso(ts):
        return datetime.fromtimestamp(ts,timezone.utc).isoformat().replace('+00:00','Z')

    def agy_rows(self,c):
        return {r['bucket']:(r['remaining'],r['resets'],r['source']) for r in c.execute("SELECT * FROM limits WHERE route='antigravity'")}

    def test_antigravity_app_quota_groups_and_proto3_omitted_zero(self):
        from llm_usage.limits import agy_app_limits
        payload=self.agy(
            {'modelId':'gemini-3.8-flash','quotaInfo':{'remainingFraction':0.95,'resetTime':'2026-09-09T07:00:00Z'}},
            {'modelId':'gemini-3.6-flash','quotaInfo':{'remainingFraction':0.95,'resetTime':'2026-09-09T07:00:00Z'}},
            # QuotaInfo.remaining_fraction has no presence: protojson omits exactly 0.0.
            {'modelId':'claude-opus-5-5','quotaInfo':{'resetTime':'2026-09-16T03:00:00Z'}},
            {'modelId':'claude-sonnet-5-5','quotaInfo':{'resetTime':'2026-09-16T03:00:00Z'}},
            {'modelId':'custom-unlimited'})
        with self.store.connect() as c:
            self.store.limit(c,'antigravity','app-gemini',50.0,NOW+1000,NOW,'antigravity-app')
            self.store.save_state(c,'label:antigravity:3p-5h','3rd party')
            self.assertEqual(agy_app_limits(self.store,c,payload,NOW),(2,0,False))
            rows=self.agy_rows(c)
            labels=c.execute("SELECT COUNT(*) FROM state WHERE key LIKE 'label:antigravity:%'").fetchone()[0]
        # Only the windows the response described; no 5h value is derived from the weekly one.
        self.assertEqual(rows,{'gemini-5h':(95.0,NOW+4*3600,'antigravity-app'),
                               '3p-weekly':(0.0,stamp('2026-09-16T03:00:00Z'),'antigravity-app')})
        self.assertEqual(labels,0)

    def test_antigravity_app_window_follows_reset_not_a_fixed_horizon(self):
        from llm_usage.limits import agy_app_limits
        weekly=NOW+3*3600
        with self.store.connect() as c:
            # The family's weekly reset is already known (status line or an earlier poll).
            self.store.limit(c,'antigravity','3p-weekly',4.0,weekly+2,NOW-3600,'antigravity')
            agy_app_limits(self.store,c,self.agy(
                {'modelId':'claude-opus-5-5','quotaInfo':{'remainingFraction':0.02,'resetTime':self.iso(weekly)}},
                {'modelId':'gemini-3.8-flash','quotaInfo':{'remainingFraction':0.5,'resetTime':self.iso(NOW+30*3600)}}),NOW)
            rows=self.agy_rows(c)
        # Three hours before the weekly reset is still the weekly window, and a reset
        # 30 hours away cannot be a 5h window.
        self.assertEqual(rows['3p-weekly'][:2],(2.0,weekly))
        self.assertEqual(rows['gemini-weekly'][:2],(50.0,NOW+30*3600))
        self.assertNotIn('3p-5h',rows);self.assertNotIn('gemini-5h',rows)
        with self.store.connect() as c:
            # A near reset that is not the known weekly one is the 5h window.
            agy_app_limits(self.store,c,self.agy(
                {'modelId':'claude-opus-5-5','quotaInfo':{'remainingFraction':1,'resetTime':self.iso(NOW+5*3600)}}),NOW+60)
            rows=self.agy_rows(c)
        self.assertEqual(rows['3p-5h'][:2],(100.0,NOW+5*3600))
        self.assertEqual(rows['3p-weekly'][:2],(2.0,weekly))

    def test_antigravity_app_skips_unusable_quota_and_drops_stale_split(self):
        from llm_usage.limits import agy_app_limits
        with self.store.connect() as c:
            self.store.limit(c,'antigravity','gemini-5h-2',40.0,NOW+3600,NOW-600,'antigravity-app')
            self.store.limit(c,'antigravity','gemini-weekly-2',40.0,NOW+86400,NOW-600,'antigravity')
            result=agy_app_limits(self.store,c,self.agy(
                {'modelId':'gemini-3.8-flash','quotaInfo':{'remainingFraction':0.5,'resetTime':self.iso(NOW+3600)}},
                {'modelId':'gemini-expired','quotaInfo':{'remainingFraction':0.5,'resetTime':self.iso(NOW-60)}},
                {'modelId':'gemini-no-reset','quotaInfo':{'remainingFraction':0.5}},
                {'modelId':'gemini-bad-reset','quotaInfo':{'remainingFraction':0.5,'resetTime':'not-a-time'}},
                {'modelId':'claude-empty','quotaInfo':{}}),NOW)
            rows=self.agy_rows(c)
        self.assertEqual(result,(1,4,False))
        self.assertEqual(set(rows),{'gemini-5h','gemini-weekly-2'})

    def test_shorter_window_is_blocked_only_by_a_current_exhausted_parent(self):
        with self.store.connect() as c:
            self.store.limit(c,'antigravity','3p-5h',100.0,NOW+3600,NOW-60,'antigravity-app')
            self.store.limit(c,'antigravity','3p-weekly',0.0,NOW+86400*6,NOW-120,'antigravity-app')
            self.store.limit(c,'claude-code','five_hour',80.0,NOW+3600,NOW,'claude-oauth')
            self.store.limit(c,'claude-code','seven_day',0.0,NOW+86400*6,NOW,'claude-oauth')
            self.store.limit(c,'devin','daily',50.0,NOW+3600,NOW,'devin')
            self.store.limit(c,'devin','weekly',30.0,NOW+86400,NOW,'devin')
        rows={(r['route'],r['bucket']):r for r in self.store.limits(NOW)['limits']}
        blocked=rows[('antigravity','3p-5h')]
        # The lower window keeps its own observed value; only the block is reported.
        self.assertEqual((blocked['remaining'],blocked['status']),(100.0,'fresh'))
        self.assertEqual(blocked['blocked_by'],{'bucket':'3p-weekly','resets':NOW+86400*6,'seconds_to_reset':86400*6})
        self.assertEqual(rows[('claude-code','five_hour')]['blocked_by']['bucket'],'seven_day')
        self.assertIsNone(rows[('devin','daily')]['blocked_by'])
        self.assertIsNone(rows[('antigravity','3p-weekly')]['blocked_by'])
        # A weekly 0% observed long ago, or one whose reset has passed, blocks nothing.
        for checked,resets in ((NOW-3600,NOW+86400),(NOW-120,NOW-60)):
            with self.store.connect() as c:
                c.execute("UPDATE limits SET checked=?,resets=? WHERE bucket='3p-weekly'",(checked,resets))
            row=next(r for r in self.store.limits(NOW)['limits'] if r['bucket']=='3p-5h')
            self.assertEqual((row['remaining'],row['blocked_by']),(100.0,None))

    def test_antigravity_app_split_buckets_and_missing_server(self):
        from llm_usage.limits import poll_antigravity_app
        poll_antigravity_app(self.store,{},servers=[])
        with self.store.connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM sources WHERE name='antigravity-app'").fetchone())
        sample=self.agy(
            {'modelId':'gemini-flash','quotaInfo':{'remainingFraction':0.8,'resetTime':self.iso(NOW+3600)}},
            {'modelId':'gemini-pro','quotaInfo':{'remainingFraction':0.5,'resetTime':self.iso(NOW+7200)}})
        with patch('llm_usage.limits.time.time',return_value=NOW):
            poll_antigravity_app(self.store,{},servers=[(1234,'TEST-TOKEN-PRIVATE')],reader=lambda _:sample,
                                 quota_reader=self.no_summary)
        with self.store.connect() as c:
            source=dict(c.execute("SELECT * FROM sources WHERE name='antigravity-app'").fetchone())
            buckets={r[0] for r in c.execute("SELECT bucket FROM limits WHERE route='antigravity'")}
            dump='\n'.join(c.iterdump())
        self.assertEqual(source['status'],'partial')
        self.assertIn('서로 다른 한도',source['detail'])
        self.assertEqual(buckets,{'gemini-5h','gemini-5h-2'})
        self.assertNotIn('TEST-TOKEN-PRIVATE',dump)
        # Disabled polling neither reads nor touches the recorded state.
        poll_antigravity_app(self.store,{'antigravity_app_quota':False},servers=[(1234,'tok')],reader=lambda _:1/0)
        with self.store.connect() as c:
            self.assertEqual(dict(c.execute("SELECT * FROM sources WHERE name='antigravity-app'").fetchone()),source)

    def test_antigravity_quota_summary_reports_every_window(self):
        from llm_usage.limits import poll_antigravity_app
        summary={'response':{'groups':[
            {'displayName':'Gemini Models','buckets':[
                {'bucketId':'gemini-weekly','window':'weekly','remainingFraction':0.9,'resetTime':self.iso(NOW+6*86400)},
                {'bucketId':'gemini-5h','window':'5h','remainingFraction':0.5,'resetTime':self.iso(NOW+3600)}]},
            {'displayName':'Claude and GPT models','buckets':[
                {'bucketId':'3p-weekly','window':'weekly','remainingFraction':0.3,'resetTime':self.iso(NOW+7*86400)},
                # remaining_fraction has no proto3 presence: omitted with a reset means exhausted.
                {'bucketId':'3p-5h','window':'5h','resetTime':self.iso(NOW+2*3600)}]}]}}
        with patch('llm_usage.limits.time.time',return_value=NOW):
            poll_antigravity_app(self.store,{},servers=[(1234,'TEST-TOKEN-PRIVATE')],
                                 reader=lambda _:self.fail('GetUserStatus must not run when the summary answers'),
                                 quota_reader=lambda _:summary)
        with self.store.connect() as c:
            rows=self.agy_rows(c)
            source=dict(c.execute("SELECT status,detail FROM sources WHERE name='antigravity-app'").fetchone())
            dump='\n'.join(c.iterdump())
        self.assertEqual(rows,{'gemini-weekly':(90.0,NOW+6*86400,'antigravity-app'),
                               'gemini-5h':(50.0,NOW+3600,'antigravity-app'),
                               '3p-weekly':(30.0,NOW+7*86400,'antigravity-app'),
                               '3p-5h':(0.0,NOW+2*3600,'antigravity-app')})
        self.assertEqual((source['status'],source['detail']),
                         ('ok','WSL 데스크톱 앱 한도 요약 조회 · 내부 경로 · 응답의 창 레이블 사용'))
        self.assertNotIn('TEST-TOKEN-PRIVATE',dump)

    def test_antigravity_quota_summary_skips_unusable_and_drops_leftover_splits(self):
        from llm_usage.limits import agy_quota_limits
        payload={'groups':[{'buckets':[  # the unwrapped shape parses as well
            {'bucketId':'gemini-5h','remainingFraction':0.5,'resetTime':self.iso(NOW+3600)},
            {'bucketId':'gemini-weekly','remainingFraction':0.9},
            {'bucketId':'bad id','remainingFraction':0.5,'resetTime':self.iso(NOW+60)},
            {'bucketId':'3p-5h','remainingFraction':2,'resetTime':self.iso(NOW+60)},
            {'bucketId':'3p-weekly','remainingFraction':0.5,'resetTime':'not-a-time'},
            {'bucketId':'gemini-5h-2','remainingFraction':0.5,'resetTime':self.iso(NOW-60)},
            'junk']}]}
        with self.store.connect() as c:
            self.store.limit(c,'antigravity','3p-5h-2',40.0,NOW+3600,NOW-600,'antigravity-app')
            self.store.limit(c,'antigravity','gemini-weekly-2',40.0,NOW+86400,NOW-600,'antigravity')
            self.assertEqual(agy_quota_limits(self.store,c,payload,NOW),(1,6))
            rows=self.agy_rows(c)
        # The app's leftover split is gone; the status-line copy is not this poll's to drop.
        self.assertEqual(set(rows),{'gemini-5h','gemini-weekly-2'})

    def test_antigravity_quota_summary_failure_falls_back_to_per_model_read(self):
        from llm_usage.limits import poll_antigravity_app
        sample=self.agy({'modelId':'claude-opus-5-5','quotaInfo':{'remainingFraction':0.4,'resetTime':self.iso(NOW+5*3600)}})
        seen=[]
        with patch('llm_usage.limits.time.time',return_value=NOW):
            for quota_reader in (lambda _:{'response':{'groups':[]}},self.no_summary):
                poll_antigravity_app(self.store,{},servers=[(1,'t')],quota_reader=quota_reader,
                                     reader=lambda _:seen.append(1) or sample)
        self.assertEqual(len(seen),2)
        with self.store.connect() as c:
            rows=self.agy_rows(c)
            detail=c.execute("SELECT detail FROM sources WHERE name='antigravity-app'").fetchone()[0]
        self.assertEqual(rows['3p-5h'][:2],(40.0,NOW+5*3600))
        self.assertIn('창 길이는 초기화 시각으로 판단',detail)

    def test_antigravity_app_discovery_failure_does_not_stop_other_polls(self):
        from llm_usage import limits
        with patch.object(limits,'agy_app_servers',side_effect=PermissionError('/proc')):
            limits.poll_antigravity_app(self.store,{})
        with self.store.connect() as c:
            source=dict(c.execute("SELECT status,detail FROM sources WHERE name='antigravity-app'").fetchone())
        self.assertEqual(source['status'],'error');self.assertIn('PermissionError',source['detail'])
        called=[]
        with patch.object(limits,'agy_app_servers',side_effect=OSError),\
             patch.object(limits,'poll_claude'),patch.object(limits,'poll_devin'),\
             patch.object(limits,'read_codex',side_effect=lambda _:called.append('codex') or {}),\
             patch.object(limits,'codex_limits',return_value=0):
            limits.poll_limits(self.store,{'codex_binary':'codex','opencode_auth':str(self.root/'missing.json')})
        self.assertEqual(called,['codex'])

    def test_antigravity_app_reader_skips_silent_ports_and_remembers_the_answer(self):
        from llm_usage import limits
        limits._agy_endpoints.clear();self.addCleanup(limits._agy_endpoints.clear)
        opened=[]
        class Response(BytesIO):
            def __enter__(self):return self
            def __exit__(self,*exc):self.close()
        def opener(*handlers):
            class Opener:
                def open(self,request,timeout):
                    opened.append(request.full_url);return Response(b'{"userStatus":{}}')
            return Opener()
        probes=[]
        def ready(host,port,context,timeout):
            probes.append(port);return port==3
        with patch.object(limits,'_listening_ports',return_value=[('127.0.0.1',1),('127.0.0.1',2),('::1',3)]),\
             patch.object(limits,'_tls_ready',side_effect=ready),\
             patch.object(limits.urllib.request,'build_opener',side_effect=opener):
            self.assertEqual(limits.read_agy_app([(77,'tok')]),{'userStatus':{}})
            self.assertEqual(limits.read_agy_app([(77,'tok')]),{'userStatus':{}})
        # Silent ports cost one short probe once; the answering endpoint is reused first.
        self.assertEqual(probes,[1,2,3])
        self.assertEqual([u.split('/exa.')[0] for u in opened],['https://[::1]:3']*2)
        self.assertEqual(limits._agy_endpoints,{77:('::1',3)})


if __name__=='__main__':unittest.main()

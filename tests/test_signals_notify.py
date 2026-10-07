"""Quota events, capacity, usage spikes, external notifications and their web endpoints."""
import json
from pathlib import Path
import socket
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from llm_usage import notify
from llm_usage.signals import capacity, quota_events, usage_spike
from llm_usage.store import Store, stamp
from llm_usage.webapp import create_app

NOW=stamp('2026-10-03T09:00:00Z')
CHANNELS=dict(ntfy_url='https://ntfy.example/secret-topic',webhook_url='https://hooks.example/abc/SECRET',
              telegram_token='BOT-SECRET',telegram_chat='42')


class Response:
    def __enter__(self):return self
    def __exit__(self,*exc):return False
    def read(self,n):return b''


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'usage.db')

    def test_exhaustion_and_recovery_events_from_one_source(self):
        with patch('llm_usage.store.time.time',return_value=NOW),self.store.connect() as c:
            for minutes,remaining,resets in ((300,30,NOW+100),(240,0,NOW+100),(230,0,NOW+100),(200,100,NOW+18000),(100,0,NOW+18000)):
                self.store.limit(c,'claude-code','five_hour',remaining,resets,NOW-minutes*60,'claude-oauth')
            self.store.limit(c,'claude-code','five_hour',0,NOW+100,NOW-50*60,'claude-code')
            events=quota_events(c,'claude-code','five_hour','claude-oauth',NOW)
        self.assertEqual([e['kind'] for e in events['recent']],['exhausted','recovered','exhausted'])
        self.assertEqual((events['exhausted'],events['last_exhausted']),(2,NOW-100*60))

    def test_capacity_needs_a_used_open_window(self):
        row=dict(status='fresh',remaining=80.0,plan={'ahead':0},window=dict(open=True,tokens=2_000_000,requests=40))
        self.assertEqual(capacity(row),dict(tokens_per_point=100_000,remaining_tokens=8_000_000,remaining_requests=160,used_points=20))
        self.assertIsNone(capacity({**row,'remaining':97.0}))
        self.assertIsNone(capacity({**row,'plan':None}))
        self.assertIsNone(capacity({**row,'window':{**row['window'],'open':False}}))

    def test_spike_against_this_accounts_usual_hours(self):
        hour=int(NOW//3600)
        with self.store.connect() as c:
            for h in range(hour-24*7,hour-1):
                self.store.event(c,f'e{h}',h*3600+60,'OpenAI','codex','m',dict(cached_input=1_000_000+(h%5)*100_000))
            self.assertIsNone(usage_spike(c,NOW))
            self.store.event(c,'burst',hour*3600+60,'OpenAI','codex','m',dict(cached_input=40_000_000))
            spike=usage_spike(c,NOW)
        self.assertEqual((spike['hour_start'],spike['current']),(hour*3600,True))
        self.assertGreater(spike['ratio'],1)
        with tempfile.TemporaryDirectory() as folder:
            fresh=Store(Path(folder)/'u.db')
            with fresh.connect() as c:
                fresh.event(c,'x',NOW-60,'OpenAI','codex','m',dict(cached_input=90_000_000))
                self.assertIsNone(usage_spike(c,NOW))  # too little history to judge


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'usage.db')

    def limits(self,remaining,resets=NOW+3600,status='fresh'):
        return dict(low_percent=15,limits=[dict(route='claude-code',bucket='five_hour',remaining=remaining,resets=resets,status=status)])

    def run_eval(self,limits,config=None,now=NOW,**kw):
        with patch('llm_usage.notify.urllib.request.urlopen',side_effect=lambda r,timeout:(self.sent.append((r.full_url,json.loads(r.data))),Response())[1]):
            return notify.evaluate(self.store,config if config is not None else {'ntfy_url':CHANNELS['ntfy_url']},now,limits,**kw)

    def test_transitions_are_sent_once_and_first_sight_only_records(self):
        self.sent=[]
        self.assertEqual(self.run_eval(self.limits(50)),[])
        self.assertEqual(self.run_eval(self.limits(40)),[])
        self.assertEqual([m[0] for m in self.run_eval(self.limits(12))],['low'])
        self.assertEqual(self.run_eval(self.limits(10)),[])
        self.assertEqual([m[0] for m in self.run_eval(self.limits(0))],['exhausted'])
        self.assertEqual([m[0] for m in self.run_eval(self.limits(100,resets=NOW+18000))],['recovered'])
        url,payload=self.sent[0]
        self.assertEqual(url,'https://ntfy.example')
        self.assertEqual((payload['topic'],payload['title']),('secret-topic','Claude 5시간 잔여 12.0%'))
        # A stale row neither sends nor resets the remembered state.
        self.assertEqual(self.run_eval(self.limits(5,status='stale')),[])

    def test_quiet_hours_disabled_events_and_blocked_windows(self):
        self.sent=[]
        self.run_eval(self.limits(50))
        quiet={'ntfy_url':CHANNELS['ntfy_url'],'quiet':[17,19]}  # 18:00 KST
        self.assertEqual(self.run_eval(self.limits(10),quiet),[]);self.assertEqual(self.sent,[])
        self.run_eval(self.limits(50))
        off={'ntfy_url':CHANNELS['ntfy_url'],'events':{'low':False}}
        self.assertEqual(self.run_eval(self.limits(10),off),[])
        blocked=self.limits(50);blocked['limits'][0]['blocked_by']={'bucket':'seven_day'}
        self.assertEqual(self.run_eval(blocked),[])
        self.assertTrue(notify.quiet_now({'quiet':[23,7]},stamp('2026-10-03T16:30:00Z')))  # 01:30 KST
        self.assertFalse(notify.quiet_now({'quiet':[23,7]},NOW))

    def test_channel_payloads_and_secret_free_errors(self):
        captured=[]
        def opener(request,timeout):
            captured.append((request.full_url,json.loads(request.data)))
            if 'telegram' in request.full_url:raise OSError('boom BOT-SECRET')
            return Response()
        with patch('llm_usage.notify.urllib.request.urlopen',side_effect=opener):
            errors=notify.send(CHANNELS,'제목','본문')
        self.assertEqual([u for u,_ in captured],['https://ntfy.example','https://hooks.example/abc/SECRET','https://api.telegram.org/botBOT-SECRET/sendMessage'])
        self.assertEqual(captured[1][1],{'content':'**제목**\n본문','text':'**제목**\n본문'})
        self.assertEqual(captured[2][1],{'chat_id':'42','text':'제목\n본문'})
        self.assertEqual(errors,{'telegram':'OSError'})
        self.assertEqual(notify.masked(CHANNELS)['channels'],{'ntfy':'ntfy.example','webhook':'hooks.example','telegram':'설정됨'})
        self.assertNotIn('SECRET',json.dumps(notify.masked(CHANNELS)))

    def test_delivery_outcome_is_a_source_without_secrets(self):
        self.sent=[]
        self.run_eval(self.limits(50))
        with patch('llm_usage.notify.urllib.request.urlopen',side_effect=OSError('down')):
            notify.evaluate(self.store,{'webhook_url':CHANNELS['webhook_url']},NOW,self.limits(10))
        with self.store.connect() as c:
            row=dict(c.execute("SELECT status,detail FROM sources WHERE name='외부 알림'").fetchone())
            dump='\n'.join(c.iterdump())
        self.assertEqual(row,{'status':'error','detail':'webhook OSError'})
        self.assertNotIn('SECRET',dump)

    def test_persistent_source_failure_and_collector_gap(self):
        self.sent=[]
        failing=[dict(name='codex',status='error',detail='HTTP 500')]
        self.assertEqual(self.run_eval(None,sources=failing),[])
        self.assertEqual([m[0] for m in self.run_eval(None,now=NOW+31*60,sources=failing)],['source'])
        self.assertEqual(self.run_eval(None,now=NOW+40*60,sources=failing),[])
        self.run_eval(None,now=NOW+41*60,sources=[dict(name='codex',status='ok')])
        self.assertEqual(self.run_eval(None,now=NOW+80*60,sources=failing),[])
        with self.store.connect() as c:self.store.save_state(c,'collector',dict(status='idle',checked=NOW-25*60))
        with patch('llm_usage.notify.urllib.request.urlopen',side_effect=lambda r,timeout:Response()):
            sent=notify.collector_started(self.store,{'ntfy_url':CHANNELS['ntfy_url']},NOW)
        self.assertIn('약 25분',sent[0][2])

    def test_spike_is_sent_once_per_hour(self):
        self.sent=[]
        spike=dict(hour_start=NOW-NOW%3600,tokens=60e6,ratio=3.2)
        self.assertEqual([m[0] for m in self.run_eval(None,spike=spike)],['spike'])
        self.assertEqual(self.run_eval(None,spike=spike),[])


class WebSignalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'usage.db')
        self.origin='https://pc.example.ts.net:9444'
        (self.root/'config.json').write_text('{}')
        (self.root/'pricing.json').write_text(json.dumps({'hidden-model':None}))
        self.app=create_app(dict(database=str(self.root/'usage.db'),origin=self.origin,secret_key='t',allowed_logins=['owner'],
                                 config_file=str(self.root/'config.json'),pricing_file=str(self.root/'pricing.json')))
        self.client=self.app.test_client()
        self.env={'REMOTE_ADDR':'','gunicorn.socket':SimpleNamespace(family=socket.AF_UNIX)}
        self.headers={'Tailscale-User-Login':'owner'}

    def get(self,path):return self.client.get(path,base_url=self.origin,headers=self.headers,environ_overrides=self.env)

    def post(self,path,body):
        token=self.get('/api/bootstrap').json['csrf']
        return self.client.post(path,json=body,base_url=self.origin,environ_overrides=self.env,
                                headers={**self.headers,'Origin':self.origin,'X-CSRF-Token':token})

    def test_unpriced_models_exclude_hidden_and_internal(self):
        now=time.time()
        with self.store.connect() as c:
            for model in ('brand-new-model','hidden-model','compactor','gpt-reserve','unknown','gpt-6-sol'):
                self.store.event(c,model,now-3600,'OpenAI','codex',model,dict(uncached_input=1000),session='s1',project='p')
        data=self.get('/api/usage?period=7d').get_json()
        self.assertEqual(data['unpriced_models'],['brand-new-model'])
        detail=self.get('/api/session?id=s1').get_json()
        self.assertEqual((detail['requests'],detail['project'],len(detail['models'])),(6,'p',6))
        self.assertEqual(len(detail['hours']),1)
        self.assertEqual(self.get('/api/session?id=bad%20id').status_code,400)
        self.assertEqual(self.get('/api/session?id=none').get_json()['models'],[])

    def test_notify_settings_keep_secrets_server_side(self):
        r=self.post('/api/config/notify',{'ntfy_url':CHANNELS['ntfy_url'],'quiet':[23,7],'events':{'spike':False}})
        self.assertEqual(r.status_code,200)
        self.assertNotIn('secret-topic',r.get_data(as_text=True))
        config=self.get('/api/config').get_json()
        self.assertEqual(config['notify']['channels']['ntfy'],'ntfy.example')
        self.assertFalse(config['notify']['events']['spike'])
        self.assertNotIn('secret-topic',json.dumps(config))
        local=json.loads((self.root/'local.json').read_text())
        self.assertEqual(local['notify']['ntfy_url'],CHANNELS['ntfy_url'])
        # Omitted keys keep the stored secret; an empty string clears it.
        self.post('/api/config/notify',{'quiet':None})
        self.assertEqual(json.loads((self.root/'local.json').read_text())['notify']['ntfy_url'],CHANNELS['ntfy_url'])
        self.assertEqual(self.post('/api/config/notify',{'webhook_url':'http://plain'}).status_code,400)
        self.assertEqual(self.post('/api/config/notify',{'quiet':[5,5]}).status_code,400)
        with patch('llm_usage.notify.urllib.request.urlopen',side_effect=lambda r,timeout:Response()):
            self.assertEqual(self.post('/api/notify/test',{}).get_json(),{'channels':['ntfy'],'errors':{}})
        with patch('llm_usage.notify.urllib.request.urlopen',side_effect=OSError('down')):
            failed=self.post('/api/notify/test',{})
        self.assertEqual((failed.status_code,failed.get_json()['error']),(502,'전송 실패: ntfy OSError'))
        self.post('/api/config/notify',{'ntfy_url':''})
        self.assertEqual(self.post('/api/notify/test',{}).status_code,400)


if __name__=='__main__':unittest.main()

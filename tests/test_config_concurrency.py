"""Settings commits serialize whole read/merge/write operations, including processes."""
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from llm_usage import config
from llm_usage.webapp import create_app


def process_write(directory,field,value,barrier):
    original=config.atomic_json
    def delayed(path,data):
        try:barrier.wait(timeout=.3)
        except threading.BrokenBarrierError:pass
        original(path,data)
    with patch.object(config,'atomic_json',delayed):
        config.update_local({field:value},{'database':str(Path(directory)/'usage.db')})


class ConfigConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.origin='https://fixture.example'
        self.cfg=dict(database=str(self.root/'usage.db'),config_file=str(self.root/'config.json'),
                      origin=self.origin,secret_key='fixture',allowed_logins=['owner'],refresh_seconds=300,
                      notify={'webhook_url':'https://hooks.example/SECRET','events':{'low':True}})
        self.app=create_app(self.cfg)

    def client(self):
        client=self.app.test_client()
        env={'gunicorn.socket':SimpleNamespace(family=socket.AF_UNIX),'REMOTE_ADDR':''}
        identity={'Tailscale-User-Login':'owner'}
        token=client.get('/api/bootstrap',base_url=self.origin,headers=identity,environ_overrides=env).json['csrf']
        headers={**identity,'Origin':self.origin,'X-CSRF-Token':token}
        return lambda path,body:client.post(path,json=body,base_url=self.origin,headers=headers,environ_overrides=env)

    def simultaneous(self,left,right):
        clients=[self.client(),self.client()];barrier=threading.Barrier(2);original=config.atomic_json
        def delayed(path,data):
            try:barrier.wait(timeout=.2)
            except threading.BrokenBarrierError:pass
            original(path,data)
        with patch.object(config,'atomic_json',delayed),ThreadPoolExecutor(2) as pool:
            futures=[pool.submit(client,*request) for client,request in zip(clients,[left,right])]
            self.assertEqual([f.result(timeout=5).status_code for f in futures],[200,200])
        return json.loads((self.root/'local.json').read_text())

    def test_parallel_different_fields_survive_reopening(self):
        saved=self.simultaneous(('/api/config/refresh',{'refresh_seconds':60}),
                                ('/api/config/value-alert',{'value_alert_usd':100}))
        self.assertEqual(saved,{'refresh_seconds':60,'value_alert_usd':100})
        self.assertEqual(config.with_local({'database':self.cfg['database']})['value_alert_usd'],100)
        self.assertEqual(self.cfg['refresh_seconds'],60)

    def test_parallel_nested_notify_patches_keep_omitted_secrets(self):
        saved=self.simultaneous(('/api/config/notify',{'events':{'low':False}}),
                                ('/api/config/notify',{'quiet':[23,7]}))['notify']
        self.assertEqual(saved,{'events':{'low':False},'quiet':[23,7],'webhook_url':'https://hooks.example/SECRET'})
        self.assertEqual(self.cfg['notify'],saved)

    def test_parallel_threshold_fields_survive(self):
        saved=self.simultaneous(('/api/config/thresholds',{'low_percent':20}),
                                ('/api/config/thresholds',{'stale_seconds':900}))['thresholds']
        self.assertEqual(saved['low_percent'],20);self.assertEqual(saved['stale_seconds'],900)

    def test_same_field_commit_keeps_file_and_memory_consistent(self):
        saved=self.simultaneous(('/api/config/refresh',{'refresh_seconds':60}),
                                ('/api/config/refresh',{'refresh_seconds':120}))
        self.assertIn(saved['refresh_seconds'],(60,120))
        self.assertEqual(self.cfg['refresh_seconds'],saved['refresh_seconds'])

    def test_stale_app_merges_notify_against_latest_file(self):
        stale=create_app(dict(self.cfg))
        self.assertEqual(self.client()('/api/config/notify',{'webhook_url':''}).status_code,200)
        self.app=stale
        self.assertEqual(self.client()('/api/config/notify',{'quiet':[23,7]}).status_code,200)
        saved=json.loads((self.root/'local.json').read_text())['notify']
        self.assertNotIn('webhook_url',saved);self.assertEqual(saved['quiet'],[23,7])

    def test_failed_commit_does_not_publish_memory_or_secret_error(self):
        (self.root/'local.json').write_text('{"refresh_seconds":300}')
        client=self.client()
        with patch.object(config,'atomic_json',side_effect=OSError('SECRET disk path')):
            response=client('/api/config/refresh',{'refresh_seconds':60})
        self.assertEqual(response.status_code,500)
        self.assertNotIn('SECRET',response.get_data(as_text=True))
        self.assertEqual(self.cfg['refresh_seconds'],300)
        self.assertEqual(json.loads((self.root/'local.json').read_text()),{'refresh_seconds':300})

    def test_corrupt_file_is_not_silently_replaced(self):
        path=self.root/'local.json';path.write_text('{broken')
        self.assertEqual(self.client()('/api/config/refresh',{'refresh_seconds':60}).status_code,500)
        self.assertEqual(path.read_text(),'{broken');self.assertEqual(self.cfg['refresh_seconds'],300)

    def test_process_writers_share_the_same_lock(self):
        ctx=multiprocessing.get_context('spawn');barrier=ctx.Barrier(2)
        processes=[ctx.Process(target=process_write,args=(self.tmp.name,k,v,barrier))
                   for k,v in [('refresh_seconds',60),('value_alert_usd',100)]]
        try:
            for p in processes:p.start()
            for p in processes:p.join(10);self.assertEqual(p.exitcode,0)
            self.assertEqual(json.loads((self.root/'local.json').read_text()),{'refresh_seconds':60,'value_alert_usd':100})
        finally:
            for p in processes:
                if p.is_alive():p.terminate();p.join(5)

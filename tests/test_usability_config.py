import socket
import tempfile
import unittest
from pathlib import Path
from llm_usage.store import Store
from llm_usage.webapp import create_app

class Sock:
    family=socket.AF_UNIX

class SubscriptionRouteTests(unittest.TestCase):
    def test_supported_observed_and_configured_routes_survive_unset_prices(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);store=Store(root/'usage.db')
            with store.connect() as c:
                store.event(c,'example',1,'Example','custom-observed','model',dict(output=1))
            config=dict(database=str(root/'usage.db'),origin='https://dashboard.test',secret_key='synthetic'*4,
                        allowed_logins=['fixture'],pricing_file=str(root/'pricing.json'),
                        subscription_prices={'custom-configured':5})
            client=create_app(config).test_client()
            def get():
                return client.get('/api/config',headers={'Host':'dashboard.test','Tailscale-User-Login':'fixture'},
                                  environ_base={'gunicorn.socket':Sock()}).get_json()
            routes=get()['subscription_routes']
            self.assertEqual(set(routes),{'codex','claude-code','antigravity','opencode-go','devin','custom-observed','custom-configured'})
            self.assertEqual(len(routes),len(set(routes)))
            config['subscription_prices']={}
            self.assertIn('codex',get()['subscription_routes'])
            self.assertIn('custom-observed',get()['subscription_routes'])

"""Exercise actual Gunicorn socket metadata; no host config or provider access."""
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(sys.platform.startswith('linux'), 'Gunicorn Unix transport requires Linux')
class GunicornTransportTests(unittest.TestCase):
    worker_arguments = []
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='llm-transport-')
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        cls.path = root / 'web.sock'
        config = root / 'config.json'
        config.write_text(json.dumps(dict(database=str(root / 'usage.db'), origin='https://dashboard.example',
                                         secret_key='synthetic-test-only', allowed_logins=['owner@example.invalid'])))
        # Pass a pre-bound descriptor, avoiding the free-port-close-bind race.
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        cls.port = listener.getsockname()[1]
        unix_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        unix_listener.bind(str(cls.path))
        unix_listener.listen()
        cls.process = subprocess.Popen([sys.executable, '-m', 'gunicorn', '--workers', '1', '--bind',
            'fd://' + str(listener.fileno()), '--bind', 'fd://' + str(unix_listener.fileno()), '--no-control-socket',
            *cls.worker_arguments, 'llm_usage.webapp:create_app()'], pass_fds=(listener.fileno(), unix_listener.fileno()),
            env={**os.environ, 'LLM_USAGE_CONFIG':str(config)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        listener.close()
        unix_listener.close()
        cls.addClassCleanup(cls.stop)
        for _ in range(100):
            if cls.process.poll() is not None:
                raise RuntimeError('Synthetic Gunicorn exited during startup')
            if cls.path.exists():
                try:
                    conn = http.client.HTTPConnection('127.0.0.1', cls.port, timeout=.2)
                    conn.request('GET', '/healthz')
                    if conn.getresponse().status == 200:
                        conn.close()
                        break
                except OSError:
                    pass
                finally:
                    conn.close()
            time.sleep(.05)
        else:
            raise RuntimeError('Synthetic Gunicorn startup timed out')

    @classmethod
    def stop(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=5)

    def request(self, path, *, tcp=False, method='GET', headers=None, body=None):
        connection = socket.socket(socket.AF_INET if tcp else socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        try:
            connection.connect(('127.0.0.1', self.port) if tcp else str(self.path))
            payload = (body or '').encode()
            fields = {'Host':'localhost', 'Content-Length':str(len(payload)), 'Connection':'close', **(headers or {})}
            head = f'{method} {path} HTTP/1.1\r\n' + ''.join(f'{k}: {v}\r\n' for k,v in fields.items()) + '\r\n'
            # One write prevents an immediate 403 from closing between header/body writes.
            connection.sendall(head.encode() + payload)
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_real_transport_and_csrf_boundary(self):
        identity = {'Host':'dashboard.example', 'Tailscale-User-Login':'owner@example.invalid'}
        self.assertEqual(self.request('/healthz', tcp=True)[0], 200)
        self.assertEqual(self.request('/api/bootstrap', tcp=True, headers=identity)[0], 403)
        self.assertEqual(self.request('/api/bootstrap')[0], 403)
        self.assertEqual(self.request('/api/bootstrap', headers={**identity, 'Tailscale-User-Login':'other'})[0], 403)
        self.assertEqual(self.request('/api/bootstrap', headers={**identity, 'Host':'attacker.example'})[0], 403)
        status, headers, body = self.request('/api/bootstrap', headers=identity)
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie'].split(';', 1)[0]
        token = json.loads(body)['csrf']
        post = {**identity, 'Content-Type':'application/json', 'Cookie':cookie, 'Origin':'https://dashboard.example'}
        self.assertEqual(self.request('/api/refresh', method='POST', headers=post, body='{}')[0], 403)
        self.assertEqual(self.request('/api/refresh', method='POST', headers={**post, 'X-CSRF-Token':token,
            'Origin':'https://attacker.example'}, body='{}')[0], 403)
        self.assertEqual(self.request('/api/refresh', method='POST', headers={**post, 'X-CSRF-Token':token}, body='{}')[0], 202)


class GunicornThreadedTransportTests(GunicornTransportTests):
    """Match production: --threads 4 selects the gthread worker."""
    worker_arguments = ["--threads", "4"]

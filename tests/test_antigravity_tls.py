"""Loopback Antigravity RPC protocol floor; no provider, socket or credential access."""
from io import BytesIO
import ssl
import unittest
from unittest.mock import patch

from llm_usage import limits


class AntigravityTLSFloorTests(unittest.TestCase):
    def test_probe_and_rpc_reject_protocols_below_tls12_even_with_weak_system_default(self):
        for reader in (limits.read_agy_app,limits.read_agy_quota):
            with self.subTest(reader=reader.__name__):
                context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                # Some runtimes defer their floor to OpenSSL's system configuration.
                # Simulate that explicitly so this test also detects regressions on
                # runtimes whose create_default_context already enforces TLS 1.2.
                context.minimum_version=ssl.TLSVersion.MINIMUM_SUPPORTED
                probe_contexts=[]
                def ready(host,port,ctx,timeout):
                    probe_contexts.append(ctx)
                    self.assertGreaterEqual(ctx.minimum_version,ssl.TLSVersion.TLSv1_2)
                    return True
                with patch.dict(limits._agy_endpoints,{},clear=True),\
                     patch.object(ssl,'create_default_context',return_value=context),\
                     patch.object(limits,'_listening_ports',return_value=[('127.0.0.1',12345)]),\
                     patch.object(limits,'_tls_ready',side_effect=ready),\
                     patch.object(limits.socket,'create_connection',side_effect=AssertionError('network forbidden')),\
                     patch.object(limits.urllib.request,'HTTPSHandler',wraps=limits.urllib.request.HTTPSHandler) as handler,\
                     patch.object(limits.urllib.request,'build_opener') as opener:
                    opener.return_value.open.return_value=BytesIO(b'{"synthetic":true}')
                    self.assertEqual(reader([(4242,'synthetic-csrf')]),{'synthetic':True})
                self.assertEqual(probe_contexts,[context])
                self.assertIs(handler.call_args.kwargs['context'],context)
                self.assertGreaterEqual(context.minimum_version,ssl.TLSVersion.TLSv1_2)
                # The local app uses a self-signed certificate; this remains scoped
                # to the process-owned loopback connection rather than global SSL.
                self.assertFalse(context.check_hostname)
                self.assertEqual(context.verify_mode,ssl.CERT_NONE)


if __name__=='__main__':unittest.main()

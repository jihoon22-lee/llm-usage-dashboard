"""Reject invalid live verification targets before deployment can change services."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import SplitResult

spec=importlib.util.spec_from_file_location('deploy_preflight',Path(__file__).resolve().parents[1]/'packaging/deploy.py')
deploy=importlib.util.module_from_spec(spec);spec.loader.exec_module(deploy)


class DeployPreflightTests(unittest.TestCase):
    def test_invalid_smoke_origin_stops_at_argument_parser_before_any_deployment(self):
        invalid=[None,'http://host.example','https://user:secret@host.example','https://host.example/path',
                 'https://host.example?query=secret','https://host.example#secret','https://host.example:',
                 'https://host.example:70000','https://host.example:notaport','https://[::1]SECRET',
                 'https://host.example%40secret','https://host.example\n','https://-bad.example','https://']
        for origin in invalid:
            with self.subTest(origin=origin),contextlib.ExitStack() as stack:
                args=['deploy.py','--smoke']+(['--smoke-origin',origin] if origin is not None else [])
                stack.enter_context(patch.object(sys,'argv',args))
                stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                blockers=[stack.enter_context(patch.object(deploy,name,side_effect=AssertionError('deployment before validation')))
                          for name in ('checks','restart','deploy_release','switch','run','release_mode')]
                with self.assertRaises(SystemExit) as stopped:deploy.main()
                self.assertEqual(stopped.exception.code,2)
                for blocker in blockers:blocker.assert_not_called()

    def test_valid_dns_and_ipv6_origins_pass_preflight(self):
        for origin in ('https://host.example','https://host.example:9444/','https://[::1]:9444'):
            with self.subTest(origin=origin):self.assertEqual(deploy.smoke_origin(origin),origin.rstrip('/'))

    def test_older_urlsplit_does_not_accept_suffix_after_ipv6_bracket(self):
        with patch.object(deploy.urllib.parse,'urlsplit',return_value=SplitResult('https','[::1]SECRET','','','')):
            with self.assertRaises(ValueError):deploy.smoke_origin('https://[::1]SECRET')


if __name__=='__main__':unittest.main()

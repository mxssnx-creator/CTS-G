"""Bounded, connection-scoped Redis reads; no Redis server or venue access."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path[:0] = [str(Path(__file__).resolve().parent / 'regression'), str(Path(__file__).resolve().parents[1] / 'server/pulse')]
import pulse_trader as mod

class RedisReads(unittest.TestCase):
    def test_credentials_have_a_deadline_and_exact_scope(self):
        with patch.object(mod.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='value\n')) as run:
            self.assertEqual(mod.redis_hget('api_key'), 'value')
        self.assertEqual(run.call_args.args[0], ['redis-cli', 'HGET', mod.REDIS_CONN, 'api_key'])
        self.assertEqual(run.call_args.kwargs.get('timeout'), 6)

    def test_errors_and_nil_are_not_credentials(self):
        for code, value in [(1, 'unexpected-body'), (0, '(nil)'), (0, 'NOAUTH Authentication required.'),
                            (0, 'ERR denied'), (0, 'WRONGTYPE wrong kind'), (0, 'NOPERM denied'), (0, '(error) ERR denied')]:
            with self.subTest(code=code, value=value), patch.object(mod.subprocess, 'run', return_value=SimpleNamespace(returncode=code, stdout=value)):
                self.assertEqual(mod.redis_hget('api_secret'), '')

    def test_unavailable_redis_returns_no_credential_without_retry(self):
        for error in [FileNotFoundError(), subprocess.TimeoutExpired('redis-cli', 6)]:
            with self.subTest(error=type(error).__name__), patch.object(mod.subprocess, 'run', side_effect=error) as run:
                self.assertEqual(mod.redis_hget('base_url'), '')
                self.assertEqual(run.call_count, 1)

    def test_failed_settings_response_is_not_persisted_or_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'settings.json';target.write_text('{"held":"unchanged"}')
            for code, output in [(1, 'setMinPf\n0\n'), (0, 'NOAUTH denied\nsetMinPf\n0\n'), (0, 'WRONGTYPE denied\nvalue\n')]:
                with self.subTest(code=code, output=output), patch.object(mod, '_CTS_CACHE', {'t':0., 'data':{}}), patch.object(mod, 'CTS_PATH', str(target)), patch.object(mod.subprocess, 'run', return_value=SimpleNamespace(returncode=code, stdout=output)) as run:
                    self.assertEqual(mod.dump_cts_settings(), {})
                    self.assertEqual(target.read_text(), '{"held":"unchanged"}')
                    self.assertEqual(run.call_args.kwargs['timeout'], 3)
                    self.assertEqual(run.call_args.args[0], ['redis-cli','HGETALL',f'settings:connection_settings:{mod.CONN_SHORT}'])

    def test_successful_settings_keep_zero_false_json_and_empty_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            with patch.object(mod, '_CTS_CACHE', {'t':0., 'data':{}}), patch.object(mod, 'CTS_PATH', str(path)), patch.object(mod.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='zero\n0\nflag\nfalse\njson\n{"own":1}\nempty\n\n')) as run:
                expected={'zero':0, 'flag':False, 'json':{'own':1}, 'empty':''}
                self.assertEqual(mod.dump_cts_settings(), expected)
                self.assertEqual(json.loads(path.read_text()), expected)
                self.assertEqual(mod.dump_cts_settings(), expected)
                self.assertEqual(run.call_count, 1)

if __name__ == '__main__': unittest.main()

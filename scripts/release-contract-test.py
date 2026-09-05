#!/usr/bin/env python3
"""Credential-free release/import, isolation and replay regression checks."""
import copy
import importlib
import os
import errno
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DATA = tempfile.TemporaryDirectory(prefix="cts-release-test-")
os.environ["CTS_DATA_DIR"] = DATA.name
sys.path.insert(0, str(ROOT / "server/pulse"))
from set_engine import SetBook
from pulse_trader import Pulse, Position
import runtime_scope
import pulse_http


class ReleaseTests(unittest.TestCase):
    def test_redis_readiness_retries_loading_and_requires_pong(self):
        for mode, expected, calls in (("loading",0,3),("auth",1,1),("error",1,5)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                counter = Path(tmp)/'count'
                code = '''source "$1"
timeout() { local n=0; [[ ! -f "$2" ]] || true; [[ ! -f "$COUNT_FILE" ]] || read -r n < "$COUNT_FILE"; n=$((n+1)); echo "$n" > "$COUNT_FILE"; case "$MODE" in loading) [[ "$n" -ge 3 ]] && echo PONG || echo LOADING;; auth) echo NOAUTH;; *) echo ERROR;; esac; }
sleep() { :; }
redis_ready
'''
                env=dict(os.environ,COUNT_FILE=str(counter),MODE=mode)
                r=subprocess.run(['bash','-c',code,'probe',str(ROOT/'deploy/linux-common.sh')],env=env,capture_output=True,timeout=5)
                self.assertEqual(r.returncode,expected);self.assertEqual(int(counter.read_text()),calls)

    def port_probe(self, port):
        return subprocess.run(["bash", "-c", 'source "$1"; can_bind_port "$2"',
                               "probe", str(ROOT / "deploy/linux-common.sh"), str(port)],
                              capture_output=True, text=True, timeout=5).returncode

    def test_port_probe_rejects_an_actual_listener(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            self.assertNotEqual(self.port_probe(listener.getsockname()[1]), 0)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux installer TIME_WAIT regression")
    def test_port_probe_accepts_time_wait_after_http_style_close(self):
        with socket.socket() as listener, socket.socket() as client:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            listener.listen(1)
            client.settimeout(2)
            client.connect(("127.0.0.1", port))
            accepted, _ = listener.accept()
            with accepted:
                accepted.shutdown(socket.SHUT_WR)
                self.assertEqual(client.recv(1), b"")
        # Reproduce the old false positive before verifying the real helper.
        with socket.socket() as plain:
            with self.assertRaises(OSError) as error:
                plain.bind(("0.0.0.0", port))
            self.assertEqual(error.exception.errno, errno.EADDRINUSE)
        self.assertEqual(self.port_probe(port), 0)

    def test_runtime_entrypoints_import_in_clean_process(self):
        for module in ("pulse_trader", "pulse_http", "hist_calc"):
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, "-c",
                    "import sys;sys.path.insert(0,sys.argv[1]);__import__(sys.argv[2])",
                    str(ROOT / "server/pulse"), module], capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr[-1500:])

    def test_canonical_keys_do_not_orphan_existing_install(self):
        with patch.dict(os.environ, {"CTS_G_NAME": "cts-g"}):
            os.environ.pop("CTS_REDIS_PREFIX", None)
            importlib.reload(runtime_scope)
            self.assertEqual(runtime_scope.redis_key("connection:bingx-x01"), "connection:bingx-x01")
            self.assertEqual(runtime_scope.order_tag("bingx-x01"), "Gx01")
            os.environ["CTS_REDIS_PREFIX"] = "cts-g:"
            self.assertEqual(runtime_scope.redis_key("connection:bingx-x01"), "cts-g:connection:bingx-x01")
        importlib.reload(runtime_scope)

    def test_named_installs_have_different_ownership_and_keys(self):
        tags, keys = set(), set()
        for name in ("qa-one", "qa-two"):
            with patch.dict(os.environ, {"CTS_G_NAME": name}):
                os.environ.pop("CTS_REDIS_PREFIX", None)
                importlib.reload(runtime_scope)
                tags.add(runtime_scope.order_tag("bingx-x02"))
                keys.add(runtime_scope.redis_key("connection:bingx-x02"))
                os.environ["CTS_REDIS_PREFIX"] = "foreign:"
                with self.assertRaises(ValueError):
                    runtime_scope.redis_key("connection:bingx-x02")
        importlib.reload(runtime_scope)
        self.assertEqual(len(tags), 2)
        self.assertEqual(len(keys), 2)
        with self.assertRaises(ValueError):
            runtime_scope.order_tag("unknown")

    def test_replay_copy_independent_and_preserves_set_aliases(self):
        book = SetBook()
        book.load({"stratGeneral": True, "stratIndications": False, "stratTrailing": False,
                   "slToTpRatios": [.6], "setMinStep": 3, "setStepMax": 3})
        book.bars["SOL-USDT"] = [[1, 2, .5, 1, 20]]
        book._snap_cache = {"old": [1]}
        cloned = copy.deepcopy(book)
        self.assertIsNot(cloned._pick_lock, book._pick_lock)
        self.assertIs(cloned.by_idx[0], cloned.sets[cloned.by_idx[0].id])
        self.assertIsNone(cloned._snap_cache)
        cloned.bars["SOL-USDT"][0][0] = 3
        self.assertEqual(book.bars["SOL-USDT"][0][0], 1)

    def test_declared_python_requirements_cover_optional_fast_adapters(self):
        required = (ROOT / "server/pulse/requirements.txt").read_text()
        for name in ("numpy==", "httpx==", "websocket-client==", "orjson=="):
            self.assertIn(name, required)

    def test_group_merge_extends_started_close_quantity_without_name_error(self):
        bot = Pulse.__new__(Pulse)
        bot.security_prices = lambda position: (position.sl, position.tp)
        bot.merge_parent_lanes = lambda *args, **kwargs: None
        target = Position("SOL-USDT", "LONG", 2, 100, 1000, 99, 102, 101,
                          client_id="first", close_started_qty=2)
        incoming = Position("SOL-USDT", "LONG", 1, 103, 1001, 99, 102, 103,
                            client_id="second")
        merged = bot.merge_position(target, incoming)
        self.assertEqual(merged.qty, 3)
        self.assertEqual(merged.close_started_qty, 3)
        self.assertAlmostEqual(merged.entry, 101)

    def test_mainnet_start_and_path_inputs_fail_closed(self):
        self.assertFalse(pulse_http._live_start_allowed("bingx-x01"))
        self.assertFalse(pulse_http._live_heal_allowed("bingx-x01"))
        self.assertTrue(pulse_http._live_start_allowed("bingx-x02"))
        self.assertTrue(pulse_http._live_heal_allowed("bingx-x02"))
        with self.assertRaises(ValueError):
            pulse_http.write_overlay("../../outside", {"setMinStep": 1})

    def test_storage_and_historic_range_contracts(self):
        from hist_calc import HOURS_MAX, hours_to_bars, parse_options
        self.assertEqual(HOURS_MAX, 72)
        self.assertEqual(
            {hours_to_bars(hours) for hours in (2, 4, 20, 24, 48, 72, 120)},
            {120, 240, 1200, 1440, 2880, 4320},
        )
        options = parse_options({"hours": 999, "minStep": -3, "stepMax": 999})
        self.assertEqual(options["hours"], HOURS_MAX)
        self.assertEqual(options["minStep"], 1)
        self.assertEqual(options["stepMax"], 22)


if __name__ == "__main__":
    unittest.main(verbosity=2)

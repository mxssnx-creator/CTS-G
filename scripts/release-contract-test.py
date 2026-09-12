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
    def test_no_live_update_restarts_only_the_demo_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            record=Path(tmp)/'calls'
            code='''source "$1"
systemctl() { echo "$*" >> "$CALL_RECORD"; }
redis_has_keys() { return 0; }
pulse_http_unit() { echo qa-http; }
desk_unit() { echo qa-desk; }
retention_timer_unit() { echo qa-retention; }
pulse_instance_unit() { echo "qa-$1"; }
skip() { :; }
VST_SLOT=bingx-x02
LIVE_SLOT=bingx-x01
start_stack 0
'''
            result=subprocess.run(['bash','-c',code,'probe',str(ROOT/'deploy/linux-common.sh')],
                                  env={**os.environ,'CALL_RECORD':str(record)},capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            commands=record.read_text().splitlines()
            self.assertIn('restart qa-bingx-x02',commands)
            self.assertFalse(any('bingx-x01' in row for row in commands))

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

    def test_install_scripts_start_live_by_default(self):
        install = (ROOT / "deploy/install-linux.sh").read_text()
        update = (ROOT / "deploy/update-linux.sh").read_text()
        remote = (ROOT / "deploy/remote-install.sh").read_text()
        common = (ROOT / "deploy/linux-common.sh").read_text()
        http = (ROOT / "server/pulse/pulse_http.py").read_text()
        self.assertIn("START_LIVE=1", install)
        self.assertIn("--enable-live", install)
        self.assertIn("--no-live", install)
        self.assertIn("START_LIVE=1", update)
        self.assertIn("--enable-live", update)
        self.assertIn("--no-live", update)
        self.assertIn("START_LIVE=1", remote)
        self.assertIn("--enable-live", remote)
        self.assertIn("--no-live", remote)
        self.assertIn('local start_live="${1:-1}"', common)
        self.assertIn("clear_live_halt_flags", common)
        self.assertIn("Reload helpers after the tree moves forward", update)
        self.assertIn("CTS_DISABLE_LIVE_START", http)
        self.assertNotIn("CTS_ALLOW_LIVE_START", http)

    def test_mainnet_start_defaults_on_and_can_be_disabled(self):
        os.environ.pop("CTS_DISABLE_LIVE_START", None)
        os.environ.pop("CTS_DISABLE_LIVE_HEAL", None)
        self.assertTrue(pulse_http._live_start_allowed("bingx-x01"))
        self.assertTrue(pulse_http._live_heal_allowed("bingx-x01"))
        self.assertTrue(pulse_http._live_start_allowed("bingx-x02"))
        self.assertTrue(pulse_http._live_heal_allowed("bingx-x02"))
        with patch.dict(os.environ, {"CTS_DISABLE_LIVE_START": "1", "CTS_DISABLE_LIVE_HEAL": "1"}):
            self.assertFalse(pulse_http._live_start_allowed("bingx-x01"))
            self.assertFalse(pulse_http._live_heal_allowed("bingx-x01"))
            self.assertTrue(pulse_http._live_start_allowed("bingx-x02"))
            self.assertTrue(pulse_http._live_heal_allowed("bingx-x02"))
        with self.assertRaises(ValueError):
            pulse_http.write_overlay("../../outside", {"setMinStep": 1})
    def test_pulse_units_run_within_project_tree(self):
        engine = (ROOT / "deploy/grok-pulse@.service").read_text()
        http = (ROOT / "deploy/grok-pulse-http.service").read_text()
        common = (ROOT / "deploy/linux-common.sh").read_text()
        # The shipped pulse units must run from the in-project server/pulse
        # tree, never the former standalone /opt/grok-x01-pulse path.
        for unit in (engine, http):
            self.assertIn("WorkingDirectory=/opt/cts-g/server/pulse", unit)
            self.assertNotIn("/opt/grok-x01-pulse", unit)
        self.assertIn("/opt/cts-g/server/pulse/pulse_trader.py", engine)
        self.assertIn("/opt/cts-g/server/pulse/pulse_http.py", http)
        # render_unit scopes the project-relative pulse path to ${PULSE_DIR}
        # without letting the generic root rewrite re-match it.
        self.assertIn("s|/opt/cts-g/server/pulse|__CTS_PULSE_DIR__|g", common)
        self.assertIn("s|__CTS_PULSE_DIR__|${PULSE_DIR}|g", common)
        self.assertIn('PULSE_DIR="$CTS_G_ROOT/server/pulse"', common)

    def test_render_unit_does_not_double_substitute_root(self):
        # An install name that extends the default (cts-gx) must not expand
        # /opt/cts-g/server/pulse into /opt/cts-gxx/server/pulse, which would
        # leave the pulse units pointing at a nonexistent tree.
        import subprocess
        import tempfile
        common = ROOT / "deploy/linux-common.sh"
        src = ROOT / "deploy/grok-pulse@.service"
        with tempfile.TemporaryDirectory() as td:
            dest = os.path.join(td, "out.service")
            script = (
                "set -euo pipefail; "
                "export CTS_G_NAME=cts-gx CTS_INSTALL_PREFIX= PULSE_PORT=3016 DESK_PORT=3107; "
                f'source "{common}"; '
                f'render_unit "{src}" "{dest}"'
            )
            subprocess.run(["bash", "-c", script], check=True)
            with open(dest) as handle:
                rendered = handle.read()
        self.assertNotIn("/opt/cts-gxx", rendered)
        self.assertIn("WorkingDirectory=/opt/cts-gx/server/pulse", rendered)
        self.assertIn("/opt/cts-gx/server/pulse/pulse_trader.py", rendered)
        self.assertIn("/opt/cts-gx/.venv/bin/python", rendered)

    def test_storage_and_historic_range_contracts(self):
        from hist_calc import HOURS_MAX, hours_to_bars, overlay_from_options, parse_options
        self.assertEqual(HOURS_MAX, 336)
        self.assertEqual(hours_to_bars(1), 60)
        one_hour = overlay_from_options(parse_options({"hours": 1}))
        self.assertEqual(one_hour["histLookbackBars"], 60)
        self.assertEqual(one_hour["histWarmup"], 30)
        self.assertTrue(one_hour["histExactWindow"])
        self.assertEqual(
            {hours_to_bars(hours) for hours in (1, 2, 4, 20, 24, 48, 72, 120, 336)},
            {60, 120, 240, 1200, 1440, 2880, 4320, 7200, 20160},
        )
        options = parse_options({"hours": 9999, "minStep": -3, "stepMax": 999})
        self.assertEqual(options["hours"], HOURS_MAX)
        self.assertEqual(options["minStep"], 1)
        self.assertEqual(options["stepMax"], 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)

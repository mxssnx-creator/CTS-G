"""Offline startup contract: no real systemd, Redis, exchange, or credential calls."""
from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("CTSG_PULSE_DIR", str(ROOT / "server" / "pulse")))
import pulse_http as h


class EndCycle(BaseException):
    """Leave the infinite recovery loop after one iteration, even before its refactor."""


class StartupControls(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.calls = []
        self.redis_calls = []
        self.states = {lane["id"]: "failed" for lane in h.LANES}
        self.enabled = "enabled"
        self.start_rc = {lane["id"]: [1] for lane in h.LANES}
        self.credentials = {"api_key": "fixture-key", "api_secret": "fixture-secret"}
        self.redis_rc = 0
        for name, value in (("DIR", str(self.directory)), ("STOP_ALL_PATH", str(self.directory / "STOP")),
                            ("HALT_ON_INACTIVE", True), ("_sysctl", self.systemctl),
                            ("unit_state", lambda cid, fresh=False: self.states[cid])):
            self.stack.enter_context(patch.object(h, name, value))
        self.stack.enter_context(patch.object(h.subprocess, "run", self.redis))
        self.stack.enter_context(patch.object(h.time, "sleep", side_effect=EndCycle))

    def systemctl(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == "is-enabled":
            return (0 if self.enabled in ("enabled", "enabled-runtime", "static") else 1), self.enabled
        if args[0] == "start":
            cid = args[1].split("@", 1)[1].removesuffix(".service")
            codes = self.start_rc[cid]
            rc = codes.pop(0) if len(codes) > 1 else codes[0]
            return rc, "" if rc == 0 else "fixture start error"
        return 0, ""

    def redis(self, args, **kwargs):
        self.assertEqual(args[:2], ["redis-cli", "HGET"], "unexpected subprocess in offline test")
        self.redis_calls.append(args)
        return subprocess.CompletedProcess(args, self.redis_rc, self.credentials.get(args[-1], ""), "")

    def recover_once(self):
        with self.assertRaises(EndCycle):
            h.heal_loop()

    def mutations(self):
        return [args for args in self.calls if args[0] in ("start", "reset-failed")]

    def test_failed_start_is_failure_and_keeps_entry_halts(self):
        for name in ("STOP", "STOP-bingx-x01", "PAUSE-bingx-x01"):
            (self.directory / name).touch()
        ok, detail = h.apply_control("bingx-x01", "start")
        self.assertFalse(ok, detail)
        self.assertIn("rc=1", detail)
        for name in ("STOP", "STOP-bingx-x01", "PAUSE-bingx-x01"):
            self.assertTrue((self.directory / name).exists(), name)
        self.assertFalse((self.directory / "reset-eq-bingx-x01").exists())

    def test_success_exit_without_active_unit_is_not_success(self):
        self.start_rc["bingx-x01"] = [0]
        for state in ("failed", "inactive", "activating", "deactivating", "unknown"):
            with self.subTest(state=state):
                self.states["bingx-x01"] = state
                ok, detail = h.apply_control("bingx-x01", "start")
                self.assertFalse(ok, detail)

    def test_manual_start_can_retry_and_resume_only_after_success(self):
        self.start_rc["bingx-x01"] = [1, 0]
        self.states["bingx-x01"] = "active"
        (self.directory / "STOP-bingx-x01").touch()
        ok, detail = h.apply_control("bingx-x01", "start")
        self.assertTrue(ok, detail)
        self.assertEqual([a[0] for a in self.mutations()], ["start", "reset-failed", "start"])
        self.assertFalse((self.directory / "STOP-bingx-x01").exists())
        self.assertTrue((self.directory / "reset-eq-bingx-x01").exists())

    def test_overall_start_reports_partial_failure_and_keeps_global_halt(self):
        self.start_rc["bingx-x01"] = [0]
        self.states["bingx-x01"] = "active"
        (self.directory / "STOP").touch()
        ok, detail = h.apply_control("overall", "start")
        self.assertFalse(ok, detail)
        self.assertTrue((self.directory / "STOP").exists())
        self.assertFalse((self.directory / "reset-eq-bingx-x02").exists())

    def test_single_lane_start_keeps_other_lane_globally_halted(self):
        self.start_rc["bingx-x01"] = [0]
        self.states["bingx-x01"] = "active"
        (self.directory / "STOP").touch()
        ok, detail = h.apply_control("bingx-x01", "start")
        self.assertTrue(ok, detail)
        self.assertFalse((self.directory / "STOP").exists())
        self.assertTrue((self.directory / "STOP-bingx-x02").exists())
        self.assertFalse((self.directory / "STOP-bingx-x01").exists())

    def test_no_systemd_mode_does_not_run_service_recovery(self):
        with patch.object(h, "HALT_ON_INACTIVE", False):
            h.heal_loop()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.redis_calls, [])

    def test_recovery_respects_disabled_masked_and_unknown_units(self):
        for state in ("disabled", "masked", "static", "not-found", "", "error"):
            with self.subTest(enabled=state):
                self.enabled = state
                self.calls.clear()
                self.redis_calls.clear()
                self.recover_once()
                self.assertEqual(self.mutations(), [])
                self.assertEqual(self.redis_calls, [])

    def test_recovery_leaves_running_and_transitioning_units_alone(self):
        for state in ("active", "activating", "deactivating", "unknown"):
            with self.subTest(state=state):
                self.states = {cid: state for cid in self.states}
                self.calls.clear()
                self.recover_once()
                self.assertEqual(self.mutations(), [])

    def test_recovery_requires_both_successful_redis_fields(self):
        for key, secret, rc in (("fixture-key", "", 0), ("(nil)", "fixture-secret", 0),
                                ("fixture-key", "(nil)", 0), ("error", "error", 1)):
            with self.subTest(key=bool(key), secret=bool(secret), rc=rc):
                self.credentials = {"api_key": key, "api_secret": secret}
                self.redis_rc = rc
                self.calls.clear()
                self.recover_once()
                self.assertEqual(self.mutations(), [])

    def test_recovery_still_retries_enabled_failed_or_inactive_units(self):
        for enabled, state in (("enabled", "failed"), ("enabled-runtime", "inactive")):
            with self.subTest(enabled=enabled, state=state):
                self.enabled = enabled
                self.states = {cid: state for cid in self.states}
                self.calls.clear()
                self.recover_once()
                self.assertEqual(len([a for a in self.calls if a[0] == "start"]), 2)

    def test_recovery_honors_manual_stop(self):
        (self.directory / "STOP-bingx-x02").touch()
        self.recover_once()
        self.assertEqual([a[1] for a in self.calls if a[0] == "start"], ["grok-pulse@bingx-x01"])
        self.calls.clear()
        (self.directory / "STOP").touch()
        self.recover_once()
        self.assertEqual(self.mutations(), [])


class SystemdPrerequisite(unittest.TestCase):
    def test_missing_manager_aborts_and_working_manager_passes(self):
        # Even the successful case uses a shell function, never real systemd.
        script = '''source "$1"
systemd_runtime_present() { [[ "$CTSG_MANAGER_RUNTIME" == 1 ]]; }
systemctl() { [[ "$*" == "--system show-environment" ]] || return 99; return "$CTSG_MANAGER_RC"; }
require_systemd
printf 'READY\n'
'''
        for runtime, manager_rc in ((1, 0), (1, 1), (1, 127), (0, 0)):
            with self.subTest(runtime=runtime, manager_rc=manager_rc):
                proc = subprocess.run(["bash", "-c", script, "--", str(ROOT / "deploy" / "linux-common.sh")],
                                      env=dict(os.environ, CTSG_MANAGER_RUNTIME=str(runtime), CTSG_MANAGER_RC=str(manager_rc)),
                                      capture_output=True, text=True, timeout=10)
                expected = runtime == 1 and manager_rc == 0
                self.assertEqual(proc.returncode == 0, expected, proc.stderr)
                self.assertEqual("READY" in proc.stdout, expected)
                if not expected:
                    self.assertIn("running systemd manager is required", proc.stderr)

    def test_installer_and_updater_check_manager_before_mutations(self):
        for name in ("install-linux.sh", "update-linux.sh"):
            with self.subTest(script=name):
                source = (ROOT / "deploy" / name).read_text()
                self.assertLess(source.index("\nrequire_systemd\n"), source.index("\nensure_base_packages\n"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

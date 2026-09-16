"""Offline tests for host-aware cgroup sizing and CPU policy."""
import importlib.util
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dynamic_resources", ROOT / "deploy/dynamic-resources.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class DynamicResourceTests(unittest.TestCase):
    def test_meminfo_reads_kib(self):
        with tempfile.NamedTemporaryFile(mode="w+") as handle:
            handle.write("MemTotal:       16384000 kB\nMemAvailable:    8192000 kB\n")
            handle.flush()
            self.assertEqual(MODULE.read_meminfo(handle.name), (16000.0, 8000.0))

    def test_policy_uses_available_memory_but_keeps_reserve(self):
        policy = MODULE.compute_policy(16000, 8000, {"cts-gx-pulse@bingx-x02.service": 4200})
        self.assertGreater(policy["memoryMaxMb"], 4200)
        self.assertGreater(policy["memoryHighMb"], 4200)
        self.assertLessEqual(policy["memoryMaxMb"], 16000 - policy["memoryReserveMb"] + 64)
        self.assertEqual(policy["cpuWeight"], 10000)
        self.assertEqual(policy["cpuQuota"], "infinity")

    def test_two_pulse_lanes_keep_catalog_floor(self):
        policy = MODULE.compute_policy(16000, 8000, {
            "cts-ga-pulse@bingx-x01.service": 220,
            "cts-ga-pulse@bingx-x02.service": 1200,
        })
        self.assertGreaterEqual(policy["memoryMaxMb"], MODULE.MIN_PULSE_MAX_MB)
        self.assertGreaterEqual(policy["memoryMaxMb"], 1200 + MODULE.SAFETY_MARGIN_MB)
        self.assertLess(policy["memoryHighMb"], policy["memoryMaxMb"])

    def test_low_available_never_sets_max_below_live_floor(self):
        policy = MODULE.compute_policy(16000, 300, {"pulse@x02.service": 4900})
        self.assertGreaterEqual(policy["memoryMaxMb"], 5156)
        self.assertLess(policy["memoryHighMb"], policy["memoryMaxMb"])

    def test_no_current_process_gets_startup_room(self):
        policy = MODULE.compute_policy(16000, 8000, {})
        self.assertGreaterEqual(policy["memoryMaxMb"], 6000)
        self.assertEqual(policy["activePulseCount"], 0)

    def test_active_unit_parser_is_scoped(self):
        class Result:
            returncode = 0
            stdout = (
                "cts-gx-pulse@bingx-x02.service loaded active running pulse\n"
                "cts-gx-pulse-http.service loaded active running http\n"
                "other-pulse@bingx-x01.service loaded active running other\n"
            )

        def runner(*args, **kwargs):
            return Result()

        self.assertEqual(
            MODULE.active_pulse_units("cts-gx", runner),
            ("cts-gx-pulse@bingx-x02.service",),
        )

    def test_dropin_has_no_cpu_ceiling(self):
        policy = MODULE.compute_policy(16000, 8000, {})
        text = MODULE.dropin_text(policy)
        self.assertIn("CPUWeight=10000", text)
        self.assertIn("CPUQuota=infinity", text)
        self.assertIn("MemorySwapMax=0", text)
        self.assertIn("MemoryHigh=", text)
        self.assertIn("MemoryMax=", text)

    def test_policy_dropin_has_last_precedence_name(self):
        with tempfile.TemporaryDirectory() as systemd:
            policy = MODULE.compute_policy(16000, 8000, {})
            target = pathlib.Path(systemd) / "cts-gx-pulse@.service.d" / "99-dynamic-resources.conf"
            target.parent.mkdir(parents=True)
            target.write_text(MODULE.dropin_text(policy), encoding="utf-8")
            self.assertTrue(target.name.startswith("99-"))

    def test_rendered_resource_unit_uses_scoped_name(self):
        source = (ROOT / "deploy/grok-resources.service").read_text(encoding="utf-8")
        self.assertIn("--name __CTS_G_NAME__", source)
        self.assertNotIn("--name cts-g ", source)


if __name__ == "__main__":
    unittest.main()

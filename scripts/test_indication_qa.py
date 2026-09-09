"""Regression for the false VST self-test failure with Common disabled."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server/pulse"))
from indication_engine import IndicationBook
from pulse_trader import Pulse
from unittest.mock import patch


class IndicationQaTests(unittest.TestCase):
    def test_all_enabled_and_disabled_combinations_are_valid(self):
        kinds = ("state", "direction", "move", "active", "common", "signals", "trend", "break")
        pulse = Pulse.__new__(Pulse)
        pulse.indications = IndicationBook()
        results = []
        pulse.record_test = lambda name, ok, detail: results.append((name, ok, detail))
        for mask in range(256):
            pulse.indications.settings.update({"type" + kind.title(): bool(mask & (1 << index)) for index, kind in enumerate(kinds)})
            pulse._qa_indication_types(pulse.indications.snapshot())
        self.assertEqual(len(results), 256)
        self.assertTrue(all(row[1] for row in results), [row for row in results if not row[1]])

    def test_missing_or_incorrect_type_still_fails(self):
        pulse = Pulse.__new__(Pulse)
        pulse.indications = IndicationBook()
        results = []
        pulse.record_test = lambda name, ok, detail: results.append(ok)
        snapshot = pulse.indications.snapshot()
        del snapshot["types"]["trend"]
        pulse._qa_indication_types(snapshot)
        snapshot = pulse.indications.snapshot()
        snapshot["types"]["break"] = False
        pulse._qa_indication_types(snapshot)
        self.assertEqual(results, [False, False])

    def test_failure_is_visible_and_status_counters_do_not_accumulate_on_recovery(self):
        pulse = Pulse.__new__(Pulse)
        pulse.test_map = {}; pulse.qa_pass = 0; pulse.qa_fail = 0
        with patch('pulse_trader.log'):
            pulse.record_test('configured-kind', True)
            pulse.record_test('configured-kind', False, 'real mismatch')
            for i in range(40): pulse.record_test(f'check-{i}', True)
            self.assertEqual((pulse.qa_pass, pulse.qa_fail), (40, 1))
            self.assertEqual(pulse.tests[0]['name'], 'configured-kind')
            self.assertEqual(len(pulse.tests), 28)
            pulse.record_test('configured-kind', True)
            pulse.record_test('configured-kind', True)
            self.assertEqual((pulse.qa_pass, pulse.qa_fail), (41, 0))
            pulse.record_test('configured-kind', False)
            pulse.record_test('configured-kind', True)
            self.assertEqual((pulse.qa_pass, pulse.qa_fail), (41, 0))


if __name__ == "__main__":
    unittest.main()

"""volumeFactor end to end: overlay load, entry, Block/DCA adds and controls, offline."""
import pathlib
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server/pulse'))
import pulse_trader as pt
import storage_paths


class _StopAfterVolume(Exception):
    """Raised by the first setting read after targetNotional/volumeFactor."""


class _LevMap(dict):
    def __bool__(self):
        raise _StopAfterVolume


class VolumeFactorOverlayLoad(unittest.TestCase):
    def load(self, overlay):
        p = pt.Pulse.__new__(pt.Pulse)
        p.api = NS()
        p.lev_map = _LevMap()
        with patch.object(pt, 'dump_cts_settings', return_value={}), \
                patch.object(pt, 'load_json_file', return_value=dict(overlay)), \
                patch.object(pt.redis_config, 'configure'), \
                patch.object(storage_paths, 'configure_retention'), \
                patch.object(pt, 'TARGET_NOTIONAL', 2.15):
            with self.assertRaises(_StopAfterVolume):
                p.apply_live_config(initial=True)
            return pt.TARGET_NOTIONAL, p.volume_factor

    def test_saved_factor_and_target_are_applied_within_engine_bounds(self):
        self.assertEqual(self.load({'volumeFactor': 2.5, 'targetNotional': 3}), (3.0, 2.5))
        self.assertEqual(self.load({'volumeFactor': 99, 'targetNotional': 9999}), (500.0, 10.0))
        self.assertEqual(self.load({'volumeFactor': 0.01, 'targetNotional': 0.01}), (0.2, 0.05))
        self.assertEqual(self.load({}), (2.15, 1.0))

    def test_nonfinite_or_corrupt_values_never_saturate_to_the_maximum(self):
        # write_overlay rejects float NaN, but a JSON string still round-trips
        # and float("nan") passes min(): it must not become 10x / 500 USDT.
        for bad in ('nan', 'NaN', 'inf', '-Infinity', 'abc', None):
            target, factor = self.load({'volumeFactor': bad, 'targetNotional': bad})
            self.assertEqual(factor, 1.0, bad)
            self.assertEqual(target, 2.15, bad)


if __name__ == '__main__':
    unittest.main()

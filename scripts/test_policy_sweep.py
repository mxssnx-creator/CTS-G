"""Independent scalar oracle for the accelerated admission-policy sweep."""
import pathlib
import subprocess
import tempfile
import unittest

import numpy as np
from sweep_seven_days import kernel


def oracle(entry, close, net, cost, policy, end, split):
    window, deact, hours, cadence, main, real = policy
    r = np.zeros(20)
    checked = -1
    valid = False
    disabled = False
    accepted = []
    equity = peak = 0.
    underwater = None
    for j, start in enumerate(entry):
        past = [k for k in range(j) if close[k] < start]
        if len(past) < max(window, main, real):
            continue
        if checked < 0 or len(past) - checked >= cadence:
            checked = len(past)
            valid = all(1 + .1 * np.mean([net[k]/cost[k] for k in past[-n:]]) > 1.02 + 1e-9
                        for n in (window, main, real))
        shadow = high = 0.
        dd = None
        for k in past:
            shadow += net[k]
            if shadow >= high - 1e-12:
                high = max(high, shadow)
                dd = None
            elif dd is None:
                dd = close[k]
        if not valid or disabled or (dd is not None and start - dd > hours * 60):
            continue
        v = net[j]
        accepted.append(v)
        r[:6] += [1, v > 0, v, cost[j], max(0, v), max(0, -v)]
        r[13] += v/cost[j]
        offset = 8 if close[j] < split else 10
        r[offset:offset+2] += [1, v]
        col = 14 if close[j] < split else 17
        r[col:col+3] += [max(0,v),max(0,-v),v/cost[j]]
        equity += v
        if equity >= peak - 1e-12:
            if underwater is not None:
                r[7] = max(r[7], close[j] - underwater)
            peak = max(peak, equity)
            underwater = None
        elif underwater is None:
            underwater = close[j]
        r[6] = max(r[6], peak - equity)
        if underwater is not None:
            r[7] = max(r[7], close[j] - underwater)
        if len(accepted) >= deact and sum(accepted[-deact:]) < -1e-12:
            disabled = True
            r[12] = 1
    if underwater is not None:
        r[7] = max(r[7], end - underwater)
    return r


class PolicySweepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        lib = pathlib.Path(cls.tmp.name)/'sweep.so'
        subprocess.run(['g++', '-O3', '-shared', '-fPIC', str(pathlib.Path(__file__).with_name('policy_sweep.cpp')), '-o', str(lib)], check=True)
        cls.fn = staticmethod(kernel(lib))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_kernel(self, entry, close, net, cost, policies):
        result = np.zeros((len(policies), 20))
        args = [np.ascontiguousarray(x, dtype=t) for x, t in
                ((entry, np.int32), (close, np.int32), (net, float), (cost, float))]
        self.fn(len(entry), *args, len(policies), np.array(policies, dtype=np.int32), int(close[-1]+60), 450, result)
        return result

    def test_random_events_match_scalar_oracle(self):
        rng = np.random.default_rng(90212)
        policies = [(n, d, h, c, m, r) for n in (5, 30, 75) for d in (5, 15, 25)
                    for h in (1, 9) for c in (1, 5, 10) for m, r in ((5, 3), (15, 10))]
        for _ in range(4):
            close = np.cumsum(rng.integers(2, 12, 120))
            entry = np.r_[0, close[:-1] + 1]
            net = rng.choice([-.004, -.002, .001, .006], 120)
            cost = rng.uniform(.0008, .002, 120)
            actual = self.run_kernel(entry, close, net, cost, policies)
            expected = np.array([oracle(entry, close, net, cost, p, close[-1]+60, 450) for p in policies])
            np.testing.assert_allclose(actual, expected, atol=1e-10)

    def test_cold_start_and_same_bar_do_not_use_future_closes(self):
        close = np.arange(10, 110, 10)
        entry = close - 10  # prior close at entry is deliberately ineligible
        actual = self.run_kernel(entry, close, np.full(10, .003), np.full(10, .001), [(5, 5, 9, 1, 5, 3)])
        self.assertEqual(actual[0, 0], 4)  # first admissible entry is index six


if __name__ == '__main__':
    unittest.main()

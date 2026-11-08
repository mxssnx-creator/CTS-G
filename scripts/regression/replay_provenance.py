"""Replay guard and cache-provenance checks; no exchange access."""
import json
import os
import subprocess
import sys
import tempfile

from common import ROOT, T0, synth_bars
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import replay_cache
from fetch_audit_market import select_universe


def market_audit_selects_200_eligible_distinct_symbols():
    tickers = [{"symbol": f"COIN{i:03d}-USDT", "quoteVolume": str(i)} for i in range(300)]
    contracts = {r["symbol"]: {"apiStateOpen": True if i % 2 else "TRUE"}
                 for i, r in enumerate(tickers)}
    # These higher-volume records must not consume the first 200 places.
    excluded = [f"NCSK{i}-USDT" for i in range(90)] + ["NCCOXAU-USDT", "NCFXEUR-USDT", "ETH-USDC", "SOL-USDC"]
    for symbol in excluded:
        tickers.append({"symbol": symbol, "quoteVolume": "1000000"})
        contracts[symbol] = {"apiStateOpen": "true"}
    tickers.extend([dict(tickers[-1]), dict(tickers[299]),
                    {"symbol": "CLOSED-USDT", "quoteVolume": "9999999"},
                    {"symbol": "MISSING-USDT", "quoteVolume": "9999999"}])
    contracts["CLOSED-USDT"] = {"apiStateOpen": False}
    for i, value in enumerate(("NaN", "Infinity", "invalid", "-1")):
        symbol = f"BAD{i}-USDT"
        tickers.append({"symbol": symbol, "quoteVolume": value})
        contracts[symbol] = {"apiStateOpen": True}
    actual = [r["symbol"] for r in select_universe(tickers, contracts)]
    expected = [f"COIN{i:03d}-USDT" for i in range(299, 99, -1)]
    ok = actual == expected and len(set(actual)) == 200
    return ok, f"eligible unique symbols={len(set(actual))}; high-volume exclusions cannot crowd out the crypto universe"


def caches_refuse_mismatched_inputs_or_missing_metadata():
    with tempfile.TemporaryDirectory() as d:
        data, engine = os.path.join(d, "data"), os.path.join(d, "engine")
        os.mkdir(data); os.mkdir(engine)
        market = os.path.join(data, "A-USDT.json")
        code = os.path.join(engine, "set_engine.py")
        sim = os.path.join(d, "stage_eval.py")
        for path, value in ((market, "market"), (code, "engine"), (sim, "simulator")):
            with open(path, "w") as f:
                f.write(value)
        def fp(overlay):
            return replay_cache.fingerprint(data, ["A-USDT"], overlay, engine, sim)
        base, changed = fp({"cost": .12}), fp({"cost": .15})
        cache = os.path.join(d, "shadow.pkl")
        replay_cache.save(cache, {"result": 1}, base)
        exact = replay_cache.load(cache, base) == {"result": 1}
        def refuses(expected):
            try:
                replay_cache.load(cache, expected)
            except ValueError:
                return True
            return False
        settings_refused = refuses(changed)
        with open(market, "a") as f: f.write("x")
        data_refused = refuses(fp({"cost": .12}))
        with open(market, "w") as f: f.write("market")
        with open(code, "a") as f: f.write("x")
        code_refused = refuses(fp({"cost": .12}))
        with open(code, "w") as f: f.write("engine")
        with open(sim, "a") as f: f.write("x")
        sim_refused = refuses(fp({"cost": .12}))
        with open(cache, "ab") as f: f.write(b"modified")
        bytes_refused = refuses(base)
        os.unlink(cache + ".meta.json")
        legacy_refused = refuses(base)
        ok = exact and all((settings_refused, data_refused, code_refused, sim_refused, bytes_refused, legacy_refused))
        return ok, f"exact={exact}; settings/data/code/simulator/bytes/legacy refused={settings_refused}/{data_refused}/{code_refused}/{sim_refused}/{bytes_refused}/{legacy_refused}"


def simulator_models_automatic_holds():
    with tempfile.TemporaryDirectory() as d:
        preset = json.load(open(os.path.join(ROOT, "presets", "secure-minimal.json"), encoding="utf-8"))
        def run(patch):
            return subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "trade_sim.py"), "--data", d,
                                   "--cache", os.path.join(d, "cache.pkl"), "--patch", json.dumps(patch)],
                                  capture_output=True, text=True, timeout=30)
        auto = run({**preset, "holdEnabled": True, "holdAuto": True})
        fixed = run({**preset, "holdAuto": False})
        # automatic holds are modelled now (stage_eval.build_hold_auto: the engine's own lane per Set): both settings reach the data check
        ok = ("Automatic hold selection is not modeled" not in auto.stdout and auto.returncode == 2 and "No market data found" in auto.stdout
              and fixed.returncode == 2 and "No market data found" in fixed.stdout)
        return ok, f"automatic holds reach the data check={'No market data found' in auto.stdout}; fixed hold reaches data check={'No market data found' in fixed.stdout}"


def simulator_builds_and_reuses_only_matching_cache():
    with tempfile.TemporaryDirectory() as d:
        candles = [[int((T0 + i * 60) * 1000), *bar] for i, bar in enumerate(synth_bars(7, 300))]
        with open(os.path.join(d, "A-USDT.json"), "w") as f:
            json.dump(candles, f)
        patch = {"holdEnabled": False, "histLookbackBars": 120, "histLookbackAuto": False,
                 "setStepAdapt": False, "setMinStep": 2, "setStepMax": 2, "setSlRatios": [1],
                 "stratTrailing": False, "stratShortRange": False, "exitIgnoreTp": False}
        cache = os.path.join(d, "shadow.pkl")
        def run(settings):
            return subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "trade_sim.py"), "--data", d,
                                   "--cache", cache, "--symbols", "1", "--jobs", "1", "--last-hours", "1",
                                   "--patch", json.dumps(settings)], capture_output=True, text=True, timeout=60)
        first, second = run(patch), run(patch)
        changed = run({**patch, "noise": .17})
        ok = (first.returncode == second.returncode == 0 and os.path.isfile(cache + ".meta.json")
              and changed.returncode != 0 and "cache provenance differs" in changed.stderr)
        return ok, f"build/reuse exits={first.returncode}/{second.returncode}; changed settings refused={changed.returncode != 0}"


CHECKS = [("replay-provenance." + fn.__name__.replace("_", "-"), fn) for fn in (
    market_audit_selects_200_eligible_distinct_symbols,
    caches_refuse_mismatched_inputs_or_missing_metadata, simulator_models_automatic_holds,
    simulator_builds_and_reuses_only_matching_cache)]

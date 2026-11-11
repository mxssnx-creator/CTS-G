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


def micro_short_selection_preserves_losses_and_sample_requirements():
    from micro_short_study import metric, candidate_rank
    wins = metric([.01] * 20)
    good = {"closed": wins, "marked": wins, "halves": [metric([.01] * 10)] * 2}
    held_loss = {**good, "marked": metric([.01] * 20 + [-.25])}
    thin = {**good, "halves": [metric([.01]), metric([.01] * 19)]}
    unknown_rejected = False
    try:
        metric([.01, float("nan")])
    except ValueError:
        unknown_rejected = True
    ok = (metric([])["pf"] is None and metric([0])["pf"] == 1
          and candidate_rank(good)[0] and not candidate_rank(held_loss)[0]
          and not candidate_rank(thin)[0] and unknown_rejected)
    return ok, "no-loss is distinct from empty; held loss, thin half and unknown return cannot qualify"


def micro_short_plans_keep_ranges_and_signal_families_independent():
    from micro_short_study import plans
    from set_engine import extra_known
    base = {"lastNByRange": {"long": {"realN": 25}}, "minPfStepRatio": .1}
    original = json.dumps(base, sort_keys=True)
    groups = plans(base) + [g for g in plans(base, True) if g["id"].endswith("-microfamily")]
    profiles = [p["id"] for g in groups for p in g["profiles"]]
    unknown = []
    for g in groups:
        for cfg in g["overlay"]["indicationConfigs"]:
            for name in cfg["names"]:
                for relation in cfg["relations"]:
                    source = name + ("@" + relation if relation else "")
                    if not extra_known(source): unknown.append(source)
    micro = next(g for g in groups if g["id"] == "micro-hold0-core")
    short = next(g for g in groups if g["id"] == "short-hold0-core")
    ok = (len(profiles) == len(set(profiles)) == 84 and not unknown
          and micro["overlay"]["setStepMax"] == 3 and short["overlay"]["setMinStep"] == 7
          and json.dumps(base, sort_keys=True) == original)
    return ok, f"84 distinct profiles; unknown sources={unknown}; source settings unchanged"


CHECKS = [("replay-provenance." + fn.__name__.replace("_", "-"), fn) for fn in (
    market_audit_selects_200_eligible_distinct_symbols,
    caches_refuse_mismatched_inputs_or_missing_metadata, simulator_models_automatic_holds,
    simulator_builds_and_reuses_only_matching_cache, micro_short_selection_preserves_losses_and_sample_requirements,
    micro_short_plans_keep_ranges_and_signal_families_independent)]

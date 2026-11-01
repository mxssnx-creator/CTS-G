"""Independent, deliberately simple three-type stage oracle (no engine imports).

Unit-notional returns, completed minute bars, strict exit-before-entry clocks.
No allocation or exchange model. Scalar position dictionaries and full lists.
"""


def scalar_stages(bars, events, protection, lane, fees, history_minutes, floor, max_dd, side):
    names = ("base", "main", "real")
    closed = {s: [] for s in names}
    opened = {s: None for s in names}
    for bar in bars:
        t, op, high, low, close, _ = bar
        passes = {}
        for s in names:
            values = [v for end, v in closed[s] if t-history_minutes*60000 <= end < t][-lane[s]:]
            gain, loss = sum(v for v in values if v > 0), -sum(v for v in values if v < 0)
            total = peak = dd = 0
            for value in values:
                total += value; peak = max(peak, total); dd = max(dd, peak-total)
            passes[s] = len(values) == lane[s] and gain > 0 and (loss == 0 or gain / loss >= floor) and dd <= max_dd
        if events.get(t) == side:
            for s, admit in (("base", True), ("main", passes["base"]), ("real", passes["base"] and passes["main"])):
                if admit and opened[s] is None:
                    entry = op * (1 + side * fees["slippagePerLeg"])
                    opened[s] = {"t":t,"entry":entry,"best":entry,"stop":entry*(1-side*protection["sl"])}
        for s in names:
            if opened[s] is None: continue
            position=opened[s]
            at,entry,stop=position["t"],position["entry"],position["stop"]
            target=entry*(1+side*protection["tp"]) if protection["tp"] else None
            exit_ = None
            if side * (op-stop) <= 0: exit_ = op
            elif (low <= stop if side > 0 else high >= stop): exit_ = stop
            elif target is not None and (high >= target if side > 0 else low <= target): exit_ = target
            if exit_ is None and protection["kind"] != "normal":
                best=max(position["best"],high) if side>0 else min(position["best"],low)
                position["best"]=best
                gain=(best-entry)*side/entry
                if gain>=protection["trailArm"]:
                    candidate=best-side*entry*protection["trailGap"]
                    stop=max(stop,candidate) if side>0 else min(stop,candidate)
                    position["stop"]=stop
                    if side*(close-stop)<=0:exit_=stop
            if exit_ is None and t+60000-at >= protection["holdMinutes"]*60000: exit_ = close
            if exit_ is not None:
                fill = exit_*(1-side*fees["slippagePerLeg"])
                net = side*(fill/entry-1) - fees[fees["entryLiquidity"]] - (fill/entry)*fees[fees["exitLiquidity"]]
                closed[s].append((t+60000, net)); opened[s] = None
    return closed, opened


# Compatibility for the initial normal-only differential fixture.
normal_stages = scalar_stages

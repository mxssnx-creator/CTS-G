"""Compare Axis sizing, Block and DCA on identical qualified, historical Exec entries."""
import argparse
import bisect
from collections import defaultdict
import json
import math
import os
from pathlib import Path
import pickle
import tempfile

import stage_eval as sg
import replay_cache
import axis_sets
import block_rules
from dca_engine import DcaBook
from pulse_trader import Position, Pulse
from set_block import SetBlockBook


def metrics(closes):
    gains = losses = balance = peak = worst = 0.
    for _, returns in sorted(closes.items()):
        # Simultaneous settlements form a single equity observation.
        for amount in returns:
            gains += max(0., amount)
            losses += max(0., -amount)
        balance += sum(returns)
        peak = max(peak, balance)
        worst = max(worst, peak - balance)
    return dict(closed=sum(map(len, closes.values())), pf=round(gains / losses, 4) if losses else None,
                gain=gains, loss=losses, net=round(balance * 100, 3), drawdown=round(worst * 100, 3))


def variants():
    yield "control", {}, {}, {}
    for n, floor, cap in ((8, .5, 1), (15, .75, 1), (25, .75, 1.25), (25, .5, 1.5)):
        yield f"axis-{n}-{floor}-{cap}", dict(enabled=True, n=n, minN=8, vol="floor", vmin=floor, vmax=cap), {}, {}
    yield "block-shipped", {}, {"shipped":True}, {}
    for step in (1, 2):
        for ratio in (.1, .2, .4):
            block = dict(blockRelations=["set"], blockCounts=[dict(step=step, ratio=ratio)] * 2,
                         blockStackType="shared", blockSharedTotal=ratio, blockMinObservations=3,
                         blockProfitFactorRatio=1, blockSkipFirst=0, blockCarry=False)
            yield f"block-{step}-{ratio}", {}, block, {}
    selected_axis = dict(enabled=True,n=15,minN=8,vol="floor",vmin=.75,vmax=1)
    selected_block = dict(blockRelations=["set"], blockCounts=[dict(step=2,ratio=.1)]*2,
                          blockStackType="shared",blockSharedTotal=.1,blockMinObservations=3,
                          blockProfitFactorRatio=1,blockSkipFirst=0,blockCarry=False)
    yield "axis15-block2", selected_axis, selected_block, {}
    yield "axis15-dca-bounce", selected_axis, {}, dict(fraction=.6,ratio=.1,bounce=True)
    for distance in (.2, .4, .6):
        for ratio in (.1, .25, .5):
            # A 0.8% stop times this distance is the configured adverse percentage of entry price.
            for bounce in (False, True):
                yield f"dca-{distance}-{ratio}" + ("-bounce" if bounce else ""), {}, {}, dict(fraction=distance, ratio=ratio, bounce=bounce)


def study(market, receipts, cache, count, arms):
    with open(receipts, "rb") as stream:
        run = pickle.load(stream)  # trusted optional local output of trade_sim
    os.environ["STAGE_OVERLAY_PATCH"] = json.dumps(run["overlay"])
    names, bars, times = sg.load_bars(market, count)
    manifest = run["fingerprint"]
    if set(names) != set(manifest["markets"]) or any(replay_cache.file_sha(Path(market) / (s + ".json")) != manifest["markets"][s] for s in names):
        raise ValueError("Candles differ from the Exec replay")
    shadow = replay_cache.load(cache, manifest)
    book = sg.make_book()
    if not book.stage_grid or book.grid_auto_enabled():
        raise ValueError("This comparison needs the fixed-floor stage-grid research preset")
    tapes = sg.Tapes(book, names, times, shadow)
    settles = [r for r in run["rows"] if r.get("execLaneId") and ("tr" in r or r.get("open_at_end"))]
    events, mem = [], {}
    for i, row in enumerate(settles):
        symbol, start = row["symbol"], row["t_in"]
        tr = row.get("tr")
        end = row["t"] if tr else row["t_mark"]
        entry = tr["entry_px"] if tr else row["entry"]
        side = tr["side"] if tr else row["side"]
        st = book.sets[row["set"]]
        sl, tp = book.lane_fracs(st)
        mem[i] = (st, book.stage_grid.path(st.id, row["execLaneId"]), entry, side, sl, tp)
        events.append((start, 2, i, entry))
        events.append((end, 1, i, tr["exit_px"] if tr else row["mark"]))
        first = bisect.bisect_right(times[symbol], start - 60)
        last = bisect.bisect_left(times[symbol], end - 60)
        for j in range(first, last):
            events.append((times[symbol][j] + 60, 0, i, bars[symbol][j][3]))
    events.sort()
    proof, samples = {}, {}
    def allowed(i, at):
        st, path, *_ = mem[i]
        key = (st.id, path, at)
        if key not in proof:
            result = sg.grid_at(book, tapes, st, at, streams=False)
            proof[key] = bool(result.lanes and result.lanes.contains(path))
        return proof[key]
    def size(i, at, config):
        if not config.enabled:
            return 1.
        st, path, *_ = mem[i]
        key = st.id, path, at
        if key not in samples:
            result = sg.grid_at(book, tapes, st, at)
            history, floor = result.exec_parent(path, sg.se.PROC_KEEP)
            samples[key] = ([x.close for x in history], [x.entry for x in history], [x.net for x in history], floor)
        verdict = axis_sets.choose(*samples[key], config)
        return verdict["vol"] if verdict["allow"] else 1.
    out = {}
    for label, ax, blk, dca in arms:
        positions, additions, closes, sizing = {}, defaultdict(list), defaultdict(list), {}
        n_adds = sized = 0
        dbook = DcaBook()
        dbook.load(run["overlay"])
        with tempfile.TemporaryDirectory() as folder:
            block = SetBlockBook(str(Path(folder) / "block.json"))
            block.save = lambda: None
            if blk:
                block.cfg = block_rules.clean_cfg(run["overlay"] if "shipped" in blk else blk)
            axis = axis_sets.config_of(ax)
            for at, kind, i, px in events:
                row = settles[i]
                st, path, entry, side, sl, tp = mem[i]
                sign = 1 if side == "LONG" else -1
                if kind == 2:
                    sizing[i] = size(i, at, axis) * row.get("vol", 1)
                    sized += abs(sizing[i] - row.get("vol", 1)) > 1e-12
                    positions[i] = Position(row["symbol"], side, 1., entry, at, entry*(1-sign*sl), entry*(1+sign*tp), entry,
                        set_id=st.id, pack=st.pack, lane=row["lane"], exec_lane_id=row["execLaneId"],
                        base_entry=entry, base_qty=1., client_id=f"replay-{i}")
                    continue
                pos = positions[i]
                if kind == 1:
                    if not row.get("open_at_end"):
                        if blk:
                            block.close(pos, px, at, None, book.cost_pct)
                        extra = sum(q * (sign*(px-price) - book.cost_pct / 100 * price)/entry for price,q in additions[i])
                        closes[at].append(sizing[i] * (row["net"] + extra))
                        if dca and additions[i]:
                            price, _ = additions[i][0]
                            gross = sign*(px/price-1)
                            dbook.on_close(dict(symbol=pos.symbol, side=side, scope=Pulse.dca_scope(pos), dca_n=1, t=at,
                                                client_id=pos.client_id, pnl_pct=gross, net_pnl_pct=gross-book.cost_pct/100))
                    continue
                if blk:
                    candidate = block.candidate(pos, px, at, None, book.cost_pct, book.lookback * 60,
                                                floor=book.min_pf, honor_tp=True)
                    if not candidate or not allowed(i, at):
                        continue
                    q = candidate["qty"]
                    pos.block_fills.append(dict(group=candidate["group"], qty=q, price=px, t=at))
                elif dca:
                    if additions[i]:
                        continue
                    distance = min(max(dca["fraction"]*sl, book.cost_pct/100), .9*sl)
                    if sign*(1-px/entry) < distance or sign*(px-pos.sl) <= 0 or not allowed(i, at):
                        continue
                    if dca["bounce"]:
                        index = bisect.bisect_left(times[row["symbol"]], at) - 1
                        prior = bars[row["symbol"]]
                        if index < 1 or sign*(prior[index][3]-prior[index-1][3]) <= 0:
                            continue
                    if not dbook.score(Pulse.dca_scope(pos))["active"]:
                        continue
                    q = dca["ratio"]
                else:
                    continue
                additions[i].append((px,q))
                pos.entry = (pos.entry*pos.qty+px*q)/(pos.qty+q)
                pos.qty += q
                n_adds += 1
        out[label] = {**metrics(closes), "adds":n_adds, "sized":sized,
                      "held":sum(bool(r.get("open_at_end")) for r in settles)}
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    for arg in ("market", "receipts", "cache", "out"):
        ap.add_argument("--"+arg, required=True)
    ap.add_argument("--symbols", type=int, required=True)
    parameters = ap.parse_args()
    result = study(parameters.market, parameters.receipts, parameters.cache, parameters.symbols, list(variants()))
    Path(parameters.out).write_text(json.dumps(result, indent=2)+"\n")

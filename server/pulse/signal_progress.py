"""Event-driven Signals Base -> Main -> Real progression.

Base histories are shared only by mathematically identical entry/protection
streams. Each Base rule owns a separate Main simulation; each (Base, Main)
rule pair owns a separate Real simulation. Children are allocated on their
first eligible entry. Unallocated children have zero evidence, never a pass.
No best-window selection, cross-source pooling or borrowed sibling results.
Execution candidates are exact lane identities; this module sends no orders.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
import hashlib
import json
import math

from signal_replay import Position, open_position, advance_position, close_result, canonical
from signal_windows import History, rules, positive, STRATEGY_RANGES, SIGNAL_MIN_PF, policy_listing


def distance_checks(p, fees):
    required = {"id", "kind", "strategyRange", "tp", "sl", "trailArm", "trailGap", "holdMinutes"}
    if set(p) != required or p["strategyRange"] not in STRATEGY_RANGES:
        raise ValueError("Signals strategy progress permits only Signals and Exit ranges")
    if not isinstance(p["id"], str) or not p["id"] or p["kind"] not in ("normal", "trailing", "trailing-only"):
        raise ValueError("unsupported Signal protection identity/type")
    for k in ("tp", "sl", "trailArm", "trailGap"):
        if type(p[k]) not in (int, float) or not math.isfinite(p[k]) or not 0 <= p[k] < 1:
            raise ValueError("invalid Signal distance fraction")
    if type(p["holdMinutes"]) is not int or not 1 <= p["holdMinutes"] <= 4320:
        raise ValueError("Signal hold must be explicit and bounded")
    # The worst directional fee/slippage break-even distance follows the same
    # two-leg notional convention as fills; an approximate sum understates it.
    a, b, s = fees[fees["entryLiquidity"]], fees[fees["exitLiquidity"]], fees["slippagePerLeg"]
    long_break_even = (1+a)*(1+s)/((1-b)*(1-s))-1
    short_break_even = 1-(1-a)*(1-s)/((1+b)*(1+s))
    minimum = max(long_break_even, short_break_even)
    if not minimum < p["sl"] <= .05:
        raise ValueError("Signal stop must exceed costs and stay at or below 5%")
    if p["kind"] == "normal":
        if p["tp"] <= minimum or p["trailArm"] or p["trailGap"]:
            raise ValueError("normal Signal requires cost-covering TP and no trailing")
    else:
        if not minimum < p["trailGap"] <= .05 or p["trailArm"]-p["trailGap"] <= minimum:
            raise ValueError("Signal trailing gap and locked-in activation margin must exceed costs")
        if p["kind"] == "trailing-only" and p["tp"] != 0:
            raise ValueError("trailing-only must have TP=0")
        if p["kind"] == "trailing" and p["tp"] <= p["trailArm"]:
            raise ValueError("trailing TP must be beyond activation")
    return {"roundTripBreakEven": minimum, "minimumStop": minimum, "minimumTrail": minimum,
            "activationNetMargin": p["trailArm"]-p["trailGap"]-minimum if p["trailGap"] else None}


@dataclass(slots=True)
class Node:
    history: History
    position: Position | None = None
    opened: int = 0


@dataclass(slots=True)
class Root:
    id: str
    identity: dict
    protection: dict
    base: Node
    main: dict = field(default_factory=dict)
    real: dict = field(default_factory=dict)


class SignalProgress:
    def __init__(self, config):
        required = {"base", "main", "realLastN", "minPf", "historyMinutes", "maxContextDrawdown", "fees"}
        if set(config) != required:
            raise ValueError("unknown/missing Signals progression controls")
        self.config = json.loads(canonical(config))
        self.base_rules, self.main_rules = rules(config["base"]), rules(config["main"])
        if not self.base_rules or not self.main_rules:
            raise ValueError("Signals progression has no legal long/short pair")
        real_n = config["realLastN"]
        if not ((type(real_n) is int and 1 <= real_n <= 200) or
                (type(real_n) is str and real_n in ("lane-minimal", "coordinated"))):
            raise ValueError("invalid Signals Real last N")
        if type(config["minPf"]) not in (int,float) or not math.isfinite(config["minPf"]) or config["minPf"] < SIGNAL_MIN_PF:
            raise ValueError("Signals use a common PF floor >= 2.2 at every positive gate")
        if type(config["historyMinutes"]) is not int or not 1 <= config["historyMinutes"] <= 43200:
            raise ValueError("invalid Signals history minutes")
        if type(config["maxContextDrawdown"]) not in (int,float) or not 0 < config["maxContextDrawdown"] <= 1:
            raise ValueError("invalid Signals context drawdown")
        f = config["fees"]
        if set(f) != {"maker","taker","slippagePerLeg","entryLiquidity","exitLiquidity","source"}:
            raise ValueError("Signal costs require complete provenance")
        if f["source"] != "configured-scenario" or any(f[k] not in ("maker","taker") for k in ("entryLiquidity","exitLiquidity")):
            raise ValueError("unsupported Signal fee provenance/liquidity")
        if any(type(f[k]) not in (int,float) or not math.isfinite(f[k]) or not 0 <= f[k] < .1 for k in ("maker","taker","slippagePerLeg")):
            raise ValueError("invalid Signal costs")
        self.main_by_id = {r.id: r for r in self.main_rules}
        self.capacity = max(real_n if type(real_n) is int else 0,
                            *(r.long+r.minimal for r in (*self.base_rules,*self.main_rules)))
        self.roots, self.events, self.active, self.last_bars = {}, {}, {}, {}
        self.history_ms = config["historyMinutes"]*60000
        self.floor, self.max_dd, self.fees = config["minPf"], config["maxContextDrawdown"], f
        self.version = hashlib.sha256(canonical(config).encode()).hexdigest()
        self.steps = self.event_visits = 0

    def node(self):
        return Node(History(self.capacity))

    def register(self, identity, protection):
        if self.steps:
            raise ValueError("register the complete source/profile universe before progression starts")
        if set(identity) != {"symbol", "source", "sourceRange", "tf", "side", "coordination"}:
            raise ValueError("incomplete Signals source identity")
        if (identity["side"] not in (-1,1) or identity["tf"] not in (1,5,15,30) or
                identity["sourceRange"] not in ("short","medium","combined") or
                identity["coordination"] not in ("source","dominating")):
            raise ValueError("invalid Signal source range/timeframe/side/coordination")
        if any(not isinstance(identity[k],str) or not identity[k] for k in ("symbol","source")):
            raise ValueError("invalid Signals symbol/source")
        distance_checks(protection, self.fees)
        ident, p = json.loads(canonical(identity)), json.loads(canonical(protection))
        rid = hashlib.sha256(canonical({"source":ident,"protection":p}).encode()).hexdigest()[:24]
        if rid in self.roots:
            raise ValueError("duplicate Signals profile")
        root = Root(rid, ident, p, self.node())
        self.roots[rid] = root
        key = self.event_key(ident)
        self.events.setdefault(key, {}).setdefault(ident["side"], []).append(root)
        return rid

    @staticmethod
    def event_key(identity):
        return (identity["symbol"], identity["source"], identity["sourceRange"], identity["tf"], identity["coordination"])

    def allowed(self, node, t, policy):
        return node.history.evaluate(t,self.history_ms,policy,self.floor,self.max_dd)

    def real_pass(self,node,t,main_rule,main_evidence):
        """Execution admission, distinct from the Real stream's measured PF.

        Coordinated mode uses the complete, current Main long/short/minimal
        proof already attached to this exact lane. It does not re-test a fixed
        number of closed Real positions. Dynamic mode tests this lane's own
        minimal count; integer mode retains an explicit fixed-count gate.
        Neither mode borrows another lane or changes recorded returns.
        """
        n = self.config["realLastN"]
        if n == "coordinated":
            if main_evidence is None:
                return False, {"validationStage":"main", "validationMode":n}
            return True, {**main_evidence["long"], "validationStage":"main", "validationMode":n,
                          "rule":main_rule, "floor":self.floor}
        if n == "lane-minimal":
            n = self.main_by_id[main_rule].minimal
        m = node.history.window(t,self.history_ms,n)
        return positive(m,n,self.floor,self.max_dd), {**m,"validationStage":"real",
            "validationMode":self.config["realLastN"],"requiredN":n,"floor":self.floor}

    def step(self, symbol, bar, events):
        """Consume exactly one new completed minute and its prior-close signals.

        `events` maps full event keys to directions known at bar.open time.
        The caller must generate events from completed bars only. Returns
        exact Real-qualified candidates before advancing this minute's exits.
        Identical duplicate delivery is idempotent; history rewrites fail.
        """
        if len(bar)!=6 or any(not math.isfinite(x) for x in bar):
            raise ValueError("invalid Signals bar")
        t,o,h,l,c,v=bar
        if t%60000 or min(o,l,c)<=0 or l>min(o,c) or h<max(o,c) or v<0:
            raise ValueError("invalid Signals OHLCV")
        prior=self.last_bars.get(symbol)
        fingerprint=canonical({"bar":bar,"events":sorted((list(k),v) for k,v in events.items())})
        if prior:
            if t==prior[0] and fingerprint==prior[1]:return []
            if t!=prior[0]+60000:raise ValueError("Signals history rewrite/gap requires a new replay")
        if any(k not in self.events or k[0]!=symbol or type(side) is not int or side not in (-1,1) for k,side in events.items()):
            raise ValueError("event outside registered Signal universe")
        self.last_bars[symbol]=(t,fingerprint)
        self.steps+=1
        candidates=[]
        active=self.active.setdefault(symbol,{})

        def enter(root,node,key):
            if node.position is None:
                node.position=open_position(t,o,root.identity["side"],root.protection,self.fees)
                node.opened+=1
                active[key]=(root,node)

        for event_key,side in events.items():
            for root in self.events[event_key].get(side,[]):
                self.event_visits+=1
                base=self.allowed(root.base,t,self.base_rules)
                for br,bf in base.items():
                    main=root.main.get(br)
                    if main is None:
                        main=root.main[br]=self.node()
                    passed_main=self.allowed(main,t,self.main_rules)
                    for mr,mf in passed_main.items():
                        path=(br,mr)
                        real=root.real.get(path)
                        if real is None:real=root.real[path]=self.node()
                        passed,rf=self.real_pass(real,t,mr,mf)
                        if passed:
                            cid=f"{root.id}/{br}/{mr}"
                            candidates.append({"id":cid,"root":root.id,"identity":root.identity,"protection":root.protection,
                                "baseRule":br,"mainRule":mr,"base":bf,"main":mf,"real":rf,"decisionT":t})
                        enter(root,real,(root.id,"real",br,mr))
                    enter(root,main,(root.id,"main",br))
                enter(root,root.base,(root.id,"base"))

        for key,(root,node) in list(active.items()):
            opposite=events.get(self.event_key(root.identity)) == -root.identity["side"]
            if root.protection["strategyRange"]=="exit" and opposite and node.position.entry_t<t:
                result=close_result(node.position,o,t,"source-exit",self.fees)
            else:
                result=advance_position(node.position,bar,root.protection,self.fees)
            if result is not None:
                node.history.record(result["exitT"],result["unitNet"])
                node.position=None
                del active[key]
        return candidates

    def summary(self, now, detail=False):
        nb,nm=len(self.base_rules),len(self.main_rules)
        counts={s:{"population":len(self.roots)*(nb if s=="base" else nb*nm),"passed":0,"chainPassed":0,
                   "allocatedStreams":0,"closed":0,"gain":0.0,"loss":0.0,"windowGain":0.0,"windowLoss":0.0} for s in ("base","main","real")}
        records=[]
        for root in self.roots.values():
            base=self.allowed(root.base,now,self.base_rules)
            counts["base"]["passed"]+=len(base);counts["base"]["chainPassed"]+=len(base)
            for stage,nodes in (("base",[(None,root.base)]),("main",root.main.items()),("real",root.real.items())):
                for path,node in nodes:
                    cumulative=node.history.cumulative()
                    count=counts[stage];count["allocatedStreams"]+=1;count["closed"]+=cumulative["n"]
                    count["gain"]+=cumulative["gain"];count["loss"]+=cumulative["loss"]
                    if stage=="base":passed=base;chain=len(base)
                    elif stage=="main":
                        passed=self.allowed(node,now,self.main_rules);chain=len(passed) if path in base else 0
                        count["passed"]+=len(passed);count["chainPassed"]+=chain
                    else:
                        main_passed=self.allowed(root.main[path[0]],now,self.main_rules)
                        ok,metric=self.real_pass(node,now,path[1],main_passed.get(path[1]))
                        passed={"real":metric} if ok else {}
                        chain=int(ok and path[0] in base and path[1] in main_passed)
                        count["passed"]+=int(ok);count["chainPassed"]+=chain
                    for p in passed.values():
                        m=p if stage=="real" else p["long"]
                        count["windowGain"]+=m["gain"];count["windowLoss"]+=m["loss"]
                    if detail:
                        records.append({"root":root.id,"identity":root.identity,"protection":root.protection,"stage":stage,
                            "path":path,"passedRules":passed,"chainPassed":chain,"opened":node.opened,"open":node.position is not None,
                            "cumulative":cumulative})
        for count in counts.values():
            count["validatedPct"]=100*count["passed"]/count["population"] if count["population"] else 0
            count["pf"]=count["gain"]/count["loss"] if count["loss"] else None
            count["passingWindowPf"]=count["windowGain"]/count["windowLoss"] if count["windowLoss"] else None
            count["passingWindowState"]="finite" if count["windowLoss"] else "no-loss" if count["windowGain"] else "empty"
        result={"at":now,"stages":counts,"roots":len(self.roots),"possibleExecutionLanes":len(self.roots)*nb*nm,
                "activeShadowPositions":sum(len(v) for v in self.active.values()),"eventVisits":self.event_visits,
                "basePolicy":policy_listing(self.config["base"]),"mainPolicy":policy_listing(self.config["main"]),
                "minPf":self.floor,"realLastN":self.config["realLastN"],
                "realValidationStage":"main" if self.config["realLastN"]=="coordinated" else "real",
                "historyMinutes":self.config["historyMinutes"],"baseRuleCount":nb,"mainRuleCount":nm,
                "strategyRanges":list(STRATEGY_RANGES)}
        if detail:result["records"]=records
        return result

    def checkpoint(self):
        def node_dump(node):
            h=node.history
            return {"times":h.times,"values":h.values,"count":h.count,"gain":h.gain,"loss":h.loss,
                    "opened":node.opened,"position":asdict(node.position) if node.position else None}
        return json.loads(canonical({"schema":"signals-progress-state-v1","config":self.config,"version":self.version,
            "steps":self.steps,"eventVisits":self.event_visits,"lastBars":self.last_bars,
            "roots":[{"id":r.id,"identity":r.identity,"protection":r.protection,"base":node_dump(r.base),
                      "main":[[k,node_dump(v)] for k,v in r.main.items()],
                      "real":[[list(k),node_dump(v)] for k,v in r.real.items()]} for r in self.roots.values()]}))

    @classmethod
    def restore(cls,state):
        if state.get("schema")!="signals-progress-state-v1":raise ValueError("unknown Signals checkpoint")
        book=cls(state["config"])
        if book.version!=state["version"]:raise ValueError("Signals checkpoint configuration changed")
        base_ids={r.id for r in book.base_rules};main_ids={r.id for r in book.main_rules}
        def read_node(raw,root,key):
            node=book.node();h=node.history
            if len(raw["times"])!=len(raw["values"]) or len(raw["times"])>h.capacity:raise ValueError("invalid Signal history tail")
            for t,v in zip(raw["times"],raw["values"]):h.record(t,v)
            if raw["count"]<len(h.times) or any(not math.isfinite(raw[k]) or raw[k]<0 for k in ("gain","loss")):
                raise ValueError("invalid Signal history totals")
            h.count,h.gain,h.loss=raw["count"],raw["gain"],raw["loss"]
            node.opened=raw["opened"]
            if raw["position"]:
                node.position=Position(**raw["position"])
                if node.position.side!=root.identity["side"] or any(not math.isfinite(v) for v in raw["position"].values()):
                    raise ValueError("invalid Signals checkpoint position")
                book.active.setdefault(root.identity["symbol"],{})[key]=(root,node)
            return node
        for saved in state["roots"]:
            rid=book.register(saved["identity"],saved["protection"])
            if rid!=saved["id"]:raise ValueError("Signals checkpoint identity changed")
            root=book.roots[rid];root.base=read_node(saved["base"],root,(rid,"base"))
            for br,raw in saved["main"]:
                if br not in base_ids or br in root.main:raise ValueError("invalid Main path")
                root.main[br]=read_node(raw,root,(rid,"main",br))
            for pair,raw in saved["real"]:
                br,mr=pair
                if br not in root.main or mr not in main_ids or (br,mr) in root.real:raise ValueError("invalid Real path")
                root.real[br,mr]=read_node(raw,root,(rid,"real",br,mr))
        book.last_bars={k:tuple(v) for k,v in state["lastBars"].items()}
        book.steps=state["steps"];book.event_visits=state["eventVisits"]
        return book

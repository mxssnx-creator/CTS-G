"""Own indication configuration × exit-grid evidence and stale publication."""
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from indication_grid import ColumnarTape, IndicationGrid, lane_set_id
from set_engine import KindSignals, SetBook, hist_fill


OPTIONS = dict(histLookbackBars=360, histMinBars=60, histWarmup=16,
    setMinStep=7, setStepMax=8, slToTpRatios=[.4, .6], stratGeneral=False,
    stratTrailing=True, trailArmMin=.3, trailArmMax=.3, trailGiveMin=.2, trailGiveMax=.2,
    baseEvalPosCount=5, mainEvalPosCount=5, realEvalPosCount=5, microEnabled=False,
    histSimulateBlock=False, histSimulateDca=False, positionCostPct=.1)


def book():
    b = SetBook(); b.load(OPTIONS)
    bars = [[100+i*.3, 100.35+i*.3, 99.99+i*.3, 100.3+i*.3, 1000] for i in range(360)]
    b.ingest_bars("X-USDT", bars)
    b.progress.ready = True
    b.last_run = 1_800_000_000
    return b


def signals(b):
    n = len(b.bars["X-USDT"])
    return ({}, KindSignals({"signals|direct_tf:1m": [(1,.9)]*n,
                             "signals|direct_tf:5m": [(-1,.9)]*n}), 16)


class IndicationGridTests(unittest.TestCase):
    def grid(self, b):
        g = IndicationGrid(b); b._indication_grid = g
        return g

    def test_full_normal_trail_sl_tp_grid_uses_own_signal_and_directions(self):
        b = book(); g = self.grid(b)
        templates = list(b.by_idx)
        with patch.object(SetBook, "prepare_replay_signals", return_value=signals(b)):
            g.calculate("signals", "direct_tf:1m")
            g.calculate("signals", "direct_tf:5m")
        good = g.jobs[("signals", "direct_tf:1m", "X-USDT")]
        bad = g.jobs[("signals", "direct_tf:5m", "X-USDT")]
        self.assertEqual(good["requested"], len(templates))
        self.assertEqual(good["completed"], len(templates))
        self.assertEqual(good["phase"], "ready")
        self.assertEqual(len(set(good["ids"]) & set(bad["ids"])), 0)
        own = [b.sets[s] for s in good["ids"]]
        other = [b.sets[s] for s in bad["ids"]]
        self.assertTrue(any(b.execution_allowed(s, "indications", "LONG") for s in own))
        self.assertFalse(any(b.execution_allowed(s, "indications", "SHORT") for s in own))
        self.assertFalse(any(b.execution_allowed(s, "indications", "SHORT") for s in other))
        self.assertTrue(all(s.hist for s in own+other))
        self.assertTrue(all(s.last15_n == 0 for s in templates))
        self.assertEqual({(s.sl_ratio,s.step,s.trail_key) for s in own},
                         {(s.sl_ratio,s.step,s.trail_key) for s in templates})
        trailing = next(s for s in own if s.kind == "trail")
        self.assertIn(trailing.parent_set_id, good["ids"])
        self.assertIsNot(b.sets[trailing.parent_set_id].hist, trailing.hist)

    def test_completed_tiles_resume_without_recomputing_prior_tiles(self):
        b = book(); g = self.grid(b); g.TILE = 1
        with patch.object(SetBook,"prepare_replay_signals",return_value=signals(b)), \
                patch.object(SetBook,"_replay_core_vectorized",wraps=b._replay_core_vectorized) as calc:
            g.calculate("signals","direct_tf:1m",tiles=1)
            job = g.jobs[("signals","direct_tf:1m","X-USDT")]
            first = list(job["ids"])
            self.assertEqual(job["completedBehaviors"],1)
            self.assertEqual(job["completed"],len(first))
            self.assertEqual(job["phase"],"calculating")
            g.calculate("signals","direct_tf:1m",tiles=1)
            self.assertEqual(job["completedBehaviors"],2)
            self.assertEqual(job["ids"][:len(first)],first)
            self.assertEqual(calc.call_count,2)
            g.calculate("signals","direct_tf:1m")
            self.assertEqual(job["completed"],job["requested"])

    def test_changed_bar_input_retires_prior_qualification_immediately(self):
        b=book();g=self.grid(b)
        with patch.object(SetBook,"prepare_replay_signals",return_value=signals(b)):
            g.calculate("signals","direct_tf:1m")
            job=g.jobs[("signals","direct_tf:1m","X-USDT")]
            own=next(b.sets[s] for s in job["ids"] if b.execution_allowed(b.sets[s],"indications","LONG"))
            changed=[list(r) for r in b.bars["X-USDT"]]
            changed[-1][3]+=.001
            b.ingest_bars("X-USDT",changed)
            self.assertFalse(b.execution_allowed(own,"indications","LONG"))
            g.calculate("signals","direct_tf:1m")
            self.assertTrue(b.execution_allowed(own,"indications","LONG"))
            self.assertEqual(len(b.by_idx),16)

    def test_new_generation_cannot_publish_inflight_old_results(self):
        b=book();g=self.grid(b)
        old=g.request("signals","direct_tf:1m",background=False)
        b.load({**OPTIONS,"setMinPf":1.2})
        with patch.object(SetBook,"prepare_replay_signals",return_value=signals(b)):
            g.calculate("signals","direct_tf:1m",job=old)
        self.assertEqual(old["completed"],0)
        self.assertFalse(any(s.indication_config for s in b.by_idx))

    def test_core_history_commit_never_erases_or_replays_independent_tapes(self):
        b=book();g=self.grid(b)
        with patch.object(SetBook,"prepare_replay_signals",return_value=signals(b)):
            g.calculate("signals","direct_tf:1m")
        job=g.jobs[("signals","direct_tf:1m","X-USDT")]
        st=b.sets[job["ids"][0]];expected=list(st.hist)
        b._commit_hist({},merge=False,score=False)
        self.assertEqual([dict(r) for r in st.hist],[dict(r) for r in expected])
        clone=b.replay_clone(["X-USDT"])
        self.assertFalse(any(s.indication_config for s in clone.by_idx))
        self.assertIsNone(clone._indication_grid)

    def test_unavailable_exact_config_has_no_evidence_and_cannot_borrow(self):
        b=book();g=self.grid(b)
        with patch.object(SetBook,"prepare_replay_signals",return_value=signals(b)):
            g.calculate("signals","direct_tf:15m")
        job=g.jobs[("signals","direct_tf:15m","X-USDT")]
        self.assertEqual((job["phase"],job["completed"],job["ids"]),("no-evidence",0,[]))

    def test_interior_corrections_and_compact_config_tags_invalidate_cache(self):
        b=book()
        b.ind_hist["trend"]=[hist_fill(1000+i,"X",1,.003,60,"tp") for i in range(8)]
        for row in b.ind_hist["trend"]:row["ind_config"]="trend:21"
        first=b.ind_config_stats("trend","trend:21","LONG")
        self.assertTrue(first["validated"])
        b.ind_hist["trend"][-3]["pnl_pct"]=-.3
        corrected=b.ind_config_stats("trend","trend:21","LONG")
        self.assertFalse(corrected["validated"])
        self.assertLess(corrected["pf"],first["pf"])
        b.ind_hist["trend"][-3]["pnl_pct"] = .003
        b.ind_hist["trend"][-3]["ind_config"]="trend:34"
        self.assertEqual(b.ind_config_stats("trend","trend:34","LONG")["n"],1)

    def test_short_exact_tape_never_inherits_a_successful_sibling(self):
        b=book()
        b.ind_hist["trend"]=[dict(t=1000+i,symbol="X",side="LONG",pnl_pct=.003,
            hold_s=60,reason="tp",ind_config="trend:21") for i in range(30)]
        self.assertTrue(b.indication_ok("trend","LONG","trend:21"))
        self.assertFalse(b.indication_ok("trend","LONG","trend:34"))

    def test_trailing_high_never_overrides_an_earlier_possible_stop(self):
        b=book()
        pos=b._seed_pos(1,100,99,110,0,"fixture")
        _,close=b._advance_pos(pos,[100,105,98,104,1000],1,sl_frac=.01,tp_frac=.1,
            use_trail=True,arm=.01,give=.01,time_bars=45,scratch_bars=45,honor_tp=True,
            ts=1000,symbol="X",st_id="own",pack="indications",strategy="core")
        self.assertEqual(close["reason"],"sl")
        self.assertAlmostEqual(close["pnl_pct"],-.01)

    def test_timestamped_ingress_preserves_the_actual_final_bar_open(self):
        b=book();t=1_800_000_000
        rows=[[t+i*60,*r] for i,r in enumerate(b.bars["X-USDT"])]
        b.ingest_bars("X-USDT",rows)
        self.assertEqual(b._bar_end_ts["X-USDT"],t+359*60)
        self.assertEqual(len(b.bars["X-USDT"][-1]),5)

    def test_columnar_tape_preserves_exact_returns_and_independent_identity(self):
        import pickle
        rows=[hist_fill(1000+i,"X" if i%2 else "Y",1 if i%2 else -1,
                        .0000123456789*i,60*i,"tp" if i%2 else "sl",ind_kind="signals")
              for i in range(96)]
        for row in rows:row["ind_config"]="direct_tf:5m"
        tape=ColumnarTape(rows,"signals","direct_tf:5m")
        self.assertEqual([dict(r) for r in tape],[dict(r) for r in rows])
        restored=pickle.loads(pickle.dumps(tape))
        self.assertEqual([dict(r) for r in restored],[dict(r) for r in rows])
        self.assertLess(tape.bytes,3000)
        tape[0]["pnl_pct"]=-99
        self.assertEqual(tape[0]["pnl_pct"],rows[0]["pnl_pct"])

    def test_restart_restores_own_identity_and_credits_one_confirmed_close(self):
        b=book();template=b.by_idx[0]
        sid=lane_set_id("signals","direct_tf:5m",template.id,"X-USDT")
        rec=dict(t=1800000000,symbol="X-USDT",side="LONG",pnl_pct=.003,
                 hold_s=60,reason="tp",set_id=sid,ind_kind="signals",ind_config="direct_tf:5m",
                 strategy="core",client_id="own",close_fill_id="fill",exchange_confirmed=True)
        b.on_live_close(rec);b.on_live_close(rec)
        self.assertIn(sid,b.sets)
        own=b.sets[sid]
        self.assertEqual(own.indication_config,"direct_tf:5m")
        self.assertEqual(len(own.live),1)
        self.assertEqual(template.live,[])
        self.assertFalse(b.execution_allowed(own,"indications","LONG"))

    def test_symbols_do_not_borrow_evidence_or_invalidate_unchanged_sibling(self):
        b=book()
        b.ingest_bars("Y-USDT",[[250-i*.3,250.01-i*.3,249.65-i*.3,249.7-i*.3,1000]
                               for i in range(360)])
        g=self.grid(b)
        with patch.object(SetBook,"prepare_replay_signals",return_value=signals(b)):
            g.calculate("signals","direct_tf:1m","X-USDT")
            g.calculate("signals","direct_tf:1m","Y-USDT")
        x=[b.sets[s] for s in g.jobs[("signals","direct_tf:1m","X-USDT")]["ids"]]
        y=[b.sets[s] for s in g.jobs[("signals","direct_tf:1m","Y-USDT")]["ids"]]
        self.assertFalse(set(s.id for s in x)&set(s.id for s in y))
        qualified=next(s for s in x if b.execution_allowed(s,"indications","LONG"))
        self.assertFalse(any(b.execution_allowed(s,"indications","LONG") for s in y))
        self.assertTrue(all({r["symbol"] for r in s.hist}=={"X-USDT"} for s in x))
        self.assertTrue(all({r["symbol"] for r in s.hist}=={"Y-USDT"} for s in y))
        changed=[list(r) for r in b.bars["Y-USDT"]];changed[-1][3]+=.001
        b.ingest_bars("Y-USDT",changed)
        self.assertTrue(b.execution_allowed(qualified,"indications","LONG"))
        with patch.object(SetBook,"prepare_replay_signals",return_value=signals(b)):
            g.calculate("signals","direct_tf:1m")
        self.assertEqual(g.jobs[("signals","direct_tf:1m","")]["phase"],"no-evidence")

    def test_zero_cost_fast_historic_pf_matches_canonical_actual_return(self):
        from position_cost import last_n_cost_pf
        b=book();b.load({**OPTIONS,"positionCostPct":0})
        rows=[hist_fill(1000+i,"X",1,.002,60,"tp") for i in range(10)]
        canonical=last_n_cost_pf(rows,5,0)
        fast=b._fast_historic_pf(rows,5)
        self.assertEqual(fast["ratio"],canonical["ratio"])
        self.assertEqual(fast["netPct"],.2)
        self.assertGreater(fast["ratio"],1)

    def test_dca_replay_settings_match_native_configured_steps(self):
        from dca_engine import DcaBook
        opts={**OPTIONS,"dcaMaxSteps":7,"dcaStepDistancesPct":[1.5,2.0],
              "dcaStepVolumeMultipliers":[1,1.2],"dcaCooldownSeconds":90}
        b=book();b.load(opts);d=DcaBook();d.load(opts)
        self.assertEqual(b.dca_dist,d.distances)
        self.assertEqual(b.dca_mult,d.mults)
        self.assertEqual((b.dca_max_steps,b.dca_cooldown_s),(d.max_steps,d.cooldown_s))

    def test_identical_resolved_ranges_share_only_immutable_historic_results(self):
        b=book();g=self.grid(b)
        with patch.object(SetBook,"prepare_replay_signals",return_value=signals(b)):
            g.calculate("signals","direct_tf:1m")
        job=g.jobs[("signals","direct_tf:1m","X-USDT")]
        self.assertLess(job["requestedBehaviors"],job["requested"])
        own=[b.sets[s] for s in job["ids"]]
        pairs=[(a,z) for a in own for z in own if a.id!=z.id and a.hist is z.hist]
        self.assertTrue(pairs)
        a,z=pairs[0]
        for i in range(5):
            b.on_live_close(dict(t=1800000000+i,symbol="X-USDT",side="LONG",pnl_pct=-.05,
                hold_s=60,reason="sl",set_id=a.id,ind_kind="signals",ind_config="direct_tf:1m",
                strategy="trailing" if a.kind=="trail" else "core",exchange_confirmed=True,
                client_id=str(i),close_fill_id="own"+str(i)))
        self.assertFalse(b.execution_allowed(a,"indications","LONG"))
        self.assertTrue(b.execution_allowed(z,"indications","LONG"))
        self.assertEqual(z.live,[])

    def test_unchanged_ingress_reuses_running_generation_and_symbol_signal_cache(self):
        b=book();g=self.grid(b);rows=b.bars["X-USDT"]
        with patch.object(SetBook,"prepare_replay_signals",return_value=signals(b)) as prepare:
            g.calculate("signals","direct_tf:1m",tiles=1)
            token=g.token("X-USDT")
            b.ingest_bars("X-USDT",[list(r) for r in rows])
            self.assertIs(b.bars["X-USDT"],rows)
            self.assertEqual(g.token("X-USDT"),token)
            g.calculate("signals","direct_tf:5m",tiles=1)
            self.assertEqual(prepare.call_count,1)

    def test_native_general_qualifies_each_symbol_with_its_own_pack_signal(self):
        b=book();b.load({**OPTIONS,"stratIndications":False,"stratGeneral":True})
        b.progress.ready=True
        b.ingest_bars("Y-USDT",[[250-i*.3,250.01-i*.3,249.65-i*.3,249.7-i*.3,1000]
                               for i in range(360)])
        g=self.grid(b)
        prepared=({"general":[(1,.9,"own-general")]*360},KindSignals(),16)
        with patch.object(SetBook,"prepare_replay_signals",return_value=prepared):
            g.calculate("general","general_signal","X-USDT")
            g.calculate("general","general_signal","Y-USDT")
        x=[b.sets[s] for s in g.jobs[("general","general_signal","X-USDT")]["ids"]]
        y=[b.sets[s] for s in g.jobs[("general","general_signal","Y-USDT")]["ids"]]
        self.assertTrue(any(b.execution_allowed(s,"general","LONG") for s in x))
        self.assertFalse(any(b.execution_allowed(s,"general","LONG") for s in y))
        self.assertTrue(all(s.pack=="general" and s.indication_symbol=="X-USDT" for s in x))

    def test_enabled_native_adjustments_publish_own_evidence_and_counts(self):
        b=book();b.load({**OPTIONS,"stratTrailing":False,"setStepMax":7,"slToTpRatios":[.6],
            "histSimulateBlock":True,"histSimulateDca":True,"dcaEnabled":True,
            "axisLastEnabled":True,"axisLastMaxWindow":1})
        b.progress.ready=True;g=self.grid(b)
        with patch.object(SetBook,"prepare_replay_signals",return_value=signals(b)):
            g.calculate("signals","direct_tf:1m")
        job=g.jobs[("signals","direct_tf:1m","X-USDT")]
        self.assertEqual((job["requested"],job["completed"]),(1,1))
        self.assertGreater(job["derivedByKind"].get("block",0),0)
        self.assertGreater(job["derivedByKind"].get("dca",0),0)
        self.assertGreater(job["derivedByKind"].get("axis",0),0)
        parent=next(b.sets[s] for s in job["ids"] if b.sets[s].kind=="base")
        children=[b.sets[s] for s in job["ids"] if b.sets[s].kind in ("block","dca","axis")]
        self.assertTrue(all(c.parent_set_id==parent.id for c in children))
        self.assertTrue(all(c.indication_symbol=="X-USDT" for c in children))
        dca=next(c for c in children if c.kind=="dca")
        self.assertEqual(len(dca.hist),0)
        self.assertFalse(b.execution_allowed(dca,"indications","LONG"))
        self.assertTrue(all(g.current(c) for c in children))

    def test_background_jobs_rotate_tiles_before_finishing_one_lane(self):
        b=book();g=self.grid(b);g.TILE=1
        g.request("signals","direct_tf:1m",background=False)
        g.request("signals","direct_tf:5m",background=False)
        with patch.object(SetBook,"prepare_replay_signals",return_value=signals(b)), \
             patch.object(g,"calculate",wraps=g.calculate) as calculate:
            g._run()
        order=[call.args[1] for call in calculate.call_args_list]
        self.assertEqual(order[:4],["direct_tf:1m","direct_tf:5m","direct_tf:1m","direct_tf:5m"])
        self.assertTrue(all(job["phase"]=="ready" for job in g.jobs.values()))

    def test_bounded_parallel_workers_publish_only_their_own_configurations(self):
        b=book();b.system_workers=2;g=self.grid(b);g.TILE=1
        with patch.object(SetBook,"prepare_replay_signals",return_value=signals(b)):
            g.request("signals","direct_tf:1m")
            g.request("signals","direct_tf:5m")
            for worker in list(g.workers):worker.join(timeout=3)
        self.assertLessEqual(len(g.workers),2)
        self.assertTrue(all(job["phase"]=="ready" for job in g.jobs.values()))
        self.assertTrue(all(b.sets[s].indication_config==job["config"]
                            for job in g.jobs.values() for s in job["ids"]))

    def test_progress_qualification_counts_update_after_one_own_live_loss(self):
        b=book();g=self.grid(b)
        with patch.object(SetBook,"prepare_replay_signals",return_value=signals(b)):
            g.calculate("signals","direct_tf:1m")
        job=g.jobs[("signals","direct_tf:1m","X-USDT")]
        before=g.snapshot()["configurations"][0]["qualifiedBase"]
        st=next(b.sets[s] for s in job["ids"] if b.sets[s].stage_ledger["base"])
        for i in range(5):
            b.on_live_close(dict(t=1800000000+i,symbol="X-USDT",side="LONG",pnl_pct=-.05,
                hold_s=60,reason="sl",set_id=st.id,ind_kind="signals",ind_config="direct_tf:1m",
                strategy="trailing" if st.kind=="trail" else "core",exchange_confirmed=True,
                client_id=str(i),close_fill_id="loss"+str(i)))
        status=g.snapshot()
        self.assertEqual(status["configurations"][0]["qualifiedBase"],before-1)
        self.assertEqual(status["coverageScope"],"requested-configurations-only")
        self.assertEqual(status["version"],1)


if __name__ == "__main__":unittest.main()

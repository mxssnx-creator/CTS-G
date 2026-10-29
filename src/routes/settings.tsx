import { SetsOverview } from "@/components/sets-overview";
import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  blockTable,
  bool,
  DEFAULT_OVERLAY,
  fetchCtsBundle,
  loadLocalOverlay,
  num,
  overlayFromCts,
  saveOverlay,
  snapSlToTp,
  SL_SET_RATIOS,
  SL_TP_RATIOS,
  TRAIL_VARIANTS,
  trailGiveFromArm,
  type CtsSettings,
  type PulseOverlay,
} from "@/lib/config-model";
import { fetchLiveStats, pickView, type LiveStats } from "@/lib/live-stats";
import { formatDuration } from "@/lib/analytics";
import { createSaveGate } from "@/lib/poll";
import { DeskShell } from "@/components/desk-shell";
import { useConnection } from "@/components/connection-provider";
import { SymbolPicker } from "@/components/symbol-picker";
import { CoveragePanel } from "@/components/coverage-overview";
import { GATE_MIN_PF_DEFAULT, MAX_SYMBOLS } from "@/lib/config-model";

export const Route = createFileRoute("/settings")({ component: SettingsPage });

const SECTIONS = [
  "overview",
  "connection",
  "profit",
  "risk",
  "trailing",
  "timeframes",
  "packs",
  "sets",
  "exits",
  "stages",
  "block",
  "dca",
  "axes",
  "volume",
  "controls",
  "indication",
  "pulse",
  "symbols",
] as const;

function SettingsPage() {
  const { conn } = useConnection();
  const [cts, setCts] = useState<CtsSettings | null>(null);
  const [raw, setRaw] = useState<LiveStats | null>(null);
  const [overlay, setOverlay] = useState<PulseOverlay>(DEFAULT_OVERLAY);
  const [section, setSection] = useState<(typeof SECTIONS)[number]>("overview");
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [ready, setReady] = useState(false);
  const dirtyRef = useRef(false);
  dirtyRef.current = dirty;
  // a poll started before a save must not overwrite the saved values when it returns (see lib/poll.ts)
  const saveGate = useRef(createSaveGate());

  useEffect(() => {
    let alive = true;
    setCts(null);
    setRaw(null);
    setDirty(false);
    dirtyRef.current = false;
    setSaveMsg(null);
    setReady(false);
    const local = loadLocalOverlay(conn);
    setOverlay(overlayFromCts({}, local || {}));
    const pull = async () => {
      const snap = saveGate.current.snapshot();
      const sP = fetchLiveStats(conn);
      const cP = fetchCtsBundle(conn);
      const s = await sP;
      if (!alive) return;
      setRaw(s);
      const c = await cP;
      if (!alive) return;
      setCts(c.cts);
      if (!dirtyRef.current && saveGate.current.fresh(snap)) {
        const stored = loadLocalOverlay(conn);
        setOverlay(overlayFromCts(c.cts ?? {}, { ...(stored || {}), ...(c.overlay || {}) }));
      }
      setReady(true);
    };
    let timer: ReturnType<typeof setTimeout> | null = null;
    const chain = async () => {
      await pull();
      if (alive) timer = setTimeout(chain, 4000);
    };
    void chain();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [conn]);
  const stats = pickView(raw, conn);

  const patch = <K extends keyof PulseOverlay>(k: K, v: PulseOverlay[K]) => {
    dirtyRef.current = true;
    setOverlay((o) => ({ ...o, [k]: v }));
    setDirty(true);
    setSaveMsg(null);
  };

  const coord = (cts?.coordination_settings ?? cts?.coordinationSettings ?? {}) as Record<
    string,
    unknown
  >;
  const strategies = (cts?.strategies ?? {}) as {
    main?: Record<string, { enabled?: boolean; min_profit_factor?: number; max_drawdown_time?: number; max_positions?: number }>;
    mainTradePfRatioSemantics?: string;
  };
  const defaultMinPf = num(strategies.main?.real?.min_profit_factor ?? cts?.realProfitFactor, GATE_MIN_PF_DEFAULT);
  const table = useMemo(
    () => blockTable(overlay.blockVolumeRatio, overlay.blockProfitFactorRatio, defaultMinPf, 1),
    [overlay.blockVolumeRatio, overlay.blockProfitFactorRatio, defaultMinPf],
  );

  const onSave = async (target = conn) => {
    if (target === "overall") {
      setSaveMsg("Pick Live or VST to save");
      return;
    }
    if (!ready) {
      setSaveMsg("Wait for overlay to load");
      return;
    }
    setSaving(true);
    saveGate.current.mark();   // polls that started before this save are now stale
    const r = await saveOverlay(overlay, target);
    setSaving(false);
    setSaveMsg(r.detail);
    if (r.ok) {
      dirtyRef.current = false;
      setDirty(false);
      if (r.overlay) setOverlay((o) => overlayFromCts(cts ?? {}, { ...o, ...r.overlay }));
    }
  };

  return (
    <DeskShell
      live={Boolean(stats?.running && !stats?.halted && !stats?.paused)}
      stale={Boolean(stats?.stale)}
      mode={stats?.paused ? "PAUSED" : stats?.mode}
      paused={Boolean(stats?.paused || stats?.haltReason === "paused")}
      statsType={stats?.connType}
      statsId={stats?.connection}
    >
      <div className="flex flex-col gap-4 lg:flex-row">
        <aside className="lg:sticky lg:top-4 lg:max-h-[calc(100vh-5.5rem)] lg:w-56 lg:shrink-0 lg:overflow-y-auto">
          <nav className="flex gap-1 overflow-x-auto pb-1 lg:flex-col lg:pb-0">
            {SECTIONS.map((id) => (
              <button
                key={id}
                type="button"
                onClick={() => setSection(id)}
                className={`min-h-11 shrink-0 rounded-lg px-3 text-left text-sm capitalize ${
                  section === id ? "bg-bg2 text-fg ring-1 ring-primary" : "text-muted"
                }`}
                data-section={id}
                data-testid={`section-${id}`}
              >
                {id === "dca"
                  ? "DCA"
                  : id === "pulse"
                    ? "Pulse overlay"
                    : id === "profit"
                      ? "Profit factor"
                      : id === "risk"
                        ? "SL / TP ratios"
                        : id === "trailing"
                          ? "Trailing recals"
                          : id === "timeframes"
                            ? "Timeframes"
                            : id === "packs"
                              ? "Strategy packs"
                              : id === "sets"
                                ? "Sets · historic"
                                : id === "exits"
                                  ? "Exits · SL"
                                  : id === "overview"
                                    ? "Overview"
                                    : id === "controls"
                                      ? "Control orders"
                                      : id}
              </button>
            ))}
          </nav>
        </aside>

        <div className="min-w-0 flex-1 space-y-4">
          <LiveApplied conn={conn} stats={stats} overlay={overlay} dirty={dirty} />
          {section === "overview" && (
            <Card title="Coverage · controls · live overviews" hint="Scan, packs, indication types, block counts, set families, recon and order protection — all live">
              <CoveragePanel live={stats} />
              <Grid>
                <EnableSlider label="Indications" on={overlay.stratIndications} onChange={(v) => patch("stratIndications", v)} />
                <EnableSlider label="General pulse" on={overlay.stratGeneral} onChange={(v) => patch("stratGeneral", v)} />
                <EnableSlider label="Short Range" on={overlay.stratShortRange} onChange={(v) => patch("stratShortRange", v)} />
                <EnableSlider label="Trend (Sets and Base evaluation)" on={overlay.stratTrend} onChange={(v) => patch("stratTrend", v)} />
                <EnableSlider label="Breakout (Sets and Base evaluation)" on={overlay.stratBreakout} onChange={(v) => patch("stratBreakout", v)} />
                <EnableSlider label="Reversion (Sets and Base evaluation)" on={overlay.stratReversion} onChange={(v) => patch("stratReversion", v)} />
                <EnableSlider label="Flow (Sets and Base evaluation)" on={overlay.stratFlow} onChange={(v) => patch("stratFlow", v)} />
                <EnableSlider label="Multi (Sets and Base evaluation)" on={overlay.stratMulti} onChange={(v) => patch("stratMulti", v)} />
                <EnableSlider label="Block" on={overlay.stratBlock && overlay.blockEnabled} onChange={(v) => { patch("stratBlock", v); patch("blockEnabled", v); }} />
                <EnableSlider label="Trailing" on={overlay.stratTrailing} onChange={(v) => patch("stratTrailing", v)} />
                <EnableSlider label="DCA" on={overlay.dcaEnabled === true && overlay.stratDca === true} onChange={(v) => { patch("dcaEnabled", v); patch("stratDca", v); }} />
                <EnableSlider label="Control orders" on={overlay.controlOrders} onChange={(v) => patch("controlOrders", v)} />
                <EnableSlider label="Historic sets" on={overlay.histEnabled} onChange={(v) => patch("histEnabled", v)} />
                <EnableSlider label="Exit coordinator" on={overlay.exitEnabled} onChange={(v) => patch("exitEnabled", v)} />
              </Grid>
              <p className="text-sm text-muted">
                These sliders write the overlay. Save on Live or VST persists every field — including DCA steps, modules and symbol universe — and the engine reloads the file.
              </p>
            </Card>
          )}
          {section === "connection" && (
            <Card title="Connection" hint={connHint(conn, stats)}>
              <Grid>
                <KV k="Selected" v={conn === "vst" ? "VST demo" : conn === "live" ? "Live mainnet" : "Overall"} />
                <KV k="Name" v={conn === "vst" ? "BingX X02" : conn === "live" ? "BingX X01" : "All desks"} />
                <KV k="Exchange" v={String(stats?.exchange ?? "BingX")} />
                <KV k="Mode" v={String(stats?.mode ?? "—")} />
                <KV k="Position mode" v={String(cts?.position_mode ?? "hedge")} />
                <KV k="Margin" v={String(cts?.margin_mode ?? "cross")} />
                <KV k="Leverage % (CTS)" v={`${num(cts?.leveragePercentage, 100)}%`} />
                <KV k="Maximal leverage" v={bool(cts?.useMaximalLeverage) ? "on" : "off"} />
                <KV k="Live requested" v={bool(cts?.live_trade_requested) ? "yes" : "no"} />
                <KV k="CTS engine live flag" v={bool(cts?.live_trading_enabled) ? "on" : "off"} />
                <KV k="Close-only" v={bool(cts?.useSystemCloseOnly) ? "on" : "off"} />
                <KV k="PF semantics" v={String(cts?.mainTradePfRatioSemantics ?? "position-cost-net-v3")} />
                <KV k="Settings version" v={String(cts?.settings_version ?? "—")} />
                <KV k="Updated" v={String(cts?.updated_at ?? "—")} />
                <KV k="Pulse running" v={stats?.paused ? "paused" : stats?.running && !stats?.halted ? "yes" : stats?.haltReason || "halted"} />
                <KV k="Scan" v={`${fmtNum(stats?.scanMs, 1)} ms · cycle ${stats?.cycle ?? "—"}`} />
                <KV k="Volume factor" v={String(num(stats?.volumeFactor ?? overlay.volumeFactor, 1))} />
                <KV k="Unit" v={String(stats?.unit ?? (conn === "vst" ? "VST" : conn === "live" ? "USDT" : "MIXED"))} />
              </Grid>
              <p className="text-sm text-muted">
                This desk is independent of the CTS engine. Overlay saves apply only to Live or VST —
                Overall is a mixed view and will not write either book.
              </p>
            </Card>
          )}

          {section === "profit" && (
            <Card
              title="Profit factor · PositionCost"
              hint="1.00 = Neutral after cost · 1.10 = +1× PositionCost net · last 15 closes"
            >
              <Grid>
                <Slider
                  label="Position cost"
                  value={overlay.positionCostPct}
                  min={0.02}
                  max={1}
                  step={0.01}
                  unit="%"
                  hint="Default 0.15%. Deducted once from the gross move before R."
                  onChange={(v) => patch("positionCostPct", v)}
                />
                <Slider
                  label="Min PF"
                  value={overlay.minPf}
                  min={1.1}
                  max={2.3}
                  step={0.02}
                  hint={pfHint(overlay.minPf, overlay.positionCostPct)}
                  onChange={(v) => patch("minPf", v)}
                />
                <Toggle
                  label="Windows follow the overall validation N (PF, Set PF, Exit PF, SL:TP and trail recalc)"
                  on={overlay.windowsLinked !== false}
                  onChange={(v) => patch("windowsLinked", v)}
                />
                <Slider
                  label="PF window (own N when windows are unlinked)"
                  value={overlay.pfWindow}
                  min={8}
                  max={50}
                  step={1}
                  hint="Last N closed trades for the average Result-R."
                  onChange={(v) => patch("pfWindow", v)}
                />
              </Grid>
              <div className="grid gap-3 sm:grid-cols-3">
                <KV k="1.00 Neutral" v={`net 0 · gross ${overlay.positionCostPct.toFixed(2)}%`} />
                <KV k="1.10 = +1× cost" v={`net +${overlay.positionCostPct.toFixed(2)}% · gross ${(overlay.positionCostPct * 2).toFixed(2)}%`} />
                <KV
                  k={`Last ${overlay.pfWindow} live`}
                  v={pfLive(stats, overlay)}
                />
              </div>
              <p className="text-sm text-muted">
                Ratio = 1 + avg( (move% − cost%) / cost% ) × 0.10. Gate uses this scale, not classic
                gross-profit / gross-loss. Classic PF still shows on Results.
              </p>
            </Card>
          )}

          {section === "risk" && (
            <Card title="Set grid · SL = TP × ratio" hint="Every ratio below runs as its own Set, for every TP step and pack. Read-only.">
              <div className="flex flex-wrap gap-2 font-mono text-xs">
                {SL_SET_RATIOS.map((r) => (
                  <span key={r} className="rounded-md border border-border px-2 py-1 text-muted">
                    {r.toFixed(2)}
                  </span>
                ))}
              </div>
              <p className="font-mono text-xs text-muted">
                TP = k × PositionCost for k = {overlay.setMinStep}–{overlay.setStepMax}; the net at TP is (k − 1) × PositionCost.
              </p>
            </Card>
          )}
          {section === "risk" && (
            <Card title="Live SL:TP pick" hint="Live variant pick (auto-recalc) · grid 0.3–1.5 step 0.3 · separate from the Set grid below">
              <div className="flex flex-wrap gap-2">
                {SL_TP_RATIOS.map((r) => {
                  const active = Math.abs(overlay.slToTpRatio - r) < 1e-9;
                  const tp = overlay.positionCostPct * overlay.tpCostRatio;
                  const sl = tp * r;
                  return (
                    <button
                      key={r}
                      type="button"
                      data-ratio={r}
                      onClick={() => patch("slToTpRatio", snapSlToTp(r))}
                      className={`min-h-11 min-w-20 rounded-lg border px-3 py-2 font-mono text-sm ${
                        active ? "border-primary bg-primary-dim/40 text-fg" : "border-border text-muted"
                      }`}
                    >
                      <div>{r.toFixed(1)}</div>
                      <div className="text-[10px] tracking-wide uppercase">
                        RR {(1 / r).toFixed(2)} · SL {sl.toFixed(2)}%
                      </div>
                    </button>
                  );
                })}
              </div>
              <Grid>
                <Toggle label="Auto-recalc SL:TP" on={overlay.slToTpAuto} onChange={(v) => patch("slToTpAuto", v)} />
                <Slider
                  label="Recalc min samples"
                  value={overlay.slToTpRecalcN}
                  min={8}
                  max={20}
                  step={1}
                  hint="Last-N closes tagged with that ratio"
                  onChange={(v) => patch("slToTpRecalcN", v)}
                />
                <Slider
                  label="Recalc every N closes"
                  value={overlay.slToTpRecalcEvery}
                  min={3}
                  max={30}
                  step={1}
                  onChange={(v) => patch("slToTpRecalcEvery", v)}
                />
                <Slider label="SL min" value={overlay.slMinPct} min={0.05} max={1} step={0.01} unit="%" onChange={(v) => patch("slMinPct", v)} />
                <Slider label="SL max" value={overlay.slMaxPct} min={0.2} max={3} step={0.05} unit="%" onChange={(v) => patch("slMaxPct", v)} />
                <Slider label="TP min" value={overlay.tpMinPct} min={0.1} max={2} step={0.05} unit="%" onChange={(v) => patch("tpMinPct", v)} />
                <Slider label="TP max" value={overlay.tpMaxPct} min={0.4} max={6} step={0.05} unit="%" onChange={(v) => patch("tpMaxPct", v)} />
                <Slider
                  label="TP × PositionCost"
                  value={overlay.tpCostRatio}
                  min={1}
                  max={20}
                  step={1}
                  hint={`Default 5× → ${(overlay.positionCostPct * overlay.tpCostRatio).toFixed(2)}% TP`}
                  onChange={(v) => patch("tpCostRatio", v)}
                />
                <Slider label="Base TP" value={overlay.tpPct} min={0.2} max={4} step={0.05} unit="%" onChange={(v) => patch("tpPct", v)} />
              </Grid>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="font-mono text-xs text-muted">
                    <tr>
                      <th className="pb-2 font-medium">Ratio</th>
                      <th className="pb-2 font-medium">RR</th>
                      <th className="pb-2 font-medium">SL %</th>
                      <th className="pb-2 font-medium">TP %</th>
                      <th className="pb-2 font-medium">Win needed</th>
                    </tr>
                  </thead>
                  <tbody>
                    {SL_TP_RATIOS.map((r) => {
                      const tp = overlay.positionCostPct * overlay.tpCostRatio;
                      const sl = tp * r;
                      const wr = sl / (sl + tp);
                      const on = Math.abs(overlay.slToTpRatio - r) < 1e-9;
                      return (
                        <tr key={r} className={`border-t border-border font-mono ${on ? "text-primary" : ""}`}>
                          <td className="py-1.5">{r.toFixed(1)}</td>
                          <td className="py-1.5">{(1 / r).toFixed(2)}</td>
                          <td className="py-1.5">{sl.toFixed(2)}</td>
                          <td className="py-1.5">{tp.toFixed(2)}</td>
                          <td className="py-1.5">{(wr * 100).toFixed(0)}%</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <p className="text-sm text-muted">
                Active {overlay.slToTpRatio.toFixed(1)} · TP {(overlay.positionCostPct * overlay.tpCostRatio).toFixed(2)}% × ratio → SL{" "}
                {(overlay.positionCostPct * overlay.tpCostRatio * overlay.slToTpRatio).toFixed(2)}%. Auto picks the ratio with the best last-{overlay.slToTpRecalcN} PositionCost score.
              </p>
            </Card>
          )}

          {section === "packs" && (
            <Card title="Strategy types" hint="Each type runs independently · sliders ON=1 OFF=0">
              <Grid>
                <EnableSlider label="Indications" on={overlay.stratIndications} hint="State/Direction/Move/Active/Common/Signals" onChange={(v) => patch("stratIndications", v)} />
                <EnableSlider label="General pulse" on={overlay.stratGeneral} hint="score() pack" onChange={(v) => patch("stratGeneral", v)} />
                <EnableSlider label="Block strategy" on={overlay.stratBlock && overlay.blockEnabled} hint="all counts, 0 = unlimited" onChange={(v) => { patch("stratBlock", v); patch("blockEnabled", v); }} />
                <EnableSlider label="Trailing" on={overlay.stratTrailing} hint="independent trail Sets" onChange={(v) => patch("stratTrailing", v)} />
                <EnableSlider label="DCA" on={overlay.dcaEnabled === true && overlay.stratDca === true} hint="independent steps" onChange={(v) => { patch("dcaEnabled", v); patch("stratDca", v); }} />
              </Grid>
              <p className="text-sm text-muted">
                Indications and general run in parallel for entries. Block adds on a live parent for every count (max stack {overlay.blockMaxStack || "unlimited"}).
                Trailing only moves SL after min-step. Last-{overlay.pfWindow} PositionCost PF must pass before any new risk.
              </p>
            </Card>
          )}

          {section === "sets" && <SetsOverview stats={stats} />}
          {section === "sets" && (
            <Card
              title="Hold time per step"
              hint="Hold time on: positions leave on a time exit of their own. Auto Mode picks it for every Set (pack × range × step, trailing pair) from that Set's own last orders (as many as it has, at most the limit); a step with its manual hold on keeps the manual value. Steps 1–5: 1–50 min; steps 6–12: 30–350 min. Replay and live use the same hold."
            >
              <Grid>
                <Toggle label="Hold time" on={overlay.holdEnabled !== false} onChange={(v) => patch("holdEnabled", v)} />
                <Toggle label="Auto Mode (best hold per Set)" on={overlay.holdEnabled !== false && overlay.holdAuto !== false} onChange={(v) => patch("holdAuto", v)} />
                <Slider
                  label="Auto: orders judged (max)"
                  value={overlay.holdAutoMax ?? 50}
                  min={10}
                  max={50}
                  step={1}
                  hint={`last ${overlay.holdAutoMax ?? 50} orders of the Set at most`}
                  onChange={(v) => patch("holdAutoMax", v)}
                />
                <Slider
                  label="Auto: orders needed (min)"
                  value={overlay.holdAutoMin ?? 10}
                  min={3}
                  max={50}
                  step={1}
                  hint={`below ${overlay.holdAutoMin ?? 10} orders a Set keeps the global stop`}
                  onChange={(v) => patch("holdAutoMin", v)}
                />
              </Grid>
              <div className="grid gap-2 sm:grid-cols-2" data-testid="step-hold">
                {Array.from({ length: 12 }, (_, i) => {
                  const step = i + 1;
                  const [lo, hi] = step <= 5 ? [1, 50] : [30, 350];
                  const on = overlay.stepHoldOn?.[i] === true;
                  const raw = Number(overlay.stepHoldMin?.[i] ?? lo);
                  const minutes = Math.max(lo, Math.min(hi, Math.round(raw)));
                  const setAt = <T,>(arr: T[] | undefined, v: T, fill: T): T[] => {
                    const next = Array.from({ length: 12 }, (_, j) => (arr?.[j] ?? fill));
                    next[i] = v;
                    return next;
                  };
                  return (
                    <div key={step} className="rounded-lg border border-border bg-bg2 px-3 py-2" data-testid={`step-hold-${step}`}>
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-mono text-xs">Step {step}</span>
                        <label className="flex items-center gap-2 font-mono text-xs text-muted">
                          <input
                            type="checkbox"
                            checked={on}
                            data-testid={`step-hold-on-${step}`}
                            onChange={(e) => patch("stepHoldOn", setAt(overlay.stepHoldOn, e.target.checked, false))}
                          />
                          hold {on ? "on" : "off"}
                        </label>
                        <span className="font-mono text-xs">{on ? `${minutes} min` : "global"}</span>
                      </div>
                      <input
                        type="range"
                        className="mt-2 w-full"
                        min={lo}
                        max={hi}
                        step={1}
                        value={minutes}
                        disabled={!on}
                        data-testid={`step-hold-min-${step}`}
                        aria-label={`Step ${step} hold minutes`}
                        onChange={(e) => patch("stepHoldMin", setAt(overlay.stepHoldMin, Number(e.target.value), lo))}
                      />
                    </div>
                  );
                })}
              </div>
            </Card>
          )}
          {section === "sets" && (
            <Card
              title="Independent Sets · 1m historic"
              hint="Each pack × SL:TP × TP-step (2–30 × position cost) is its own book. Sets below Minimal Step Range are not calculated. Live losses raise min step to the count of positive/successful fills."
            >
              <Grid>
                <Toggle label="Historic 1m replay" on={overlay.histEnabled} onChange={(v) => patch("histEnabled", v)} />
                <Toggle label="Base gate (always on: entries open only under a Base-validated Set)" on={true} onChange={() => patch("setUseHistoricGate", true)} />
                <Slider
                  label="Lookback bars"
                  value={overlay.histLookbackBars}
                  min={120}
                  max={1440}
                  step={60}
                  hint={`${overlay.histLookbackBars} × 1m = ${(overlay.histLookbackBars / 60).toFixed(1)}h`}
                  onChange={(v) => patch("histLookbackBars", v)}
                />
                <Slider
                  label="Min bars"
                  value={overlay.histMinBars}
                  min={60}
                  max={480}
                  step={20}
                  onChange={(v) => patch("histMinBars", v)}
                />
                <Slider
                  label="Warmup bars"
                  value={overlay.histWarmup}
                  min={16}
                  max={80}
                  step={2}
                  onChange={(v) => patch("histWarmup", v)}
                />
                <Slider
                  label="Refresh"
                  value={overlay.histRefreshS}
                  min={30}
                  max={600}
                  step={10}
                  unit="s"
                  onChange={(v) => patch("histRefreshS", v)}
                />
                <Slider
                  label="PF window"
                  value={overlay.setPfWindow}
                  min={8}
                  max={40}
                  step={1}
                  hint="Last N historic+live fills for PositionCost PF"
                  onChange={(v) => patch("setPfWindow", v)}
                />
                <Slider
                  label="Deact window"
                  value={overlay.setDeactN}
                  min={10}
                  max={80}
                  step={1}
                  hint="Latest N live fills · overall average loss deactivates that Set"
                  onChange={(v) => patch("setDeactN", v)}
                />
                <Slider
                  label="Min TP step"
                  value={overlay.setMinStep}
                  min={2}
                  max={30}
                  step={1}
                  hint={`TP = k × PositionCost (${overlay.positionCostPct}%). Default 2 (grid start). k = ${overlay.setMinStep} → TP ${(overlay.setMinStep * overlay.positionCostPct).toFixed(2)}%. Losing live fills raise min step.`}
                  onChange={(v) => patch("setMinStep", Math.max(2, Math.min(30, Math.round(v))))}
                />
                <Slider
                  label="Step max"
                  value={overlay.setStepMax}
                  min={2}
                  max={30}
                  step={1}
                  hint="Upper TP step k. Default 30. Range 2–30."
                  onChange={(v) => patch("setStepMax", Math.max(overlay.setMinStep || 2, Math.min(30, Math.round(v))))}
                />
                <Toggle
                  label="Adapt min step from live"
                  on={overlay.setStepAdapt !== false}
                  onChange={(v) => patch("setStepAdapt", v)}
                />
                <Slider
                  label="Set min PF"
                  value={overlay.setMinPf}
                  min={1.1}
                  max={2.3}
                  step={0.02}
                  hint={pfHint(overlay.setMinPf, overlay.positionCostPct)}
                  onChange={(v) => patch("setMinPf", v)}
                />
                <Slider
                  label="Max DD time (h)"
                  value={overlay.setMaxDdtHours}
                  min={2}
                  max={36}
                  step={2}
                  unit="h"
                  hint="A Set is eligible when the longest drawdown over its last gate-window positions is lower than this (default 24 h, owner 2026-10-29)."
                  onChange={(v) => patch("setMaxDdtHours", Math.max(2, Math.min(36, Math.round(v / 2) * 2)))}
                />
                <div className="col-span-full mt-2 font-mono text-[11px] tracking-wide text-muted uppercase">Stages · Base → Main → Real</div>
                <p className="col-span-full text-xs text-muted">
                  Each stage validates only on the orders the stage before it let through, and those orders form a new Set of the Base Set:
                  the Main Set (opened while Base passed), the Real Set (opened while Main passed) and the Real Negative Set (opened in a
                  Main dip). Execution uses Sets that pass Real; their orders mirror to the exchange.
                </p>
                <Toggle label="Stages after Base (off: the Base long window alone)" on={overlay.stagesEnabled !== false} onChange={(v) => patch("stagesEnabled", v)} />
                <Toggle
                  label="Stage windows need their full N orders (off, default: a window judges the orders it has, at least one; on: a Set with fewer never passes Main or Real)"
                  on={overlay.stageFullWindow === true}
                  onChange={(v) => patch("stageFullWindow", v)}
                />
                <div className="col-span-full mt-1 font-mono text-[11px] text-muted">Base · coordinations, ranges and types on their own tape · both windows must pass</div>
                <Slider
                  label="Base · long window (last N orders)"
                  value={overlay.setGateWindow}
                  min={10}
                  max={100}
                  step={1}
                  hint="default 50 · each Set on its own last N orders, at the minimum PF, with the drawdown time"
                  onChange={(v) => patch("setGateWindow", v)}
                />
                <Slider
                  label="Base · short window (last N orders)"
                  value={Math.min(overlay.setGateShortN, Math.floor(overlay.setGateWindow * overlay.setGateShortMaxRatio + 1e-9))}
                  min={0}
                  max={Math.max(5, Math.floor(overlay.setGateWindow * overlay.setGateShortMaxRatio + 1e-9))}
                  step={5}
                  hint={`the second Base window, 0 = off · at most ${Math.floor(overlay.setGateWindow * overlay.setGateShortMaxRatio + 1e-9)} with the long window ${overlay.setGateWindow} and the ratio below · tested 5-25 step 5`}
                  onChange={(v) => patch("setGateShortN", v)}
                />
                <Slider
                  label="Base · minimal window (newest results)"
                  value={overlay.setGateMinN}
                  min={0}
                  max={3}
                  step={1}
                  hint="the third Base window, 0 = off · the newest 1-3 results of the Set's own orders must all be positive (or all negative, below)"
                  onChange={(v) => patch("setGateMinN", v)}
                />
                <label className="rounded-lg border border-border bg-bg2 px-3 py-3">
                  <span className="font-mono text-xs text-muted">Base · minimal window results</span>
                  <select
                    className="mt-1 w-full rounded border border-border bg-bg px-2 py-1 font-mono text-sm"
                    value={overlay.setGateMinMode}
                    onChange={(e) => patch("setGateMinMode", e.target.value === "negative" ? "negative" : "positive")}
                  >
                    <option value="positive">positive (every one of the newest results above zero)</option>
                    <option value="negative">negative (every one of the newest results below zero)</option>
                  </select>
                </label>
                <Slider
                  label="Base · short window max share of the long window"
                  value={overlay.setGateShortMaxRatio}
                  min={0.1}
                  max={1}
                  step={0.05}
                  hint="default 0.5 (50 %): long 40 allows a short window of at most 20; the engine clamps the short window to this share"
                  onChange={(v) => patch("setGateShortMaxRatio", v)}
                />
                <Slider
                  label="Base · min orders"
                  value={overlay.setGateMinTrades}
                  min={0}
                  max={100}
                  step={1}
                  hint="0 = off (default, owner 2026-10-29): a Set is judged on the orders it has. Above 0: fewer orders than this are not judged (at most the long window)."
                  onChange={(v) => patch("setGateMinTrades", v)}
                />
                <div className="col-span-full mt-1 font-mono text-[11px] text-muted">Main · additional strategies, coordinations, processings · the last-N validation, on the Main Set</div>
                <Toggle label="Main · static window" on={overlay.mainGateEnabled !== false} onChange={(v) => patch("mainGateEnabled", v)} />
                <Slider
                  label="Main · static window (last N Main orders)"
                  value={overlay.mainGateN}
                  min={8}
                  max={50}
                  step={1}
                  hint="default 25 · the first Main window, at the Base minimum PF"
                  onChange={(v) => patch("mainGateN", v)}
                />
                <Toggle label="Main · Auto window (each Set picks its own; both Main windows must pass)" on={overlay.mainAutoEnabled !== false} onChange={(v) => patch("mainAutoEnabled", v)} />
                <Slider
                  label="Main · Auto window max share of the static window"
                  value={overlay.mainAutoMaxRatio}
                  min={0.1}
                  max={1}
                  step={0.05}
                  hint="default 0.5 (50 %): static 25 allows Auto windows of at most 12; candidates above it are dropped by the engine"
                  onChange={(v) => patch("mainAutoMaxRatio", v)}
                />
                <WindowChips
                  label="Main · Auto candidates"
                  value={overlay.mainAutoWindows}
                  options={[5, 10, 15, 20, 25]}
                  hint={`default 5-15 step 5 · per Set, the window whose past validations were followed by the best PF on its Main Set · at most ${Math.floor(overlay.mainGateN * overlay.mainAutoMaxRatio + 1e-9)} with the static window ${overlay.mainGateN} and the share below`}
                  onChange={(v) => patch("mainAutoWindows", v)}
                />
                <Slider
                  label="Main · Auto min orders"
                  value={overlay.mainAutoMin}
                  min={3}
                  max={50}
                  step={1}
                  hint="a candidate needs this many validated Main orders"
                  onChange={(v) => patch("mainAutoMin", v)}
                />
                <Slider
                  label="Main · Auto margin"
                  value={overlay.mainAutoMargin}
                  min={1}
                  max={1.5}
                  step={0.01}
                  hint="a candidate must beat the static window's PF by this factor (default 1.02)"
                  onChange={(v) => patch("mainAutoMargin", v)}
                />
                <Toggle label="Main · Negative lane (a dip opens Real Negative)" on={overlay.mainNegEnabled !== false} onChange={(v) => patch("mainNegEnabled", v)} />
                <WindowChips
                  label="Main · Negative windows"
                  value={overlay.mainNegWindows}
                  options={[1, 2, 5, 10]}
                  hint="default 5 and 10 · the average of the last N Main orders below zero, for some N, makes the Set's Real Negative Set (1 and 2: the last one or two results)"
                  onChange={(v) => patch("mainNegWindows", v)}
                />
                <div className="col-span-full mt-1 font-mono text-[11px] text-muted">Real · evals and overall strategies · Active execution and mirroring to the exchange</div>
                <Toggle label="Real · validation (last N of the lane's Real Set)" on={overlay.realGateEnabled !== false} onChange={(v) => patch("realGateEnabled", v)} />
                <Slider
                  label="Real · last N orders"
                  value={overlay.realGateN}
                  min={1}
                  max={50}
                  step={1}
                  hint="default 15 · single window, on the Real Set (positive lane) and on the Real Negative Set (Negative lane)"
                  onChange={(v) => patch("realGateN", v)}
                />
                <Toggle label="Real · overall strategies read the Real Sets' paper stream" on={overlay.realStagePaper !== false} onChange={(v) => patch("realStagePaper", v)} />
                <Toggle label="All validation gates use the Base minimum PF (Real, Exit, DCA, Block, coordinator)" on={overlay.minPfLinked !== false} onChange={(v) => patch("minPfLinked", v)} />
                <Toggle label="Pre-history grows when too many Sets lack orders" on={overlay.histLookbackAuto !== false} onChange={(v) => patch("histLookbackAuto", v)} />
                <Slider
                  label="Pre-history · max bars"
                  value={overlay.histLookbackMax}
                  min={480}
                  max={2880}
                  step={240}
                  hint="the lookback may grow up to this many 1m bars"
                  onChange={(v) => patch("histLookbackMax", v)}
                />
                <Slider
                  label="Min samples"
                  value={overlay.setMinSamples}
                  min={5}
                  max={40}
                  step={1}
                  onChange={(v) => patch("setMinSamples", v)}
                />
                <Slider
                  label="Short Range · TP step min"
                  value={overlay.shortRangeStepMin}
                  min={2}
                  max={6}
                  step={1}
                  hint="Short Range (Low-Range signals) trades TP steps min..max, one step = one PositionCost. Default 2–6."
                  onChange={(v) => patch("shortRangeStepMin", Math.max(2, Math.min(overlay.shortRangeStepMax || 6, Math.round(v))))}
                />
                <Slider
                  label="Short Range · TP step max"
                  value={overlay.shortRangeStepMax}
                  min={2}
                  max={6}
                  step={1}
                  onChange={(v) => patch("shortRangeStepMax", Math.max(overlay.shortRangeStepMin || 2, Math.min(6, Math.round(v))))}
                />
                <Slider
                  label="Short Range · min PF"
                  value={overlay.shortRangeMinPf}
                  min={1.1}
                  max={2.3}
                  step={0.02}
                  hint="Base PF floor of the Short Range strategy alone (default 1.25, at least 1.10); the other strategies use the Set min PF above."
                  onChange={(v) => patch("shortRangeMinPf", v)}
                />
                <Slider
                  label="Min stop · steps over cost"
                  value={overlay.slMinSteps}
                  min={0}
                  max={5}
                  step={1}
                  hint={`Overall minimum stop = PositionCost + n steps = ${(((overlay.slMinSteps ?? 1) + 1) * overlay.positionCostPct).toFixed(2)}% (default n = 1).`}
                  onChange={(v) => patch("slMinSteps", Math.max(0, Math.min(5, Math.round(v))))}
                />
                <Slider
                  label="Min trailing distance · steps over cost"
                  value={overlay.trailMinSteps}
                  min={1}
                  max={7}
                  step={1}
                  hint={`Minimum trailing distance = PositionCost + n steps = ${(((overlay.trailMinSteps ?? 2) + 1) * overlay.positionCostPct).toFixed(2)}% (default n = 2).`}
                  onChange={(v) => patch("trailMinSteps", Math.max(1, Math.min(7, Math.round(v))))}
                />
              </Grid>
              <SetsLiveTable stats={stats} overlay={overlay} />
              <p className="text-sm text-muted">
                1.00 = Neutral after cost · 1.10 = +1× PositionCost. Historic walks 1-minute OHLC, SL-first on
                same-bar, trail after arm, time/scratch stops. Live fills merge into the same Set book.
              </p>
            </Card>
          )}

          {section === "exits" && (
            <Card
              title="Best exits · optimal SL"
              hint="Closes independent of TP. Profit is taken by tightening SL to peak − optimal give. Best-of last-15 PF among lock / peak / reverse / time."
            >
              <Grid>
                <Toggle label="Exit coordinator" on={overlay.exitEnabled} onChange={(v) => patch("exitEnabled", v)} />
                <Toggle label="Ignore TP (SL takes profit)" on={overlay.exitIgnoreTp} onChange={(v) => patch("exitIgnoreTp", v)} />
                <Toggle label="Best-of last-15 PF" on={overlay.exitBestOf} onChange={(v) => patch("exitBestOf", v)} />
                <Toggle label="Auto-deactivate lanes" on={overlay.exitAutoDeact} onChange={(v) => patch("exitAutoDeact", v)} />
                <Toggle label="Lock to breakeven" on={overlay.exitLockOn} onChange={(v) => patch("exitLockOn", v)} />
                <Toggle label="Peak optimal SL" on={overlay.exitPeakOn} onChange={(v) => patch("exitPeakOn", v)} />
                <Toggle label="Reverse indication" on={overlay.exitRevOn} onChange={(v) => patch("exitRevOn", v)} />
                <Toggle label="Time / scratch via SL" on={overlay.exitTimeOn} onChange={(v) => patch("exitTimeOn", v)} />
                <Slider
                  label="Lock after"
                  value={overlay.exitLockPct}
                  min={0.05}
                  max={0.8}
                  step={0.01}
                  unit="%"
                  hint="Move SL to BE + buffer once unrealized clears this"
                  onChange={(v) => patch("exitLockPct", v)}
                />
                <Slider
                  label="BE buffer"
                  value={overlay.exitBeBuffer}
                  min={0.01}
                  max={0.2}
                  step={0.01}
                  unit="%"
                  onChange={(v) => patch("exitBeBuffer", v)}
                />
                <Slider
                  label="Optimal SL from peak"
                  value={overlay.exitOptSlPct}
                  min={0.1}
                  max={0.9}
                  step={0.05}
                  unit="%"
                  hint="Giveback from peak that takes profit — not TP"
                  onChange={(v) => patch("exitOptSlPct", v)}
                />
                <Slider label="Opt SL min" value={overlay.exitOptSlMin} min={0.05} max={0.5} step={0.01} unit="%" onChange={(v) => patch("exitOptSlMin", v)} />
                <Slider label="Opt SL max" value={overlay.exitOptSlMax} min={0.2} max={1.5} step={0.05} unit="%" onChange={(v) => patch("exitOptSlMax", v)} />
                <Slider label="Min hold" value={overlay.exitMinHoldS} min={4} max={90} step={1} unit="s" onChange={(v) => patch("exitMinHoldS", v)} />
                <Slider label="Exit min samples" value={overlay.exitMinSamples} min={5} max={40} step={1} hint="default 8 · closes of a lane before it is judged" onChange={(v) => patch("exitMinSamples", v)} />
                <Slider label="Exit PF window" value={overlay.exitPfWindow} min={8} max={40} step={1} onChange={(v) => patch("exitPfWindow", v)} />
                <Slider label="Exit deact N" value={overlay.exitDeactN} min={10} max={80} step={1} onChange={(v) => patch("exitDeactN", v)} />
                <Slider
                  label="Exit min PF"
                  value={overlay.exitMinPf}
                  min={1.1}
                  max={2.3}
                  step={0.02}
                  hint={pfHint(overlay.exitMinPf, overlay.positionCostPct)}
                  onChange={(v) => patch("exitMinPf", v)}
                />
              </Grid>
              <ExitLanesTable stats={stats} />
              <p className="text-sm text-muted">
                TP is only a far catastrophe cap. Hard SL always protects. Lock / peak / reverse / time compete;
                the lane with the best last-{overlay.exitPfWindow} PositionCost PF fires. Last {overlay.exitDeactN} average
                Result-R below 0 deactivates that lane.
              </p>
            </Card>
          )}

          {section === "stages" && (
            <Card title="Stage coordinates" hint="Base / Main / Real PositionCost floors">
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="font-mono text-xs text-muted">
                    <tr>
                      <th className="pb-2 font-medium">Stage</th>
                      <th className="pb-2 font-medium">On</th>
                      <th className="pb-2 font-medium">Min PF</th>
                      <th className="pb-2 font-medium">Max DD time</th>
                      <th className="pb-2 font-medium">Max positions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(["base", "main", "real"] as const).map((st) => {
                      const row = strategies.main?.[st];
                      return (
                        <tr key={st} className="border-t border-border">
                          <td className="py-2 capitalize">{st}</td>
                          <td className="py-2">{row?.enabled ? "yes" : "no"}</td>
                          <td className="py-2 font-mono">{row?.min_profit_factor ?? "—"}</td>
                          <td className="py-2 font-mono">{row?.max_drawdown_time ?? "—"}</td>
                          <td className="py-2 font-mono">{row?.max_positions ?? "—"}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <Grid>
                <Slider label="Prev window" value={overlay.prevPosWindow} min={5} max={100} step={1} hint="closes the coordinator's previous-positions axis reads" onChange={(v) => patch("prevPosWindow", v)} />
                <Slider label="Prev min count" value={overlay.prevPosMinCount} min={1} max={50} step={1} hint="default 8" onChange={(v) => patch("prevPosMinCount", v)} />
                <Slider label="Main eval pos count" value={overlay.mainEvalPosCount} min={3} max={50} step={1} hint="default 20" onChange={(v) => patch("mainEvalPosCount", v)} />
                <Slider label="Real eval pos count" value={overlay.realEvalPosCount} min={3} max={50} step={1} hint="default 15" onChange={(v) => patch("realEvalPosCount", v)} />
                <KV k="Min step" v={String(overlay.minStep)} />
                <KV k="Trailing min step" v={String(overlay.trailingMinStep)} />
              </Grid>
            </Card>
          )}

          {section === "block" && (
            <Card title="Block strategy" hint="Exact CTS formula · counts 1–12 independent">
              <Grid>
                <EnableSlider
                  label="Block enabled"
                  on={overlay.blockEnabled}
                  hint="default ON · all counts"
                  onChange={(v) => { patch("blockEnabled", v); patch("stratBlock", v); }}
                />
                <Toggle
                  label="Active Live overlay"
                  on={overlay.blockActiveLive}
                  onChange={(v) => patch("blockActiveLive", v)}
                />
                <Toggle
                  label="Active Real overlay"
                  on={overlay.blockActiveReal}
                  onChange={(v) => patch("blockActiveReal", v)}
                />
                <Num
                  label="Block min samples"
                  value={overlay.blockMinSamples}
                  min={1}
                  max={50}
                  step={1}
                  hint="Block's own count (default 8) — independent of the PF windows and the coordinator's prev counts"
                  onChange={(v) => patch("blockMinSamples", v)}
                />
                <Num
                  label="Block window"
                  value={overlay.blockWindow}
                  min={8}
                  max={100}
                  step={1}
                  hint="closes the Block evidence reads (default 25)"
                  onChange={(v) => patch("blockWindow", v)}
                />
                <Num
                  label="Max stack"
                  value={overlay.blockMaxStack}
                  min={0}
                  max={10000}
                  step={1}
                  hint="default 6 groups; 0 = unlimited"
                  onChange={(v) => patch("blockMaxStack", v)}
                />
                <Num
                  label="Volume ratio"
                  value={overlay.blockVolumeRatio}
                  min={0.25}
                  max={3}
                  step={0.05}
                  onChange={(v) => patch("blockVolumeRatio", v)}
                />
                <Num
                  label="PF ratio"
                  value={overlay.blockProfitFactorRatio}
                  min={0.2}
                  max={5}
                  step={0.1}
                  onChange={(v) => patch("blockProfitFactorRatio", v)}
                />
                <Num
                  label="Pause count ratio"
                  value={overlay.blockPauseCountRatio}
                  min={0}
                  max={8}
                  step={1}
                  onChange={(v) => patch("blockPauseCountRatio", v)}
                />
                <KV k="Default min PF" v={String(defaultMinPf)} />
              </Grid>
              <p className="font-mono text-xs text-muted">
                targetAdd = parentBase × (count × ratio) · requested = targetAdd − confirmedAdd ·
                minPF = 1 + (({defaultMinPf} − 1) × {overlay.blockProfitFactorRatio} × increment)
              </p>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="font-mono text-xs text-muted">
                    <tr>
                      <th className="pb-2 font-medium">N</th>
                      <th className="pb-2 font-medium">Increment</th>
                      <th className="pb-2 font-medium">Add (base=1)</th>
                      <th className="pb-2 font-medium">Total</th>
                      <th className="pb-2 font-medium">Min PF</th>
                    </tr>
                  </thead>
                  <tbody>
                    {table.map((r) => (
                      <tr key={r.n} className={`border-t border-border font-mono ${overlay.blockMaxStack > 0 && r.n > overlay.blockMaxStack ? "text-faint" : ""}`}>
                        <td className="py-1.5">{r.n}</td>
                        <td className="py-1.5">{r.inc.toFixed(2)}×</td>
                        <td className="py-1.5">{r.add.toFixed(2)}</td>
                        <td className="py-1.5">{r.tot.toFixed(2)}</td>
                        <td className="py-1.5">{r.minPf.toFixed(3)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}

          {section === "trailing" && (
            <Card title="Trailing · independent recals" hint="Own range, own scores — not coupled to SL:TP">
              <Grid>
                <Toggle
                  label="Trailing pack"
                  on={overlay.stratTrailing}
                  onChange={(v) => patch("stratTrailing", v)}
                />
                <Toggle label="Auto-recalc trail" on={overlay.trailAuto} onChange={(v) => patch("trailAuto", v)} />
                <Toggle
                  label="Recalc give from arm × factor"
                  on={overlay.trailRecalcGive}
                  onChange={(v) => {
                    patch("trailRecalcGive", v);
                    if (v) patch("trailGivePct", trailGiveFromArm(overlay.trailArmPct, overlay.trailGiveFactor, overlay.trailGiveMin, overlay.trailGiveMax));
                  }}
                />
              </Grid>
              <Slider
                label="Min trailing step"
                value={overlay.trailMinStep}
                min={2}
                max={8}
                step={1}
                hint="Smallest arm and give of a trail Set, in PositionCost units. Default 3, range 2–8. Every arm k and give j from this step up to 22 runs as a Set, with j ≤ k."
                onChange={(v) => patch("trailMinStep", Math.max(2, Math.min(8, Math.round(v))))}
              />
              <p className="font-mono text-xs text-muted">Live CTS variants · arm:give · filtered by optimal range</p>
              <div className="flex flex-wrap gap-2">
                {(Array.isArray(cts?.strategyBaseTrailingVariants) ? cts!.strategyBaseTrailingVariants : [...TRAIL_VARIANTS]).map((t) => {
                  const raw = String(t);
                  const [a, g] = raw.split(":");
                  const arm = Number(a) || 0;
                  const give = overlay.trailRecalcGive
                    ? trailGiveFromArm(arm, overlay.trailGiveFactor, overlay.trailGiveMin, overlay.trailGiveMax)
                    : Number(g) || 0;
                  const inRange =
                    arm + 1e-9 >= overlay.trailArmMin &&
                    arm - 1e-9 <= overlay.trailArmMax &&
                    give + 1e-9 >= overlay.trailGiveMin &&
                    give - 1e-9 <= overlay.trailGiveMax;
                  const active =
                    Math.abs(arm - overlay.trailArmPct) < 1e-6 &&
                    Math.abs(give - overlay.trailGivePct) < 0.02;
                  return (
                    <button
                      key={raw}
                      type="button"
                      data-trail={raw}
                      disabled={!inRange}
                      onClick={() => {
                        patch("trailArmPct", arm);
                        patch("trailGivePct", give);
                      }}
                      className={`min-h-11 rounded-lg border px-3 font-mono text-sm ${
                        active
                          ? "border-primary bg-primary-dim/40 text-fg"
                          : inRange
                            ? "border-border text-muted"
                            : "border-border text-faint opacity-40"
                      }`}
                    >
                      {arm.toFixed(1)}:{give.toFixed(1)}
                    </button>
                  );
                })}
              </div>
              <Grid>
                <Slider label="Arm min" value={overlay.trailArmMin} min={0.3} max={1.5} step={0.3} unit="%" onChange={(v) => patch("trailArmMin", v)} />
                <Slider label="Arm max" value={overlay.trailArmMax} min={0.3} max={1.5} step={0.3} unit="%" onChange={(v) => patch("trailArmMax", v)} />
                <Slider label="Give min" value={overlay.trailGiveMin} min={0.05} max={0.5} step={0.05} unit="%" onChange={(v) => patch("trailGiveMin", v)} />
                <Slider label="Give max" value={overlay.trailGiveMax} min={0.1} max={0.8} step={0.05} unit="%" onChange={(v) => patch("trailGiveMax", v)} />
                <Slider
                  label="Give factor"
                  value={overlay.trailGiveFactor}
                  min={0.2}
                  max={0.6}
                  step={0.01}
                  hint={`give = arm × factor → ${trailGiveFromArm(overlay.trailArmPct, overlay.trailGiveFactor, overlay.trailGiveMin, overlay.trailGiveMax).toFixed(2)}%`}
                  onChange={(v) => {
                    patch("trailGiveFactor", v);
                    if (overlay.trailRecalcGive) {
                      patch("trailGivePct", trailGiveFromArm(overlay.trailArmPct, v, overlay.trailGiveMin, overlay.trailGiveMax));
                    }
                  }}
                />
                <Slider label="Arm %" value={overlay.trailArmPct} min={0.3} max={1.5} step={0.3} unit="%" onChange={(v) => {
                  patch("trailArmPct", v);
                  if (overlay.trailRecalcGive) {
                    patch("trailGivePct", trailGiveFromArm(v, overlay.trailGiveFactor, overlay.trailGiveMin, overlay.trailGiveMax));
                  }
                }} />
                <Slider label="Giveback %" value={overlay.trailGivePct} min={0.05} max={0.8} step={0.05} unit="%" onChange={(v) => patch("trailGivePct", v)} />
                <Slider label="Recalc min samples" value={overlay.trailRecalcN} min={8} max={20} step={1} onChange={(v) => patch("trailRecalcN", v)} />
                <Slider label="Recalc every N closes" value={overlay.trailRecalcEvery} min={3} max={30} step={1} onChange={(v) => patch("trailRecalcEvery", v)} />
              </Grid>
              <p className="text-sm text-muted">
                Live uses {overlay.trailArmPct.toFixed(1)}:{overlay.trailGivePct.toFixed(1)} locked on the fill. Recals only pick for new entries. Scores are independent of the SL:TP book.
              </p>
            </Card>
          )}

          {section === "timeframes" && (
            <Card title="Timeframes 1 / 5 / 15" hint="Each TF is an independent Signal lane · combined is a separate Indication">
              <Grid>
                <Toggle label="1 minute" on={overlay.tf1m} onChange={(v) => patch("tf1m", v)} />
                <Toggle label="5 minutes" on={overlay.tf5m} onChange={(v) => patch("tf5m", v)} />
                <Toggle label="15 minutes" on={overlay.tf15m} onChange={(v) => patch("tf15m", v)} />
                <Toggle label="Combined consensus" on={overlay.tfCombined} onChange={(v) => patch("tfCombined", v)} />
                <Slider
                  label="Min TFs to agree"
                  value={overlay.tfMinAgree}
                  min={2}
                  max={3}
                  step={1}
                  hint="Combined fires only when this many independent TFs share a side"
                  onChange={(v) => patch("tfMinAgree", v)}
                />
              </Grid>
              <div className="grid gap-2 sm:grid-cols-3">
                {(
                  [
                    { tf: "1m", on: overlay.tf1m, hint: "fast · noise lane", w: "0.85" },
                    { tf: "5m", on: overlay.tf5m, hint: "structure lane", w: "1.00" },
                    { tf: "15m", on: overlay.tf15m, hint: "bias lane", w: "1.20" },
                  ] as const
                ).map((row) => (
                  <div key={row.tf} className="rounded-lg border border-border bg-bg2 px-3 py-2">
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-sm">{row.tf}</span>
                      <span className={`font-mono text-xs ${row.on ? "text-primary" : "text-faint"}`}>{row.on ? "ON" : "OFF"}</span>
                    </div>
                    <p className="mt-1 text-xs text-muted">{row.hint}</p>
                    <p className="font-mono text-[10px] text-faint">weight {row.w}</p>
                  </div>
                ))}
              </div>
              <p className="text-sm text-muted">
                Direct TF Indications stay visible even when combined is on. Combined is preferred for entries. Extra venues stay on 1m.
              </p>
            </Card>
          )}

          {section === "dca" && (
            <Card title="DCA" hint="Independent of Block · adds against a position · scored on its own adds (last-15 PF / last-25 deact) · off by default">
              <Toggle
                label="DCA enabled"
                on={overlay.dcaEnabled === true}
                onChange={(v) => { patch("dcaEnabled", v); patch("stratDca", v); }}
              />
              <Toggle
                label="Auto calculation (rungs from each position's own stop, one small rung, exposure ceiling)"
                on={overlay.dcaAuto !== false}
                onChange={(v) => patch("dcaAuto", v)}
              />
              <Toggle
                label="Also on Set positions (the Set keeps its own stop and TP; the Set is scored on its base result)"
                on={overlay.dcaOnSets === true}
                onChange={(v) => patch("dcaOnSets", v)}
              />
              <Toggle
                label="Also on Axis-admitted positions (only while an Axis is on and the coordinator allows)"
                on={overlay.dcaOnAxis === true}
                onChange={(v) => patch("dcaOnAxis", v)}
              />
              <Grid>
                <Num
                  label="Max exposure × base size (auto)"
                  value={overlay.dcaMaxExposureX}
                  min={1}
                  max={8}
                  step={0.5}
                  onChange={(v) => patch("dcaMaxExposureX", v)}
                />
              </Grid>
              <Toggle
                label="Auto-deact on last-25 avg loss"
                on={overlay.dcaAutoDeact !== false}
                onChange={(v) => patch("dcaAutoDeact", v)}
              />
              <details className="mt-3" open={overlay.dcaAuto === false} data-testid="dca-manual">
              <summary className="cursor-pointer font-mono text-xs text-muted">Manual static settings (used when Auto calculation is off)</summary>
              <Grid>
                <Num
                  label="Max steps"
                  value={overlay.dcaMaxSteps}
                  min={0}
                  max={10000}
                  step={1}
                  hint="0 = unlimited"
                  onChange={(v) => patch("dcaMaxSteps", v)}
                />
                <Num
                  label="Cooldown s"
                  value={overlay.dcaCooldownSeconds}
                  min={5}
                  max={180}
                  step={5}
                  onChange={(v) => patch("dcaCooldownSeconds", v)}
                />
                <Num
                  label="Breakeven +"
                  value={overlay.dcaBreakevenProfitPct}
                  min={0.05}
                  max={1}
                  step={0.05}
                  onChange={(v) => patch("dcaBreakevenProfitPct", v)}
                />
                <Num
                  label="Min PF"
                  value={overlay.dcaMinPf}
                  min={1.1}
                  max={1.5}
                  step={0.05}
                  onChange={(v) => patch("dcaMinPf", v)}
                />
                <Num
                  label="DCA PF window"
                  value={overlay.dcaPfWindow ?? overlay.pfWindow}
                  min={5}
                  max={40}
                  step={1}
                  onChange={(v) => patch("dcaPfWindow", v)}
                />
                <Num
                  label="DCA deact N"
                  value={overlay.dcaDeactN ?? overlay.setDeactN}
                  min={10}
                  max={80}
                  step={1}
                  onChange={(v) => patch("dcaDeactN", v)}
                />
              </Grid>
              <div className="mt-3 grid gap-2 sm:grid-cols-2">
                <label className="rounded-lg border border-border bg-bg2 px-3 py-3">
                  <div className="font-mono text-xs text-muted">Take profit mode</div>
                  <select
                    className="mt-2 min-h-11 w-full rounded-lg border border-border bg-surface px-2 font-mono text-sm"
                    value={overlay.dcaTakeProfitMode || "average"}
                    onChange={(e) => patch("dcaTakeProfitMode", e.target.value)}
                  >
                    <option value="average">average entry</option>
                    <option value="last">last fill</option>
                    <option value="first">first fill</option>
                  </select>
                </label>
              </div>
              <p className="mt-3 font-mono text-xs text-muted">Step distances % from average entry</p>
              <Grid>
                {(overlay.dcaStepDistancesPct || [0.5, 1, 1.5, 2]).slice(0, overlay.dcaMaxSteps || undefined).map((d, i) => (
                  <Num
                    key={`dist-${i}`}
                    label={`Distance #${i + 1}`}
                    value={d}
                    min={0.1}
                    max={8}
                    step={0.1}
                    unit="%"
                    onChange={(v) => {
                      const next = [...(overlay.dcaStepDistancesPct || [])];
                      next[i] = v;
                      patch("dcaStepDistancesPct", next);
                    }}
                  />
                ))}
              </Grid>
              <p className="mt-3 font-mono text-xs text-muted">Volume multipliers vs parent</p>
              <Grid>
                {(overlay.dcaStepVolumeMultipliers || [1.5, 2, 2.3, 2.5]).slice(0, overlay.dcaMaxSteps || undefined).map((m, i) => (
                  <Num
                    key={`mult-${i}`}
                    label={`Vol × #${i + 1}`}
                    value={m}
                    min={0.5}
                    max={8}
                    step={0.1}
                    onChange={(v) => {
                      const next = [...(overlay.dcaStepVolumeMultipliers || [])];
                      next[i] = v;
                      patch("dcaStepVolumeMultipliers", next);
                    }}
                  />
                ))}
              </Grid>
              </details>
            </Card>
          )}

          {section === "axes" && (
            <Card title="Coordination · rearrangements · thresholds" hint="CTS Main-stage axes live on pulse">
              <div className="space-y-2">
                <LiveAxis
                  name="Previous"
                  range="4–12 step 2"
                  enabled={overlay.axisPrevEnabled}
                  window={overlay.axisPrevMaxWindow}
                  min={4}
                  max={12}
                  step={2}
                  onEn={(v) => patch("axisPrevEnabled", v)}
                  onWin={(v) => patch("axisPrevMaxWindow", v)}
                />
                <LiveAxis
                  name="Last"
                  range="1–4"
                  enabled={overlay.axisLastEnabled}
                  window={overlay.axisLastMaxWindow}
                  min={1}
                  max={4}
                  step={1}
                  onEn={(v) => patch("axisLastEnabled", v)}
                  onWin={(v) => patch("axisLastMaxWindow", v)}
                />
                <LiveAxis
                  name="Continuous"
                  range="1–8"
                  enabled={overlay.axisContEnabled}
                  window={overlay.axisContMaxWindow}
                  min={1}
                  max={8}
                  step={1}
                  onEn={(v) => patch("axisContEnabled", v)}
                  onWin={(v) => patch("axisContMaxWindow", v)}
                />
                <LiveAxis
                  name="Pause"
                  range="1–8"
                  enabled={overlay.axisPauseEnabled}
                  window={overlay.axisPauseMaxWindow}
                  min={1}
                  max={8}
                  step={1}
                  onEn={(v) => patch("axisPauseEnabled", v)}
                  onWin={(v) => patch("axisPauseMaxWindow", v)}
                />
              </div>
              <Grid>
                <Num label="Min PF" value={overlay.minPf} min={0.8} max={3} step={0.05} onChange={(v) => patch("minPf", v)} />
                <Num label="Noise" value={overlay.noise} min={0.01} max={0.2} step={0.01} onChange={(v) => patch("noise", v)} />
                <Num label="Vol weight" value={overlay.volWeight} min={0.05} max={1} step={0.05} onChange={(v) => patch("volWeight", v)} />
                <Num label="Min step" value={overlay.minStep} min={1} max={12} step={1} onChange={(v) => patch("minStep", v)} />
                <Num label="Max SL ratio" value={overlay.maxStopLossRatio} min={1} max={5} step={0.1} onChange={(v) => patch("maxStopLossRatio", v)} />
                <Num label="Trail min step" value={overlay.trailingMinStep} min={1} max={30} step={1} onChange={(v) => patch("trailingMinStep", v)} />
                <Num
                  label="Pos-count vol ratio"
                  value={overlay.posCountsVolumeRatio}
                  min={0}
                  max={0.3}
                  step={0.01}
                  onChange={(v) => patch("posCountsVolumeRatio", v)}
                />
                <Num
                  label="Rearrange gap"
                  value={overlay.rearrangeGap}
                  min={0.05}
                  max={0.6}
                  step={0.01}
                  onChange={(v) => patch("rearrangeGap", v)}
                />
              </Grid>
              <Toggle label="Rearrange weak slots" on={overlay.rearrange} onChange={(v) => patch("rearrange", v)} />
              <p className="text-sm text-muted">
                Last/Prev PF gates entries. Pause halts on a loss streak. Cont caps live slots.
                Rearrange frees a weak open for a stronger signal when the confidence gap clears.
              </p>
            </Card>
          )}

          {section === "volume" && (
            <Card title="Volume" hint="USDT type · factors · position cost">
              <Grid>
                <KV k="Volume type" v={String(cts?.volume_type ?? "usdt")} />
                <KV k="Base volume factor" v={String(num(cts?.baseVolumeFactor ?? cts?.volume_factor, 1))} />
                <KV k="Live volume factor" v={String(num(cts?.volume_factor_live ?? cts?.live_volume_factor, 1))} />
                <KV k="Preset volume factor" v={String(num(cts?.volume_factor_preset, 1))} />
                <KV k="Signal volume factor" v={String(num(cts?.volume_factor_signal, 1))} />
                <KV k="Volume step ratio" v={String(num(cts?.volume_step_ratio, 0.6))} />
                <KV k="Pos-count volume ratio" v={String(overlay.posCountsVolumeRatio)} />
                <KV k="Exchange position cost" v={String(overlay.positionCostPct)} />
                <Slider
                  label="Volume factor"
                  value={overlay.volumeFactor ?? 1}
                  min={0.1}
                  max={5}
                  step={0.1}
                  hint="Scales pulse notional. 1 = base. Independent per Live / VST."
                  onChange={(v) => patch("volumeFactor", v)}
                />
                <Num
                  label="Pulse notional USDT"
                  value={overlay.targetNotional}
                  min={1}
                  max={20}
                  step={0.05}
                  onChange={(v) => patch("targetNotional", v)}
                />
                <Num
                  label="Max position USDT"
                  value={overlay.sizeMaxNotional ?? 25}
                  min={0.2}
                  max={5000}
                  step={0.5}
                  hint="Hard ceiling for one position's notional, whatever the balance or the factor."
                  onChange={(v) => patch("sizeMaxNotional", v)}
                />
                <Num
                  label="Max entry slippage %"
                  value={overlay.maxEntrySlipPct ?? 0.75}
                  min={0.05}
                  max={5}
                  step={0.05}
                  hint="A fill farther than this from the price the entry was decided at is closed at once and the symbol rests 30 min."
                  onChange={(v) => patch("maxEntrySlipPct", v)}
                />
                <Num
                  label="Drawdown halt %"
                  value={overlay.ddHaltPct ?? 80}
                  min={0}
                  max={90}
                  step={1}
                  hint="Entries halt when equity falls this far below the start equity of the active session (owner default 80). 0 = off (no drawdown protection)."
                  onChange={(v) => patch("ddHaltPct", v)}
                />
                <Toggle
                  label="Drawdown halt measures from the active session's start (off: the stored start equity)"
                  on={(overlay.ddHaltBasis ?? "session") === "session"}
                  onChange={(v) => patch("ddHaltBasis", v ? "session" : "start")}
                />
                <Num
                  label="Size % of balance"
                  value={overlay.sizePctOfBalance ?? 1}
                  min={0}
                  max={25}
                  step={0.1}
                  hint="Position notional = balance x this % x volume factor (raised to the exchange minimum). 0 = fixed pulse notional."
                  onChange={(v) => patch("sizePctOfBalance", v)}
                />
                <KV
                  k="Effective notional"
                  v={
                    Math.min(
                      (overlay.sizePctOfBalance ?? 0) > 0 && Number(stats?.equity) > 0
                        ? ((Number(stats?.equity) * (overlay.sizePctOfBalance ?? 0)) / 100) * (overlay.volumeFactor || 1)
                        : overlay.targetNotional * (overlay.volumeFactor || 1),
                      overlay.sizeMaxNotional ?? 25,
                    ).toFixed(2)
                  }
                />
                <Toggle
                  label="Always max leverage"
                  on={overlay.useMaxLeverage !== false}
                  onChange={(v) => patch("useMaxLeverage", v)}
                />
                <Num
                  label={overlay.useMaxLeverage !== false ? "Fallback leverage" : "Pulse leverage"}
                  value={overlay.leverage}
                  min={1}
                  max={150}
                  step={1}
                  onChange={(v) => patch("leverage", v)}
                />
              </Grid>
            </Card>
          )}

          {section === "controls" && (
            <Card title="Control orders" hint="Per-order SL+TP plus symbol+direction security range · hedge, no reduceOnly">
              <Toggle
                label="Place SL/TP on exchange"
                on={overlay.controlOrders}
                onChange={(v) => patch("controlOrders", v)}
              />
              <Grid>
                <Num label="Stop %" value={overlay.slPct} min={0.1} max={5} step={0.02} onChange={(v) => patch("slPct", v)} />
                <Num label="Take profit %" value={overlay.tpPct} min={0.1} max={8} step={0.05} onChange={(v) => patch("tpPct", v)} />
                <KV k="CTS SL cost ratios" v={arrJoin(cts?.activeStopLossPositionCostRatios, "2, 3, 5")} />
                <KV k="CTS TP multipliers" v={arrJoin(cts?.activeTakeProfitMultipliers, "1.25, 1.5, 1")} />
                <KV k="CTS control_orders" v={bool(cts?.control_orders, true) ? "1" : "0"} />
                <KV k="Working type" v="MARK_PRICE" />
              </Grid>
              <ControlsLive stats={stats} />
              <p className="text-sm text-muted">
                After every parent fill and every Block add, protection is rebuilt for the exact
                aggregate quantity. Trail replaces the live STOP_MARKET. Security SL/TP always exist
                on the symbol+direction book using the widest order range.
              </p>
            </Card>
          )}

          {section === "indication" && (
            <Card title="Indications" hint="CTS types: State is the consensus Indication · Direction / Move / Active / Common / Signals run independently">
              <Grid>
                <EnableSlider label="Indications on" on={overlay.indEnabled} onChange={(v) => patch("indEnabled", v)} />
                <EnableSlider label="State" on={overlay.indTypeState !== false} hint="tf_combined + low-stop consensus — the Indication" onChange={(v) => patch("indTypeState", v)} />
                <EnableSlider label="Direction" on={overlay.indTypeDirection !== false} hint="post-reversal two-window, independent Long/Short" onChange={(v) => patch("indTypeDirection", v)} />
                <EnableSlider label="Move" on={overlay.indTypeMove !== false} hint="same-dir displacement, independent Long/Short" onChange={(v) => patch("indTypeMove", v)} />
                <EnableSlider label="Active" on={overlay.indTypeActive !== false} hint="outbreak 3/5/10 vs previous window" onChange={(v) => patch("indTypeActive", v)} />
                <EnableSlider label="Common" on={overlay.indTypeCommon !== false} hint="RSI + MACD + EMA + Bollinger" onChange={(v) => patch("indTypeCommon", v)} />
                <EnableSlider label="Signals" on={overlay.indTypeSignals !== false} hint="per-TF evaluateSignalCandles" onChange={(v) => patch("indTypeSignals", v)} />
                <EnableSlider label="Extra venues (Binance/Bybit)" on={overlay.indExtraSources} onChange={(v) => patch("indExtraSources", v)} />
                <Num label="Min sources" value={overlay.indMinSources} min={2} max={8} step={1} onChange={(v) => patch("indMinSources", v)} />
                <Num label="Min agreement" value={overlay.indMinAgreement} min={0.5} max={0.95} step={0.05} onChange={(v) => patch("indMinAgreement", v)} />
                <Num label="Min confidence" value={overlay.indMinConfidence} min={0.5} max={0.95} step={0.05} onChange={(v) => patch("indMinConfidence", v)} />
                <Num label="Min strength" value={overlay.indMinStrength} min={0.05} max={0.6} step={0.05} onChange={(v) => patch("indMinStrength", v)} />
                <Num label="SL min %" value={overlay.indStopMinPct} min={0.1} max={1} step={0.05} onChange={(v) => patch("indStopMinPct", v)} />
                <Num label="SL max %" value={overlay.indStopMaxPct} min={0.4} max={3} step={0.05} onChange={(v) => patch("indStopMaxPct", v)} />
                <Num label="ATR × SL" value={overlay.indAtrMult} min={0.4} max={2} step={0.05} onChange={(v) => patch("indAtrMult", v)} />
                <Num label="Reward / risk" value={overlay.indRewardRisk} min={1.1} max={4} step={0.1} onChange={(v) => patch("indRewardRisk", v)} />
                <KV k="Outbreak ranges" v={arrJoin(cts?.activeOutbreakRanges, "3, 5, 10")} />
                <KV k="Noise filter" v={String(num(cts?.activeNoiseFilter, 0.05))} />
              </Grid>
              <p className="mt-3 text-xs text-muted">
                State is the actual Indication (combined TF + multi-source consensus). Direction, Move, Active, Common and Signals
                process additionally and independently when their slider is ON.
              </p>
            </Card>
          )}

          {section === "pulse" && (
            <Card title="Pulse overlay" hint="Always max leverage per contract. Order qty is raised to exchange min lot / min USDT if the target is smaller.">
              <Grid>
                <Num label="Max open" value={overlay.maxOpen} min={0} max={10000} step={1} hint="0 = unlimited" onChange={(v) => patch("maxOpen", v)} />
                <Num label="Max per group" value={overlay.maxPerGroup} min={0} max={10000} step={1} hint="0 = unlimited" onChange={(v) => patch("maxPerGroup", v)} />
                <Num label="Cycle s" value={overlay.scanS} min={0.2} max={8} step={0.05} onChange={(v) => patch("scanS", v)} />
                <Num label="Cooldown s" value={overlay.cooldownS} min={0} max={60} step={1} onChange={(v) => patch("cooldownS", v)} />
                <Num label="Stagger s" value={overlay.staggerS} min={0.05} max={5} step={0.01} onChange={(v) => patch("staggerS", v)} />
                <Num label="Max hold s" value={overlay.timeStopS} min={60} max={21600} step={60} hint="hard cap 6h" onChange={(v) => patch("timeStopS", v)} />
                <Num label="Scratch s" value={overlay.scratchS} min={20} max={1800} step={5} onChange={(v) => patch("scratchS", v)} />
                <Num label="Scratch min %" value={overlay.scratchMinPct} min={0.05} max={1} step={0.01} onChange={(v) => patch("scratchMinPct", v)} />
              </Grid>
            </Card>
          )}

          {section === "symbols" && (
            <Card title="Symbols" hint={`Default rank · max leverage then Volatility 1H · cap ${MAX_SYMBOLS > 0 ? MAX_SYMBOLS : "unlimited"}`}>
              <Toggle
                label="All USDT-M swaps"
                on={overlay.symbolsAll || overlay.symbols.includes("*") || overlay.symbols.includes("ALL")}
                onChange={(v) => {
                  patch("symbolsAll", v);
                  patch("symbols", v ? ["*"] : [...overlay.symbols.filter((s) => s !== "*" && s !== "ALL")]);
                }}
              />
              <div className="mt-3">
                <Grid>
                  <Num label="Dynamic cap" value={overlay.symbolCap} min={0} max={10000} step={1} hint="0 = unlimited. With Dynamic on, the engine keeps every USDT-M name (ranked max leverage then 1H vol) plus any open positions." onChange={(v) => patch("symbolCap", Math.max(0, Math.round(v)))} />
                </Grid>
              </div>
              <div className="mt-3">
                <SymbolPicker
                  selected={overlay.symbols}
                  sort={overlay.symbolSort}
                  dynamic={overlay.symbolsDynamic !== false}
                  onSortChange={(s) => patch("symbolSort", s)}
                  onDynamicChange={(v) => patch("symbolsDynamic", v)}
                  onCap={(n) => {
                    patch("symbolCap", n);
                    patch("symbolsAll", false);
                  }}
                  onChange={(next) => {
                    patch("symbols", next);
                    patch("symbolsAll", next.includes("*") || next.includes("ALL"));
                  }}
                />
              </div>
            </Card>
          )}

          <div className="sticky bottom-2 z-10 flex flex-wrap items-center gap-3 rounded-radius border border-border bg-surface px-4 py-3">
            {conn === "overall" ? (
              <>
                <button
                  type="button"
                  onClick={() => onSave("live")}
                  data-testid="save-overlay-live"
                  disabled={saving || !ready}
                  className="min-h-11 rounded-lg bg-primary px-4 text-sm font-medium text-bg disabled:opacity-40"
                >
                  {saving ? "Saving…" : "Save to Live"}
                </button>
                <button
                  type="button"
                  onClick={() => onSave("vst")}
                  data-testid="save-overlay-vst"
                  disabled={saving || !ready}
                  className="min-h-11 rounded-lg bg-primary px-4 text-sm font-medium text-bg disabled:opacity-40"
                >
                  {saving ? "Saving…" : "Save to VST"}
                </button>
              </>
            ) : (
              <button
                type="button"
                onClick={() => onSave(conn)}
                data-testid="save-overlay"
                disabled={saving || !ready}
                className="min-h-11 rounded-lg bg-primary px-4 text-sm font-medium text-bg disabled:opacity-40"
              >
                {saving ? "Saving…" : "Save overlay"}
              </button>
            )}
            <button
              type="button"
              onClick={() => {
                setOverlay(overlayFromCts(cts ?? {}));
                setDirty(true);
                setSaveMsg("Reset to CTS defaults");
              }}
              className="min-h-11 rounded-lg border border-border px-4 text-sm text-muted"
            >
              Reset from CTS
            </button>
            <span className="text-sm text-muted" data-testid="save-status">
              {saving
                ? "Saving…"
                : dirty
                  ? "Unsaved overlay"
                  : saveMsg || (conn === "overall" ? "Save writes Live or VST independently" : `Live values · ${conn}`)}
            </span>
          </div>
        </div>
      </div>
    </DeskShell>
  );
}

function Card({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section className="space-y-4 rounded-radius border border-border bg-surface p-4">
      <div>
        <h2 className="text-sm font-medium tracking-wide text-muted uppercase">{title}</h2>
        {hint ? <p className="mt-1 text-sm text-muted">{hint}</p> : null}
      </div>
      {children}
    </section>
  );
}

function Grid({ children }: { children: React.ReactNode }) {
  return <div className="grid gap-3 sm:grid-cols-2">{children}</div>;
}

function KV({ k, v }: { k: string; v: string }) {
  return (
    <div className="rounded-lg border border-border bg-bg2 px-3 py-2">
      <div className="font-mono text-xs text-muted">{k}</div>
      <div className="mt-0.5 break-all text-sm">{v || "—"}</div>
    </div>
  );
}

function EnableSlider({
  label,
  on,
  hint,
  onChange,
}: {
  label: string;
  on: boolean;
  hint?: string;
  onChange: (v: boolean) => void;
}) {
  return (
    <Slider
      label={label}
      value={on ? 1 : 0}
      min={0}
      max={1}
      step={1}
      hint={hint ? `${on ? "ON" : "OFF"} · ${hint}` : on ? "ON" : "OFF"}
      onChange={(v) => onChange(v >= 0.5)}
    />
  );
}

function Slider({
  label,
  value,
  min,
  max,
  step,
  unit = "",
  hint,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  unit?: string;
  hint?: string;
  onChange: (n: number) => void;
}) {
  const pct = max === min ? 0 : ((value - min) / (max - min)) * 100;
  return (
    <label className="rounded-lg border border-border bg-bg2 px-3 py-3">
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-xs text-muted">{label}</span>
        <span className="flex items-center gap-1 font-mono text-sm tabular-nums">
          <input
            type="number"
            min={min}
            max={max}
            step={step}
            value={Number.isInteger(step) && step >= 1 ? value : Number(value.toFixed(step < 0.05 ? 2 : 2))}
            onChange={(e) => {
              const n = Number(e.target.value);
              if (!Number.isFinite(n)) return;
              onChange(Math.min(max, Math.max(min, n)));
            }}
            className="w-16 rounded border border-border bg-surface px-1 py-0.5 text-right font-mono text-sm"
            suppressHydrationWarning
          />
          {unit}
        </span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="mt-2 w-full"
        style={{ ["--fill" as string]: `${pct}%` }}
        suppressHydrationWarning
      />
      {hint ? <p className="mt-1 text-xs text-muted">{hint}</p> : null}
    </label>
  );
}

function pfHint(ratio: number, cost: number) {
  const r = (ratio - 1) / 0.1;
  const net = cost * r;
  const gross = cost + net;
  if (ratio <= 1) return "Neutral — net 0 after one PositionCost";
  return `Net +${net.toFixed(2)}% · gross ${gross.toFixed(2)}% (${r.toFixed(2)}× cost)`;
}

function pfLive(stats: LiveStats | null, overlay: PulseOverlay) {
  const p = (stats as LiveStats & { pfCost?: { pf?: number; avgR?: number; count?: number; pass?: boolean } })?.pfCost;
  if (!p) return "waiting";
  return `PF ${(p.pf ?? 0).toFixed(2)} · R ${(p.avgR ?? 0).toFixed(2)} · n ${p.count ?? 0} · ${p.pass ? "pass" : "block"}`;
}

function Num({
  label,
  value,
  min,
  max,
  step,
  unit = "",
  hint,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  unit?: string;
  hint?: string;
  onChange: (n: number) => void;
}) {
  const n = Number.isFinite(value) ? value : min;
  const clamped = Math.min(max, Math.max(min, n));
  const pct = max === min ? 0 : ((clamped - min) / (max - min)) * 100;
  const digits = Number.isInteger(step) && step >= 1 ? 0 : step < 0.05 ? 2 : 2;
  return (
    <label className="rounded-lg border border-border bg-bg2 px-3 py-3">
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-xs text-muted">{label}</span>
        <span className="flex items-center gap-1 font-mono text-sm tabular-nums">
          <input
            type="number"
            min={min}
            max={max}
            step={step}
            value={Number(clamped.toFixed(digits))}
            onChange={(e) => {
              const n = Number(e.target.value);
              if (!Number.isFinite(n)) return;
              onChange(Math.min(max, Math.max(min, n)));
            }}
            className="w-16 rounded border border-border bg-surface px-1 py-0.5 text-right font-mono text-sm"
            suppressHydrationWarning
          />
          {unit}
        </span>
      </div>
      {max - min <= 200 ? (
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={clamped}
        onChange={(e) => onChange(Number(e.target.value))}
        className="mt-2 w-full"
        style={{ ["--fill" as string]: `${pct}%` }}
        suppressHydrationWarning
      />
      ) : null}
      {hint ? <p className="mt-1 text-xs text-muted">{hint}</p> : null}
    </label>
  );
}

function WindowChips({
  label,
  value,
  options,
  hint,
  onChange,
}: {
  label: string;
  value: number[] | undefined;
  options: number[];
  hint?: string;
  onChange: (v: number[]) => void;
}) {
  const on = new Set(value ?? options);
  return (
    <div className="flex flex-col gap-1 rounded-lg border border-border bg-bg2 px-3 py-2 text-sm">
      <span>{label}</span>
      <div className="flex flex-wrap gap-1.5">
        {options.map((n) => (
          <button
            key={n}
            type="button"
            aria-pressed={on.has(n)}
            onClick={() => {
              const next = new Set(on);
              if (next.has(n)) next.delete(n);
              else next.add(n);
              if (next.size) onChange([...next].sort((a, b) => a - b));
            }}
            className={`min-h-9 min-w-11 rounded-md border px-2 font-mono text-xs ${on.has(n) ? "border-primary text-primary" : "border-border text-muted"}`}
          >
            {n}
          </button>
        ))}
      </div>
      {hint ? <span className="text-xs text-muted">{hint}</span> : null}
    </div>
  );
}

function Toggle({
  label,
  on,
  onChange,
  locked,
}: {
  label: string;
  on: boolean;
  onChange: (v: boolean) => void;
  locked?: boolean;
}) {
  return (
    <button
      type="button"
      disabled={locked}
      onClick={() => onChange(!on)}
      className="flex min-h-11 items-center justify-between rounded-lg border border-border bg-bg2 px-3 text-left text-sm disabled:opacity-70"
    >
      <span>
        {label}
        {locked ? <span className="ml-2 font-mono text-xs text-faint">CTS</span> : null}
      </span>
      <span className={`font-mono text-xs ${on ? "text-primary" : "text-muted"}`}>{on ? "ON" : "OFF"}</span>
    </button>
  );
}

function LiveAxis({
  name,
  range,
  enabled,
  window,
  min,
  max,
  step,
  onEn,
  onWin,
}: {
  name: string;
  range: string;
  enabled: boolean;
  window: number;
  min: number;
  max: number;
  step: number;
  onEn: (v: boolean) => void;
  onWin: (v: number) => void;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-bg2 px-3 py-2">
      <Toggle label={`${name} · ${range}`} on={enabled} onChange={onEn} />
      <Num label="Window" value={window} min={min} max={max} step={step} onChange={onWin} />
    </div>
  );
}

function AxisRow({
  name,
  enabled,
  window,
  range,
}: {
  name: string;
  enabled: boolean;
  window: number;
  range: string;
}) {
  return (
    <div className="flex items-center justify-between rounded-lg border border-border bg-bg2 px-3 py-2 text-sm">
      <span>
        {name} <span className="font-mono text-xs text-muted">{range}</span>
      </span>
      <span className="font-mono text-xs text-muted">
        {enabled ? "on" : "off"} · window {window}
      </span>
    </div>
  );
}

function SetsLiveTable({ stats, overlay }: { stats: LiveStats | null; overlay: PulseOverlay }) {
  const sets = stats?.sets;
  const p = sets?.progress;
  const rows = sets?.rows ?? [];
  const pct = Math.max(0, Math.min(100, p?.pct ?? 0));
  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-border bg-bg2 px-3 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2 font-mono text-xs">
          <span className={p?.ready ? "text-primary" : "text-warn"}>{p?.phase ?? "idle"}</span>
          <span className="text-muted">
            {sets?.activeCount ?? 0}/{sets?.setCount ?? 0} active · {sets?.histFills ?? 0} hist fills · last {fmtNum(p?.lastRunMs, 0)}ms
          </span>
        </div>
        <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-border">
          <div className="h-full rounded-full bg-primary" style={{ width: `${pct}%` }} />
        </div>
        <p className="mt-2 font-mono text-[11px] text-muted">
          {p?.detail || "waiting 1m bars"} {p?.symbol ? `· ${p.symbol.replace("-USDT", "")}` : ""} {p?.error ? `· ${p.error}` : ""}
        </p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-left text-sm">
          <thead className="font-mono text-[11px] text-muted">
            <tr>
              <th className="pb-2 font-medium">Set</th>
              <th className="pb-2 font-medium">On</th>
              <th className="pb-2 text-right font-medium">n</th>
              <th className="pb-2 text-right font-medium">Gate PF</th>
              <th className="pb-2 text-right font-medium">Last {overlay.setDeactN} R</th>
              <th className="pb-2 text-right font-medium">Max DDt</th>
              <th className="pb-2 font-medium">Why</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={7} className="py-6 text-center text-muted">
                  Historic 1m replay will fill this grid
                </td>
              </tr>
            ) : (
              rows.slice(0, 40).map((r) => (
                <tr key={r.id} className="border-t border-border font-mono text-xs">
                  <td className="py-1.5">
                    {r.pack} · sl{r.slRatio.toFixed(1)} · st{r.step ?? "—"} · {r.trailKey}
                  </td>
                  <td className={r.active ? "py-1.5 text-primary" : "py-1.5 text-danger"}>{r.active ? "on" : "off"}</td>
                  <td className="py-1.5 text-right">
                    {r.n}
                    {r.liveN ? `+${r.liveN}` : ""}
                  </td>
                  <td className={`py-1.5 text-right ${r.pf + 1e-9 >= overlay.setMinPf ? "text-primary" : "text-danger"}`}>
                    {r.pf.toFixed(2)}
                  </td>
                  <td className={`py-1.5 text-right ${r.last25AvgR < 0 ? "text-danger" : "text-primary"}`}>
                    {r.last25AvgR.toFixed(2)}
                  </td>
                  <td className="py-1.5 text-right">{formatDuration(r.maxDdS * 1000)}</td>
                  <td className="py-1.5 text-muted">{r.deactReason || "—"}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ExitLanesTable({ stats }: { stats: LiveStats | null }) {
  const ex = stats?.exits;
  const lanes = ex?.lanes ?? [];
  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-3">
        <KV k="Mode" v={ex?.ignoreTp === false ? "TP allowed" : "SL takes profit"} />
        <KV k="Last pick" v={String(ex?.lastPick ?? "—")} />
        <KV k="Opt SL" v={`${Number(ex?.optSlPct ?? 0).toFixed(2)}% from peak`} />
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="font-mono text-[11px] text-muted">
            <tr>
              <th className="pb-2 font-medium">Lane</th>
              <th className="pb-2 font-medium">On</th>
              <th className="pb-2 text-right font-medium">n</th>
              <th className="pb-2 text-right font-medium">Last 15 PF</th>
              <th className="pb-2 text-right font-medium">Last 25 R</th>
              <th className="pb-2 text-right font-medium">Max DDt</th>
              <th className="pb-2 font-medium">Why</th>
            </tr>
          </thead>
          <tbody>
            {lanes.length === 0 ? (
              <tr>
                <td colSpan={7} className="py-6 text-center text-muted">
                  Exit lanes score as closes stream
                </td>
              </tr>
            ) : (
              lanes.map((r) => (
                <tr key={r.key} className="border-t border-border font-mono text-xs">
                  <td className={`py-1.5 ${r.selected ? "text-primary" : ""}`}>{r.key}</td>
                  <td className={r.active ? "py-1.5 text-primary" : "py-1.5 text-danger"}>{r.active ? "on" : "off"}</td>
                  <td className="py-1.5 text-right">{r.n}</td>
                  <td className="py-1.5 text-right">{r.last15Pf.toFixed(2)}</td>
                  <td className={`py-1.5 text-right ${r.last25AvgR < 0 ? "text-danger" : "text-primary"}`}>{r.last25AvgR.toFixed(2)}</td>
                  <td className="py-1.5 text-right">{formatDuration(r.maxDdS * 1000)}</td>
                  <td className="py-1.5 text-muted">{r.deactReason || "—"}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function arrJoin(v: unknown, fallback: string) {
  return Array.isArray(v) ? v.join(", ") : fallback;
}

function fmtNum(n: number | null | undefined, d = 1) {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toFixed(d);
}

function connHint(conn: string, stats: LiveStats | null) {
  if (conn === "vst") return "BingX X02 · VST demo · hedge · crossed";
  if (conn === "live") return "BingX X01 · live mainnet · hedge · crossed";
  return `Overall · ${stats?.lanes?.length ?? 2} independent desks`;
}

function ControlsLive({ stats }: { stats: LiveStats | null }) {
  const c = stats?.coverage?.controls;
  const open = stats?.open ?? [];
  const missing = open.filter((p) => !p.controls || !(p.secSlOid && p.secTpOid));
  const ok = c?.ok ?? open.filter((p) => p.controls).length;
  const sec = c?.security ?? open.filter((p) => p.secSlOid && p.secTpOid).length;
  return (
    <div className="rounded-lg border border-border bg-bg2 px-3 py-3 font-mono text-xs" data-testid="controls-live">
      <div className="flex flex-wrap justify-between gap-2">
        <span className={(c?.missing ?? missing.length) ? "text-danger" : "text-primary"}>
          live controls · {ok}/{c?.open ?? open.length} SL+TP · {sec} security
        </span>
        <span className="text-muted">missing {c?.missing ?? missing.length}</span>
      </div>
      {missing.length ? (
        <div className="mt-2 flex flex-wrap gap-2 text-danger">
          {missing.slice(0, 12).map((p) => (
            <span key={`${p.symbol}-${p.side}`}>
              {p.symbol.replace("-USDT", "")} {p.side === "LONG" ? "L" : "S"} {p.controls ? "" : "SL/TP"} {p.secSlOid && p.secTpOid ? "" : "SEC"}
            </span>
          ))}
        </div>
      ) : (
        <p className="mt-2 text-muted">Every open has SL + TP and a symbol+direction security pair</p>
      )}
    </div>
  );
}

function LiveApplied({
  conn,
  stats,
  overlay,
  dirty,
}: {
  conn: string;
  stats: LiveStats | null;
  overlay: PulseOverlay;
  dirty: boolean;
}) {
  const p = (stats?.pulse ?? {}) as PulseOverlay & Record<string, unknown>;
  const v = stats?.variants;
  const sl = v?.slRatio ?? p.slToTpRatio ?? overlay.slToTpRatio;
  const trail = v?.trailKey ?? `${Number(p.trailArmPct ?? overlay.trailArmPct).toFixed(1)}:${Number(p.trailGivePct ?? overlay.trailGivePct).toFixed(1)}`;
  const tf = [
    p.tf1m === false ? "1m off" : "1m",
    p.tf5m === false ? "5m off" : "5m",
    p.tf15m === false ? "15m off" : "15m",
    p.tfCombined === false ? "comb off" : "combined",
  ].join(" · ");
  const fails = (stats?.tests ?? []).filter((t) => !t.pass).slice(0, 3);
  return (
    <div className="rounded-radius border border-border bg-surface px-4 py-3 font-mono text-xs" data-testid="live-applied">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className={stats?.paused ? "text-warn" : stats?.running && !stats?.halted ? "text-primary" : "text-danger"}>
          {stats?.paused ? "paused" : "live"} {conn} · SL:TP {Number(sl).toFixed(1)} · tr {trail} · vol ×{Number(stats?.volumeFactor ?? overlay.volumeFactor ?? 1).toFixed(1)}
        </span>
        <span className="text-muted">
          {dirty ? "unsaved overlay" : "applied"} · cycle {stats?.cycle ?? "—"} · {fmtNum(stats?.engine?.hotMs ?? stats?.scanMs, 0)}ms
        </span>
      </div>
      <div className="mt-1 flex flex-wrap gap-2 text-muted">
        <span>{tf}</span>
        <span>auto sl {v?.slAuto ? "on" : "off"} / tr {v?.trailAuto ? "on" : "off"}</span>
        <span>
          qa {stats?.engine?.qaPass ?? 0}P / {stats?.engine?.qaFail ?? 0}F
        </span>
      </div>
      {stats?.haltReason ? <p className="mt-1 text-danger">{stats.haltReason}</p> : null}
      {stats?.lastError ? <p className="mt-1 text-danger">{stats.lastError}</p> : null}
      {fails.length ? (
        <p className="mt-1 text-danger">
          fail {fails.map((t) => t.name).join(", ")}
        </p>
      ) : (
        <p className="mt-1 text-muted">in-process tests holding</p>
      )}
    </div>
  );
}

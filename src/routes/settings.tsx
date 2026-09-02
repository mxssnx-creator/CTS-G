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
  SL_TP_RATIOS,
  TRAIL_VARIANTS,
  trailGiveFromArm,
  type CtsSettings,
  type PulseOverlay,
} from "@/lib/config-model";
import { fetchLiveStats, pickView, type LiveStats } from "@/lib/live-stats";
import { formatDuration } from "@/lib/analytics";
import { DeskShell } from "@/components/desk-shell";
import { useConnection } from "@/components/connection-provider";
import { SymbolPicker } from "@/components/symbol-picker";
import { CoveragePanel } from "@/components/coverage-overview";
import { MAX_SYMBOLS } from "@/lib/config-model";

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
      const sP = fetchLiveStats(conn);
      const cP = fetchCtsBundle(conn);
      const s = await sP;
      if (!alive) return;
      setRaw(s);
      const c = await cP;
      if (!alive) return;
      setCts(c.cts);
      if (!dirtyRef.current) {
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
  const defaultMinPf = num(strategies.main?.real?.min_profit_factor ?? cts?.realProfitFactor, 1.2);
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
                <EnableSlider label="Block" on={overlay.stratBlock && overlay.blockEnabled} onChange={(v) => { patch("stratBlock", v); patch("blockEnabled", v); }} />
                <EnableSlider label="Trailing" on={overlay.stratTrailing} onChange={(v) => patch("stratTrailing", v)} />
                <EnableSlider label="DCA" on={overlay.dcaEnabled !== false && overlay.stratDca !== false} onChange={(v) => { patch("dcaEnabled", v); patch("stratDca", v); }} />
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
                  label="Min PF ratio"
                  value={overlay.minPf}
                  min={1}
                  max={2.3}
                  step={0.02}
                  hint={pfHint(overlay.minPf, overlay.positionCostPct)}
                  onChange={(v) => patch("minPf", v)}
                />
                <Slider
                  label="PF window"
                  value={overlay.pfWindow}
                  min={5}
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
            <Card title="Stop loss ratios vs take profit" hint="SL = TP × ratio · grid 0.3–1.5 step 0.3 · independent of trailing">
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
                  min={3}
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
                <EnableSlider label="DCA" on={overlay.dcaEnabled !== false && overlay.stratDca !== false} hint="independent steps" onChange={(v) => { patch("dcaEnabled", v); patch("stratDca", v); }} />
              </Grid>
              <p className="text-sm text-muted">
                Indications and general run in parallel for entries. Block adds on a live parent for every count (max stack {overlay.blockMaxStack || "unlimited"}).
                Trailing only moves SL after min-step. Last-{overlay.pfWindow} PositionCost PF must pass before any new risk.
              </p>
            </Card>
          )}

          {section === "sets" && (
            <Card
              title="Independent Sets · 1m historic"
              hint="Each pack × SL:TP × TP-step (3–22 × position cost) is its own book. Sets below Minimal Step Range are not calculated. Live losses raise min step to the count of positive/successful fills."
            >
              <Grid>
                <Toggle label="Historic 1m replay" on={overlay.histEnabled} onChange={(v) => patch("histEnabled", v)} />
                <Toggle label="Gate live on historic" on={overlay.setUseHistoricGate} onChange={(v) => patch("setUseHistoricGate", v)} />
                <Toggle label="Auto-deactivate" on={overlay.setAutoDeact} onChange={(v) => patch("setAutoDeact", v)} />
                <Toggle label="Reactivate on recovery" on={overlay.setReactivate} onChange={(v) => patch("setReactivate", v)} />
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
                  min={5}
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
                  label="Minimal Step Range"
                  value={overlay.setMinStep}
                  min={3}
                  max={22}
                  step={1}
                  hint={`Default 8 (VST-validated). TP = step × position cost (${overlay.positionCostPct}%) → step ${overlay.setMinStep} = ${(overlay.setMinStep * overlay.positionCostPct).toFixed(2)}%. Losing live fills raise min step. Sets below min are not calculated.`}
                  onChange={(v) => patch("setMinStep", Math.max(3, Math.min(22, Math.round(v))))}
                />
                <Slider
                  label="Step max"
                  value={overlay.setStepMax}
                  min={3}
                  max={22}
                  step={1}
                  hint="Upper TP step. Default 12 (validated). Range 3–22."
                  onChange={(v) => patch("setStepMax", Math.max(overlay.setMinStep || 3, Math.min(22, Math.round(v))))}
                />
                <Toggle
                  label="Adapt min step from live"
                  on={overlay.setStepAdapt !== false}
                  onChange={(v) => patch("setStepAdapt", v)}
                />
                <Slider
                  label="Set min PF"
                  value={overlay.setMinPf}
                  min={1}
                  max={2.3}
                  step={0.02}
                  hint={pfHint(overlay.setMinPf, overlay.positionCostPct)}
                  onChange={(v) => patch("setMinPf", v)}
                />
                <Slider
                  label="Max DD time"
                  value={overlay.setMaxDdTimeS}
                  min={60}
                  max={3600}
                  step={30}
                  unit="s"
                  hint="Reported per Set and used to rank — last-25 negative is the kill"
                  onChange={(v) => patch("setMaxDdTimeS", v)}
                />
                <Slider
                  label="Min samples"
                  value={overlay.setMinSamples}
                  min={5}
                  max={40}
                  step={1}
                  onChange={(v) => patch("setMinSamples", v)}
                />
                <Num
                  label="Max active Sets"
                  value={overlay.setMaxActive}
                  min={0}
                  max={10000}
                  step={1}
                  hint="0 = unlimited"
                  onChange={(v) => patch("setMaxActive", v)}
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
                <Slider label="Exit PF window" value={overlay.exitPfWindow} min={5} max={40} step={1} onChange={(v) => patch("exitPfWindow", v)} />
                <Slider label="Exit deact N" value={overlay.exitDeactN} min={10} max={80} step={1} onChange={(v) => patch("exitDeactN", v)} />
                <Slider
                  label="Exit min PF"
                  value={overlay.exitMinPf}
                  min={1}
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
                <KV k="Prev window" v={String(num(cts?.prevPosWindow ?? cts?.prev_pos_window, 25))} />
                <KV k="Prev min count" v={String(num(cts?.prevPosMinCount ?? cts?.prev_pos_min_count, 5))} />
                <KV k="Main eval pos count" v={String(num(cts?.mainEvalPosCount, 5))} />
                <KV k="Real eval pos count" v={String(num(cts?.realEvalPosCount, 3))} />
                <KV k="Min step" v={String(num(cts?.minStep ?? cts?.min_step, 5))} />
                <KV k="Trailing min step" v={String(num(cts?.trailingMinStep, 5))} />
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
                  label="Max stack"
                  value={overlay.blockMaxStack}
                  min={0}
                  max={10000}
                  step={1}
                  hint="0 = unlimited"
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
                  min={0.5}
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
              <p className="font-mono text-xs text-muted">CTS variants · arm:give · filtered by optimal range</p>
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
                <Slider label="Recalc min samples" value={overlay.trailRecalcN} min={3} max={20} step={1} onChange={(v) => patch("trailRecalcN", v)} />
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
            <Card title="DCA" hint="Independent of Block · fires on adverse % from average entry · own last-15 PF / last-25 deact">
              <Toggle
                label="DCA enabled"
                on={overlay.dcaEnabled !== false}
                onChange={(v) => patch("dcaEnabled", v)}
              />
              <Toggle
                label="Auto-deact on last-25 avg loss"
                on={overlay.dcaAutoDeact !== false}
                onChange={(v) => patch("dcaAutoDeact", v)}
              />
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
                  min={1}
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
                {(overlay.dcaStepDistancesPct || [0.5, 1, 1.5, 2]).slice(0, overlay.dcaMaxSteps || 4).map((d, i) => (
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
                {(overlay.dcaStepVolumeMultipliers || [1.5, 2, 2.3, 2.5]).slice(0, overlay.dcaMaxSteps || 4).map((m, i) => (
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
                <Num label="Min PF" value={overlay.minPf} min={1} max={2.3} step={0.02} onChange={(v) => patch("minPf", v)} />
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
                <KV k="Pos-count volume ratio" v={String(num(cts?.posCountsVolumeRatio, 0.05))} />
                <KV k="Exchange position cost" v={String(num(cts?.exchangePositionCost ?? cts?.positionCost, 0.1))} />
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
                <KV
                  k="Effective notional"
                  v={(overlay.targetNotional * (overlay.volumeFactor || 1)).toFixed(2)}
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
                <Num label="Stagger s" value={overlay.staggerS} min={0.2} max={5} step={0.1} onChange={(v) => patch("staggerS", v)} />
                <Num label="Max hold s" value={overlay.timeStopS} min={60} max={21600} step={60} hint="hard cap 6h" onChange={(v) => patch("timeStopS", v)} />
                <Num label="Scratch s" value={overlay.scratchS} min={20} max={300} step={5} onChange={(v) => patch("scratchS", v)} />
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
  const p = (stats as LiveStats & { pfCost?: { ratio?: number; avgR?: number; count?: number; pass?: boolean } })?.pfCost;
  if (!p) return "waiting";
  return `${(p.ratio ?? 0).toFixed(2)} · R ${(p.avgR ?? 0).toFixed(2)} · n ${p.count ?? 0} · ${p.pass ? "pass" : "block"}`;
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
              <th className="pb-2 text-right font-medium">Last {overlay.setPfWindow} PF</th>
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
                  <td className={`py-1.5 text-right ${r.last15Ratio + 1e-9 >= overlay.setMinPf ? "text-primary" : "text-danger"}`}>
                    {r.last15Ratio.toFixed(2)}
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
                  <td className="py-1.5 text-right">{r.last15Ratio.toFixed(2)}</td>
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

import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  blockTable,
  bool,
  DEFAULT_OVERLAY,
  fetchCtsBundle,
  loadLocalOverlay,
  num,
  normalizePf,
  PF_MAX,
  PF_MIN,
  PF_STEP,
  overlayFromCts,
  saveOverlay,
  slTpGrid,
  trailGrid,
  trailGiveFromArm,
  syncOverlayFlags,
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
import { fetchConnection, saveConnection, type ConnectionCreds } from "@/lib/connections";
import { CONFIG_PRESETS, applyPresetPatch } from "@/lib/config-presets";
import {
  applyUserPreset,
  deleteUserPreset,
  deleteAllExceptDefaultUserPresets,
  fetchUserPresets,
  loadUserPreset,
  renameUserPreset,
  saveDefaultUserPreset,
  saveUserPreset,
  suggestPresetName,
  overlayOverview,
  type UserPreset,
} from "@/lib/user-presets";
import { DEFAULT_CALC_OPTIONS, fetchHistCalc, startHistCalc, type HistCalcJob, type HistCalcOptions } from "@/lib/hist-calc";
import { ForcedConfigsPanel } from "@/components/forced-configs";

export const Route = createFileRoute("/settings")({ component: SettingsPage });

const SECTIONS = [
  "overview",
  "presets",
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
  const [creds, setCreds] = useState<ConnectionCreds | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [connType, setConnType] = useState<"mainnet" | "vst">("mainnet");
  const [connMethod, setConnMethod] = useState("library");
  const [asDefaultMainnet, setAsDefaultMainnet] = useState(true);
  const [credMsg, setCredMsg] = useState<string | null>(null);
  const [credSaving, setCredSaving] = useState(false);
  const [calcOpt, setCalcOpt] = useState<HistCalcOptions>(DEFAULT_CALC_OPTIONS);
  const [calcJob, setCalcJob] = useState<HistCalcJob | null>(null);
  const [calcBusy, setCalcBusy] = useState(false);
  const [presetId, setPresetId] = useState<string | null>(null);
  const [resetAsk, setResetAsk] = useState(false);
  const [userPresets, setUserPresets] = useState<UserPreset[]>([]);
  const [userName, setUserName] = useState("Preset-1");
  const [userSel, setUserSel] = useState<string | null>(null);
  const [userBusy, setUserBusy] = useState(false);
  const [renameId, setRenameId] = useState<string | null>(null);
  const [renameVal, setRenameVal] = useState("");
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [cleanupAsk, setCleanupAsk] = useState(false);
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
    setCreds(null);
    setApiKey("");
    setApiSecret("");
    setCredMsg(null);
    setConnType(conn === "vst" ? "vst" : "mainnet");
    setAsDefaultMainnet(conn !== "vst");
    setResetAsk(false);
    setCleanupAsk(false);
    const local = loadLocalOverlay(conn);
    setOverlay(overlayFromCts({}, local || {}));
    const pull = async () => {
      const sP = fetchLiveStats(conn);
      const cP = fetchCtsBundle(conn);
      const kP = fetchConnection(conn);
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
      const k = await kP;
      if (!alive) return;
      if (k) {
        setCreds(k);
        setConnType(k.connectionType === "vst" ? "vst" : "mainnet");
        setConnMethod(k.connectionMethod || "library");
        if (k.apiKeyMasked) setApiKey(k.apiKeyMasked);
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

  useEffect(() => {
    if (!resetAsk) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setResetAsk(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [resetAsk]);

  useEffect(() => {
    let alive = true;
    void fetchUserPresets().then((rows) => {
      if (!alive) return;
      setUserPresets(rows);
      setUserName(suggestPresetName(rows));
    });
    return () => {
      alive = false;
    };
  }, []);

  const stats = pickView(raw, conn);
  const calcPhase = calcJob?.phase;

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const pull = async () => {
      const j = await fetchHistCalc();
      if (!alive) return;
      setCalcJob(j);
      const running = j.phase === "fetch" || j.phase === "replay" || j.phase === "score" || j.phase === "queued";
      if (running) timer = setTimeout(() => void pull(), 1200);
      else setCalcBusy(false);
    };
    if (calcBusy || (calcPhase && ["fetch", "replay", "score", "queued"].includes(calcPhase))) {
      void pull();
    }
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [calcBusy, calcPhase]);

  const patch = <K extends keyof PulseOverlay>(k: K, v: PulseOverlay[K]) => {
    dirtyRef.current = true;
    setOverlay((o) => ({ ...o, [k]: v }));
    setDirty(true);
    setSaveMsg(null);
  };

  const onApplyPreset = (id: string) => {
    const preset = CONFIG_PRESETS.find((p) => p.id === id);
    dirtyRef.current = true;
    setOverlay((o) => applyPresetPatch(o, id));
    setPresetId(id);
    setDirty(true);
    if (preset) {
      setCalcOpt((o) => ({
        ...o,
        minStep: preset.minStep,
        stepMax: preset.stepMax,
        trailing: true,
        stratBlock: true,
        stratDca: false,
        hours: 20,
        allConfigs: true,
      }));
    }
    setSaveMsg(`Preset ${preset?.name || id} applied · save Live or VST to persist`);
  };

  const onSaveSystemPreset = async () => {
    setUserBusy(true);
    const r = await saveUserPreset({ name: userName, overlay, calcOpt });
    setUserBusy(false);
    setUserPresets(r.presets);
    if (r.preset) {
      setUserSel(r.preset.id);
      setUserName(suggestPresetName(r.presets));
    }
    setSaveMsg(r.ok ? `${r.preset?.name || userName} saved · overall system (Live + VST)` : r.detail);
  };

  const onSaveDefault = async () => {
    setUserBusy(true);
    const r = await saveDefaultUserPreset({ overlay, calcOpt });
    setUserBusy(false);
    setUserPresets(r.presets);
    if (r.preset) setUserSel(r.preset.id);
    setSaveMsg(r.ok ? "Default saved · active settings named Default" : r.detail);
  };

  const onDeleteExceptDefault = async () => {
    setUserBusy(true);
    const r = await deleteAllExceptDefaultUserPresets();
    setUserBusy(false);
    setCleanupAsk(false);
    setUserPresets(r.presets);
    setUserSel(r.presets.find((p) => p.name.toLowerCase() === "default")?.id ?? null);
    setUserName(suggestPresetName(r.presets));
    setSaveMsg(r.detail);
  };

  const onLoadSystemPreset = async (id: string) => {
    setUserBusy(true);
    const r = await loadUserPreset(id);
    if (!r.ok || !r.preset) {
      setUserBusy(false);
      setSaveMsg(r.detail);
      return;
    }
    const next = applyUserPreset(overlay, r.preset);
    dirtyRef.current = false;
    setOverlay(next);
    setDirty(false);
    setPresetId(null);
    setUserSel(id);
    const nextCalcOpt = r.calcOpt ? { ...calcOpt, ...r.calcOpt } : calcOpt;
    if (r.calcOpt) setCalcOpt(nextCalcOpt);
    const live = await saveOverlay(next, "live");
    const vst = await saveOverlay(next, "vst");
    setUserBusy(false);
    const lanes = [live.ok ? "Live" : null, vst.ok ? "VST" : null].filter(Boolean).join(" + ");
    setSaveMsg(`${r.preset.name} loaded · set up on ${lanes || "form"} · recalculating/coordinating`);
    if (next.histEnabled) void onCalcAll(next, nextCalcOpt);
  };

  const onRenameSystemPreset = async () => {
    if (!renameId) return;
    setUserBusy(true);
    const r = await renameUserPreset(renameId, renameVal);
    setUserBusy(false);
    setUserPresets(r.presets);
    setRenameId(null);
    setSaveMsg(r.ok ? r.detail : r.detail);
  };

  const onDeleteSystemPreset = async () => {
    if (!deleteId) return;
    setUserBusy(true);
    const r = await deleteUserPreset(deleteId);
    setUserBusy(false);
    setUserPresets(r.presets);
    setUserName(suggestPresetName(r.presets));
    if (userSel === deleteId) setUserSel(null);
    setDeleteId(null);
    setSaveMsg(r.ok ? "Preset deleted" : r.detail);
  };

  const onCalcAll = async (activeOverlay = overlay, activeCalcOpt = calcOpt) => {
    setCalcBusy(true);
    setSaveMsg(null);
    const allSym = activeCalcOpt.allSymbols || activeOverlay.symbolsAll || activeOverlay.symbols.includes("*");
    const j = await startHistCalc({
      ...activeCalcOpt,
      allConfigs: true,
      allSymbols: allSym,
      symbols: allSym ? ["*"] : activeOverlay.symbols,
      preferMinimalRange: activeOverlay.preferMinimalRange,
      additionalCoordination: activeOverlay.additionalCoordination,
      coordOptimizationN: activeOverlay.coordOptimizationN,
    });
    setCalcJob(j);
    if (j.phase === "error") setCalcBusy(false);
  };

  const onApplyWinner = () => {
    const apply = calcJob?.apply;
    if (!apply || typeof apply !== "object") return;
    dirtyRef.current = true;
    setOverlay((o) => syncOverlayFlags({ ...o, ...(apply as Partial<typeof o>) }));
    setDirty(true);
    setSaveMsg("Winner applied · Block on · DCA off · save to persist");
  };

  const strategies = (cts?.strategies ?? {}) as {
    main?: Record<string, { enabled?: boolean; min_profit_factor?: number; max_drawdown_time?: number; max_positions?: number }>;
    mainTradePfRatioSemantics?: string;
  };
  const defaultMinPf = num(strategies.main?.real?.min_profit_factor ?? cts?.realProfitFactor, 1.1);
  const table = useMemo(
    () => blockTable(overlay.blockVolumeRatio, overlay.blockProfitFactorRatio, defaultMinPf, 1, 0, overlay.blockMaxVolumeMultiplier),
    [overlay.blockVolumeRatio, overlay.blockProfitFactorRatio, defaultMinPf, overlay.blockMaxVolumeMultiplier],
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
      setSaveMsg(`${r.detail} · recalculating/coordinating`);
      // The sidecar reloads and schedules its next coordination tick after an
      // atomic overlay write. Trigger the independent historic worker too so
      // the UI never shows a stale Set matrix after a settings change.
      if (overlay.histEnabled) void onCalcAll(overlay);
    }
  };

  const onSaveCreds = async () => {
    setCredSaving(true);
    setCredMsg(null);
    const payload: {
      api_key?: string;
      api_secret?: string;
      connection_type: string;
      connection_method: string;
      as_default_mainnet: boolean;
    } = {
      connection_type: connType,
      connection_method: connMethod,
      as_default_mainnet: asDefaultMainnet || connType === "mainnet",
    };
    if (apiKey && !apiKey.includes("…") && !apiKey.includes("•")) payload.api_key = apiKey.trim();
    if (apiSecret && !apiSecret.includes("•")) payload.api_secret = apiSecret.trim();
    const r = await saveConnection(conn === "vst" && !payload.as_default_mainnet ? "vst" : conn === "vst" ? "live" : conn, payload);
    setCredSaving(false);
    setCredMsg(r.detail);
    if (r.ok && r.creds) {
      setCreds(r.creds);
      setApiSecret("");
      if (r.creds.apiKeyMasked) setApiKey(r.creds.apiKeyMasked);
    }
  };

  const onResetOverlay = () => {
    setOverlay(overlayFromCts(cts ?? {}));
    setDirty(true);
    dirtyRef.current = true;
    setPresetId(null);
    setSaveMsg("Reset to CTS defaults");
    setResetAsk(false);
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
                                    : id === "presets"
                                      ? "Presets · calc"
                                    : id === "connection"
                                      ? "Connection · keys"
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
                <EnableSlider label="DCA" on={Boolean(overlay.dcaEnabled) && overlay.stratDca !== false} onChange={(v) => { patch("dcaEnabled", v); patch("stratDca", v); }} />
                <EnableSlider label="Control orders" on={overlay.controlOrders} onChange={(v) => patch("controlOrders", v)} />
                <EnableSlider label="Historic sets" on={overlay.histEnabled} onChange={(v) => patch("histEnabled", v)} />
                <EnableSlider label="Exit coordinator" on={overlay.exitEnabled} onChange={(v) => patch("exitEnabled", v)} />
              </Grid>
              <p className="text-sm text-muted">
                These sliders write the overlay. Save on Live or VST persists every field — including DCA steps, modules and symbol universe — and the engine reloads the file.
              </p>
              <div className="rounded-lg border border-border bg-bg2 p-3">
                <p className="font-mono text-xs uppercase text-muted">Best configs · low drawdown</p>
                <p className="mt-1 text-sm text-muted">
                  Live was stacking Block + DCA and using SL:TP 1.5 (stop larger than take-profit). Apply a coordinated preset, then run Historic calc — it does not start the engine.
                </p>
                <div className="mt-3 flex flex-wrap gap-2">
                  {CONFIG_PRESETS.filter((p) => p.recommended).map((p) => (
                    <button
                      key={p.id}
                      type="button"
                      className="min-h-11 rounded-lg bg-primary px-3 text-sm text-bg"
                      onClick={() => {
                        onApplyPreset(p.id);
                        setSection("presets");
                      }}
                    >
                      Apply {p.name}
                    </button>
                  ))}
                  <button
                    type="button"
                    className="min-h-11 rounded-lg border border-border px-3 text-sm"
                    onClick={() => setSection("presets")}
                  >
                    All 8 presets · calc
                  </button>
                </div>
              </div>
            </Card>
          )}
          {section === "presets" && (
            <>
              <Card title="Config presets · low drawdown" hint="8 coordinated books. Block on · DCA off · SL 0.3 or 0.6. Apply then save to Live or VST.">
                <div className="grid gap-3 sm:grid-cols-2">
                  {CONFIG_PRESETS.map((p) => {
                    const on = presetId === p.id;
                    return (
                      <button
                        key={p.id}
                        type="button"
                        data-testid={`preset-${p.id}`}
                        onClick={() => onApplyPreset(p.id)}
                        className={`min-h-11 rounded-lg border px-3 py-3 text-left ${
                          on ? "border-primary bg-primary-dim/40 text-fg" : "border-border bg-bg2 text-fg"
                        }`}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <div className="text-sm font-medium">{p.name}</div>
                          {p.recommended ? (
                            <span className="rounded bg-primary-dim/50 px-1.5 font-mono text-[10px] uppercase text-primary">best</span>
                          ) : null}
                        </div>
                        <p className="mt-1 text-xs text-muted">{p.hint}</p>
                        <p className="mt-2 text-xs text-fg/80">{p.why}</p>
                        <p className="mt-2 font-mono text-[11px] text-muted">
                          SL {p.sl.toFixed(1)} · step {p.minStep}–{p.stepMax} · trail {p.trail} · PF {p.minPf.toFixed(2)} · DDt {formatDuration(p.maxDdS * 1000)}
                        </p>
                        <p className="font-mono text-[11px] text-primary">Block ON · DCA OFF · cost subtracted</p>
                      </button>
                    );
                  })}
                </div>
              </Card>
              <Card
                title="System presets · Live + VST"
                hint="Save the current book as Preset-N for overall use. Load applies every section (SL, steps, trail, Block/DCA, indications) to Live and VST immediately."
              >
                <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                  <input
                    data-testid="user-preset-name"
                    value={userName}
                    onChange={(e) => setUserName(e.target.value)}
                    placeholder="Preset-1"
                    className="min-h-11 flex-1 rounded-lg border border-border bg-bg2 px-3 font-mono text-sm"
                  />
                  <button
                    type="button"
                    data-testid="user-preset-save"
                    disabled={userBusy || !ready}
                    onClick={() => void onSaveSystemPreset()}
                    className="min-h-11 rounded-lg bg-primary px-4 text-sm font-medium text-bg disabled:opacity-40"
                  >
                    {userBusy ? "Saving…" : "Save as system preset"}
                  </button>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    data-testid="user-preset-save-default"
                    disabled={userBusy || !ready}
                    onClick={() => void onSaveDefault()}
                    className="min-h-11 rounded-lg border border-primary px-4 text-sm text-primary disabled:opacity-40"
                  >
                    {userBusy ? "Saving…" : "Save active as Default"}
                  </button>
                  <button
                    type="button"
                    data-testid="user-preset-delete-except-default"
                    disabled={userBusy || !userPresets.some((p) => p.name.trim().toLowerCase() === "default")}
                    onClick={() => setCleanupAsk(true)}
                    className="min-h-11 rounded-lg border border-border px-4 text-sm text-muted disabled:opacity-40"
                  >
                    Delete all except Default
                  </button>
                  <span className="font-mono text-[11px] text-muted">
                    {userPresets.some((p) => p.name.trim().toLowerCase() === "default") ? "Default protected" : "Save Default first"}
                  </span>
                </div>
                <p className="font-mono text-[11px] text-muted">{overlayOverview(overlay)}</p>
                {userPresets.length === 0 ? (
                  <p className="text-sm text-muted">No saved system presets yet. Tune sliders, then save.</p>
                ) : (
                  <div className="grid gap-2">
                    {userPresets.map((p) => {
                      const on = userSel === p.id;
                      const renaming = renameId === p.id;
                      return (
                        <div
                          key={p.id}
                          data-testid={`user-preset-${p.id}`}
                          className={`rounded-lg border px-3 py-3 ${on ? "border-primary bg-primary-dim/30" : "border-border bg-bg2"}`}
                        >
                          {renaming ? (
                            <div className="flex flex-col gap-2 sm:flex-row">
                              <input
                                value={renameVal}
                                onChange={(e) => setRenameVal(e.target.value)}
                                className="min-h-11 flex-1 rounded-lg border border-border bg-surface px-3 font-mono text-sm"
                                autoFocus
                              />
                              <button type="button" className="min-h-11 rounded-lg bg-primary px-3 text-sm text-bg" onClick={() => void onRenameSystemPreset()}>
                                Save name
                              </button>
                              <button type="button" className="min-h-11 rounded-lg border border-border px-3 text-sm" onClick={() => setRenameId(null)}>
                                Cancel
                              </button>
                            </div>
                          ) : (
                            <>
                              <div className="flex flex-wrap items-center justify-between gap-2">
                                <div>
                                  <p className="text-sm font-medium">{p.name}</p>
                                  <p className="mt-1 font-mono text-[11px] text-muted">{p.overview || p.hint}</p>
                                </div>
                                <div className="flex flex-wrap gap-2">
                                  <button
                                    type="button"
                                    data-testid={`user-preset-load-${p.id}`}
                                    disabled={userBusy}
                                    className="min-h-11 rounded-lg bg-primary px-3 text-sm text-bg disabled:opacity-40"
                                    onClick={() => void onLoadSystemPreset(p.id)}
                                  >
                                    Load
                                  </button>
                                  <button
                                    type="button"
                                    className="min-h-11 rounded-lg border border-border px-3 text-sm"
                                    onClick={() => {
                                      setRenameId(p.id);
                                      setRenameVal(p.name);
                                    }}
                                  >
                                    Rename
                                  </button>
                                  <button
                                    type="button"
                                    disabled={p.name.trim().toLowerCase() === "default"}
                                    className="min-h-11 rounded-lg border border-border px-3 text-sm text-muted disabled:cursor-not-allowed disabled:opacity-50"
                                    onClick={() => setDeleteId(p.id)}
                                  >
                                    {p.name.trim().toLowerCase() === "default" ? "Protected" : "Delete"}
                                  </button>
                                </div>
                              </div>
                            </>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </Card>
              <Card
                title="Historic calc · last 20 hours"
                hint="Independent of engine start. Walks every selected pack × SL:TP × trail × step × symbol. PF + DDT scored per set, indication kind and symbol."
              >
                <Grid>
                  <Slider
                    label="Minimal Step Range"
                    value={calcOpt.minStep}
                    min={2}
                    max={22}
                    step={1}
                    hint={`Sets below step ${calcOpt.minStep} are not calculated`}
                    onChange={(v) => setCalcOpt((o) => ({ ...o, minStep: Math.round(v), stepMax: Math.max(o.stepMax, Math.round(v)) }))}
                  />
                  <Slider
                    label="Step max"
                    value={calcOpt.stepMax}
                    min={2}
                    max={22}
                    step={1}
                    onChange={(v) => setCalcOpt((o) => ({ ...o, stepMax: Math.max(o.minStep, Math.round(v)) }))}
                  />
                  <Slider
                    label="Hours"
                    value={calcOpt.hours}
                    min={8}
                    max={72}
                    step={1}
                    hint={`${calcOpt.hours}h × 1m = ${calcOpt.hours * 60} bars`}
                    onChange={(v) => setCalcOpt((o) => ({ ...o, hours: Math.round(v) }))}
                  />
                  <EnableSlider
                    label="Trailing"
                    on={calcOpt.trailing}
                    hint="off = SL:TP books only"
                    onChange={(v) => setCalcOpt((o) => ({ ...o, trailing: v }))}
                  />
                  <EnableSlider
                    label="Block"
                    on={calcOpt.stratBlock}
                    hint="enabled by default"
                    onChange={(v) => setCalcOpt((o) => ({ ...o, stratBlock: v }))}
                  />
                  <EnableSlider
                    label="DCA"
                    on={calcOpt.stratDca}
                    hint="disabled by default"
                    onChange={(v) => setCalcOpt((o) => ({ ...o, stratDca: v }))}
                  />
                  <EnableSlider
                    label="Indications"
                    on={calcOpt.stratIndications}
                    onChange={(v) => setCalcOpt((o) => ({ ...o, stratIndications: v }))}
                  />
                  <EnableSlider
                    label="General pulse"
                    on={calcOpt.stratGeneral}
                    onChange={(v) => setCalcOpt((o) => ({ ...o, stratGeneral: v }))}
                  />
                  <EnableSlider
                    label="All symbols"
                    on={calcOpt.allSymbols}
                    hint={calcOpt.allSymbols ? "ranked universe (capped)" : "selected list only"}
                    onChange={(v) => setCalcOpt((o) => ({ ...o, allSymbols: v }))}
                  />
                  <EnableSlider
                    label="Signals"
                    on={calcOpt.indTypeSignals}
                    onChange={(v) => setCalcOpt((o) => ({ ...o, indTypeSignals: v }))}
                  />
                  <EnableSlider
                    label="State"
                    on={calcOpt.indTypeState}
                    onChange={(v) => setCalcOpt((o) => ({ ...o, indTypeState: v }))}
                  />
                  <EnableSlider
                    label="Direction"
                    on={calcOpt.indTypeDirection}
                    onChange={(v) => setCalcOpt((o) => ({ ...o, indTypeDirection: v }))}
                  />
                  <EnableSlider
                    label="Move"
                    on={calcOpt.indTypeMove}
                    onChange={(v) => setCalcOpt((o) => ({ ...o, indTypeMove: v }))}
                  />
                  <EnableSlider
                    label="Active"
                    on={calcOpt.indTypeActive}
                    onChange={(v) => setCalcOpt((o) => ({ ...o, indTypeActive: v }))}
                  />
                  <EnableSlider
                    label="Common"
                    on={calcOpt.indTypeCommon}
                    onChange={(v) => setCalcOpt((o) => ({ ...o, indTypeCommon: v }))}
                  />
                  <EnableSlider
                    label="Trend"
                    on={calcOpt.indTypeTrend}
                    onChange={(v) => setCalcOpt((o) => ({ ...o, indTypeTrend: v }))}
                  />
                  <EnableSlider
                    label="Break"
                    on={calcOpt.indTypeBreak}
                    onChange={(v) => setCalcOpt((o) => ({ ...o, indTypeBreak: v }))}
                  />
                </Grid>
                <div className="flex flex-wrap items-center gap-3">
                  <button
                    type="button"
                    data-testid="hist-calc-all"
                    disabled={calcBusy}
                    onClick={() => void onCalcAll()}
                    className="min-h-11 rounded-lg bg-primary px-4 text-sm font-medium text-bg disabled:opacity-40"
                  >
                    {calcBusy ? "Calculating…" : "Calculate all configs"}
                  </button>
                  {calcJob?.winner ? (
                    <button
                      type="button"
                      data-testid="hist-calc-apply"
                      onClick={onApplyWinner}
                      className="min-h-11 rounded-lg border border-border px-4 text-sm"
                    >
                      Apply winner
                    </button>
                  ) : null}
                  <span className="text-sm text-muted">
                    {calcJob?.phase && calcJob.phase !== "idle"
                      ? `${calcJob.phase} ${Math.round(calcJob.pct || 0)}% · ${calcJob.detail || ""}`
                      : "Runs without starting the engine · last 20 hours"}
                  </span>
                </div>
                {calcJob && calcJob.phase !== "idle" ? (
                  <div className="space-y-3">
                    <div className="h-1.5 overflow-hidden rounded-full bg-border">
                      <div className="h-full rounded-full bg-primary" style={{ width: `${Math.max(0, Math.min(100, calcJob.pct || 0))}%` }} />
                    </div>
                    <div className="grid gap-3 sm:grid-cols-3">
                      <KV k="Validated rows" v={`${calcJob.validatedCount ?? 0}/${calcJob.rowCount ?? 0}`} />
                      <KV
                        k="Catalog valid sets"
                        v={`${calcJob.coverage?.validatedCount ?? 0}/${calcJob.coverage?.setCount ?? calcJob.coverage?.product ?? 0}`}
                      />
                      <KV k="Source" v={String(calcJob.source || "—")} />
                      <KV k="Lookback" v={`${calcJob.lookback ?? calcOpt.hours * 60} bars`} />
                      <KV
                        k="Set product"
                        v={
                          calcJob.coverage?.product
                            ? `${calcJob.coverage.product} · base ${calcJob.coverage.families?.base ?? "—"} / trail ${calcJob.coverage.families?.trail ?? "—"}`
                            : "—"
                        }
                      />
                    </div>
                    {calcJob.winner ? (
                      <p className="text-sm">
                        Winner <span className="font-mono text-primary">{calcJob.winner.id}</span>
                        {calcJob.winner.direction ? ` · ${calcJob.winner.direction}` : ""}
                        {" "}· PF {calcJob.winner.last15Ratio.toFixed(2)}
                        {typeof calcJob.winner.netAvg === "number" ? ` · net ${(calcJob.winner.netAvg * 100).toFixed(3)}%` : ""}
                        {" "}· DDt {formatDuration(calcJob.winner.maxDdS * 1000)} · SL {calcJob.winner.slRatio.toFixed(1)}
                      </p>
                    ) : null}
                    <div className="overflow-x-auto">
                      <table className="w-full min-w-[640px] text-left text-sm">
                        <thead className="font-mono text-[11px] text-muted">
                          <tr>
                            <th className="pb-2 font-medium">Set</th>
                            <th className="pb-2 font-medium">Dir</th>
                            <th className="pb-2 font-medium">Pack</th>
                            <th className="pb-2 text-right font-medium">SL</th>
                            <th className="pb-2 text-right font-medium">Step</th>
                            <th className="pb-2 text-right font-medium">PF</th>
                            <th className="pb-2 text-right font-medium">Max DDt</th>
                            <th className="pb-2 text-right font-medium">n</th>
                          </tr>
                        </thead>
                        <tbody>
                          {(calcJob.rows || []).slice(0, 16).map((r) => (
                            <tr key={r.id} className={`border-t border-border font-mono ${r.validated ? "text-fg" : "text-muted"}`}>
                              <td className="py-1.5">{r.id.replace("-USDT", "")}</td>
                              <td className="py-1.5">{r.direction || "BOTH"}</td>
                              <td className="py-1.5">{r.pack}</td>
                              <td className="py-1.5 text-right">{r.kind === "trail" ? r.trailKey || "trail" : r.slRatio.toFixed(1)}</td>
                              <td className="py-1.5 text-right">{r.step || "—"}</td>
                              <td className="py-1.5 text-right">{r.last15Ratio.toFixed(2)}</td>
                              <td className="py-1.5 text-right">{formatDuration(r.maxDdS * 1000)}</td>
                              <td className="py-1.5 text-right">{r.n}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <div className="grid gap-3 sm:grid-cols-2">
                      <div>
                        <p className="mb-2 font-mono text-xs text-muted uppercase">Indication kinds · PF / DDT</p>
                        <div className="space-y-1">
                          {Object.entries(calcJob.kinds || {}).map(([k, v]) => (
                            <div key={k} className="flex justify-between gap-3 font-mono text-xs">
                              <span className={v.validated && v.profitable ? "text-primary" : "text-muted"}>{k}</span>
                              <span className="text-right">
                                PF {(v.pf ?? 0).toFixed(2)} · DDt {formatDuration((v.maxDdS ?? 0) * 1000)}
                                {v.bySide ? ` · L ${(v.bySide.LONG?.pf ?? 0).toFixed(2)} S ${(v.bySide.SHORT?.pf ?? 0).toFixed(2)}` : ""}
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                      <div>
                        <p className="mb-2 font-mono text-xs text-muted uppercase">Direction · net PF</p>
                        <div className="space-y-1">
                          {Object.entries(calcJob.byDirection || {}).map(([k, v]) => (
                            <div key={k} className="flex justify-between font-mono text-xs">
                              <span className={v.validated ? "text-primary" : "text-muted"}>{k}</span>
                              <span>PF {v.pf.toFixed(2)} · net {((v.netAvg ?? 0) * 100).toFixed(3)}% · DDt {formatDuration(v.maxDdS * 1000)} · n {v.n}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                      <div>
                        <p className="mb-2 font-mono text-xs text-muted uppercase">Strategy · pack / kind</p>
                        <div className="space-y-1">
                          {Object.entries(calcJob.byStrategy || {}).map(([k, v]) => (
                            <div key={k} className="flex justify-between gap-3 font-mono text-xs">
                              <span className={v.validated ? "text-primary" : "text-muted"}>{k}</span>
                              <span className="text-right">
                                PF {v.pf.toFixed(2)}
                                {v.bySide ? ` · L ${(v.bySide.LONG?.pf ?? 0).toFixed(2)} S ${(v.bySide.SHORT?.pf ?? 0).toFixed(2)}` : ""}
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                      <div>
                        <p className="mb-2 font-mono text-xs text-muted uppercase">Symbols · PF / DDT</p>
                        <div className="space-y-1">
                          {(calcJob.bySymbol || []).slice(0, 8).map((s) => (
                            <div key={s.symbol} className="flex justify-between gap-3 font-mono text-xs">
                              <span className={s.validated ? "text-primary" : "text-muted"}>{s.symbol.replace("-USDT", "")}</span>
                              <span className="text-right">
                                PF {s.pf.toFixed(2)} · DDt {formatDuration(s.maxDdS * 1000)}
                                {s.bySide ? ` · L ${(s.bySide.LONG?.pf ?? 0).toFixed(2)} S ${(s.bySide.SHORT?.pf ?? 0).toFixed(2)}` : ""}
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                    </div>
                    {calcJob.error ? <p className="text-sm text-danger">{calcJob.error}</p> : null}
                  </div>
                ) : null}
                <p className="text-sm text-muted">
                  All configs = every enabled pack × SL:TP × trail × step × LONG and SHORT independently × every selected symbol × every indication type. Position cost is subtracted from PF, expectancy and averages. Does not start the live engine.
                </p>
              </Card>
            </>
          )}
          {section === "connection" && (
            <Card title="Connection" hint={connHint(conn, stats)}>
              <Grid>
                <KV k="Selected" v={conn === "vst" ? "VST demo" : conn === "live" ? "Live mainnet" : "Overall"} />
                <KV k="Name" v={conn === "vst" ? "BingX X02" : conn === "live" ? "BingX X01" : "All desks"} />
                <KV k="Exchange" v={String(stats?.exchange ?? creds?.exchange ?? "BingX")} />
                <KV k="Connection type" v={connType === "vst" ? "VST demo" : "Live mainnet"} />
                <KV k="Connection method" v={connMethod} />
                <KV k="API key" v={creds?.apiKeyMasked || (creds?.apiKeySet ? "set" : "missing")} />
                <KV k="API secret" v={creds?.apiSecretSet ? "set · hidden" : "missing"} />
                <KV k="Base URL" v={String(creds?.baseUrl ?? (connType === "vst" ? "https://open-api-vst.bingx.com" : "https://open-api.bingx.com"))} />
                <KV k="Testnet" v={connType === "vst" ? "yes" : "no"} />
                <KV k="Live trade" v={bool(cts?.live_trading_enabled) || creds?.liveTradeEnabled ? "on" : "off"} />
                <KV k="Last status" v={String(creds?.lastTestStatus || stats?.mode || "—")} />
                <KV k="Position mode" v={String(cts?.position_mode ?? "hedge")} />
                <KV k="Margin" v={String(cts?.margin_mode ?? "cross")} />
                <KV k="Leverage % (CTS)" v={`${num(cts?.leveragePercentage, 100)}%`} />
                <KV k="Maximal leverage" v={bool(cts?.useMaximalLeverage) ? "on" : "off"} />
                <KV k="Pulse running" v={stats?.paused ? "paused" : stats?.running && !stats?.halted ? "yes" : stats?.haltReason || "halted"} />
                <KV k="Scan" v={`${fmtNum(stats?.scanMs, 1)} ms · cycle ${stats?.cycle ?? "—"}`} />
                <KV k="Volume factor" v={String(num(stats?.volumeFactor ?? overlay.volumeFactor, 1))} />
                <KV k="Unit" v={String(stats?.unit ?? (conn === "vst" ? "VST" : conn === "live" ? "USDT" : "MIXED"))} />
              </Grid>
              <div className="space-y-3 rounded-lg border border-border bg-bg2 p-3">
                <p className="font-mono text-xs text-muted uppercase">Credentials · submitted as live mainnet default</p>
                <div className="grid gap-3 sm:grid-cols-2">
                  <label className="block text-sm">
                    <span className="font-mono text-xs text-muted">Connection type</span>
                    <select
                      className="mt-1 w-full rounded-md border border-border bg-surface px-3 py-2 text-sm"
                      value={connType}
                      onChange={(e) => {
                        const v = e.target.value === "vst" ? "vst" : "mainnet";
                        setConnType(v);
                        if (v === "mainnet") setAsDefaultMainnet(true);
                      }}
                    >
                      <option value="mainnet">Live mainnet (BingX X01)</option>
                      <option value="vst">VST demo (BingX X02)</option>
                    </select>
                  </label>
                  <label className="block text-sm">
                    <span className="font-mono text-xs text-muted">Connection method</span>
                    <select
                      className="mt-1 w-full rounded-md border border-border bg-surface px-3 py-2 text-sm"
                      value={connMethod}
                      onChange={(e) => setConnMethod(e.target.value)}
                    >
                      <option value="library">library (HMAC)</option>
                      <option value="rest">REST signed</option>
                    </select>
                  </label>
                  <label className="block text-sm sm:col-span-2">
                    <span className="font-mono text-xs text-muted">API key</span>
                    <input
                      type="text"
                      autoComplete="off"
                      spellCheck={false}
                      className="mt-1 w-full rounded-md border border-border bg-surface px-3 py-2 font-mono text-sm"
                      value={apiKey}
                      placeholder={creds?.apiKeySet ? "leave to keep current" : "BingX API key"}
                      onChange={(e) => setApiKey(e.target.value)}
                    />
                  </label>
                  <label className="block text-sm sm:col-span-2">
                    <span className="font-mono text-xs text-muted">API secret</span>
                    <input
                      type="password"
                      autoComplete="new-password"
                      className="mt-1 w-full rounded-md border border-border bg-surface px-3 py-2 font-mono text-sm"
                      value={apiSecret}
                      placeholder={creds?.apiSecretSet ? "leave blank to keep current" : "BingX API secret"}
                      onChange={(e) => setApiSecret(e.target.value)}
                    />
                  </label>
                </div>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={asDefaultMainnet}
                    onChange={(e) => setAsDefaultMainnet(e.target.checked)}
                  />
                  Save submitted credentials as the default for live mainnet
                </label>
                <div className="flex flex-wrap items-center gap-3">
                  <button
                    type="button"
                    className="min-h-11 rounded-lg bg-primary px-4 text-sm text-bg"
                    disabled={credSaving}
                    onClick={() => void onSaveCreds()}
                  >
                    {credSaving ? "Saving…" : "Save credentials"}
                  </button>
                  {credMsg ? <span className="text-sm text-muted">{credMsg}</span> : null}
                </div>
                <p className="text-sm text-muted">
                  Secrets stay in Redis on the desk host and are never written to git. Submitted keys become the Live mainnet (X01) default unless you uncheck that box.
                </p>
              </div>
            </Card>
          )}

          {section === "profit" && (
            <div className="grid min-w-0 gap-4">
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
                  label="Base min PF"
                  value={overlay.baseMinPf}
                  min={PF_MIN}
                  max={PF_MAX}
                  step={PF_STEP}
                  hint={pfHint(overlay.baseMinPf, overlay.positionCostPct)}
                  onChange={(v) => patch("baseMinPf", normalizePf(v, DEFAULT_OVERLAY.baseMinPf))}
                />
                <Slider
                  label="Main min PF"
                  value={overlay.mainMinPf}
                  min={PF_MIN}
                  max={PF_MAX}
                  step={PF_STEP}
                  hint={pfHint(overlay.mainMinPf, overlay.positionCostPct)}
                  onChange={(v) => patch("mainMinPf", normalizePf(v, DEFAULT_OVERLAY.mainMinPf))}
                />
                <Slider
                  label="Real min PF"
                  value={overlay.realMinPf}
                  min={PF_MIN}
                  max={PF_MAX}
                  step={PF_STEP}
                  hint={pfHint(overlay.realMinPf, overlay.positionCostPct)}
                  onChange={(v) => {
                    const next = normalizePf(v, DEFAULT_OVERLAY.realMinPf);
                    patch("realMinPf", next);
                    patch("minPf", next);
                  }}
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
                <Toggle
                  label="Use live exchange costs"
                  on={overlay.useLivePositionCosts}
                  onChange={(v) => patch("useLivePositionCosts", v)}
                />
                <Toggle
                  label="Minimal range configuration"
                  on={overlay.preferMinimalRange}
                  hint="After validation gates, prefer the smallest stable ranges; PF is always ranked first and never minimized."
                  onChange={(v) => patch("preferMinimalRange", v)}
                />
                <Toggle
                  label="Additional 50+ coordination"
                  on={overlay.additionalCoordination}
                  hint="Evaluate the independent last-50+ position coordination window without reducing the full historic matrix."
                  onChange={(v) => patch("additionalCoordination", v)}
                />
                <Slider
                  label="Coordination last-N"
                  value={overlay.coordOptimizationN}
                  min={50}
                  max={200}
                  step={10}
                  hint="Independent live optimization window; historic catalog coverage is unchanged."
                  onChange={(v) => patch("coordOptimizationN", Math.max(50, Math.min(200, Math.round(v / 10) * 10)))}
                />
              </Grid>
              <div className="grid gap-3 sm:grid-cols-3">
                <KV k="1.00 Neutral" v={`net 0 · gross ${overlay.positionCostPct.toFixed(2)}%`} />
                <KV k="1.10 = +1× cost" v={`net +${overlay.positionCostPct.toFixed(2)}% · gross ${(overlay.positionCostPct * 2).toFixed(2)}%`} />
                <KV
                  k={`Last ${overlay.pfWindow} live`}
                  v={pfLive(stats)}
                />
              </div>
              <p className="text-sm text-muted">
                Ratio = 1 + avg( (move% − cost%) / cost% ) × 0.10. Gate uses this scale, not classic
                gross-profit / gross-loss. Classic PF still shows on Results.
              </p>
            </Card>
            <ForcedConfigsPanel live={stats?.forcedConfigs} />
            </div>
          )}

          {section === "risk" && (
            <Card title="Stop loss vs take profit" hint="All 30 SL:TP ratios × every TP step always run as independent Sets · no ratio picker">
              <p className="text-sm text-muted">
                Catalog is fixed system-wide: SL:TP 0.1–3.0 step 0.1 ({slTpGrid().length} books) × TP steps {overlay.setMinStep}–{overlay.setStepMax}.
                Every combo is historic-scored and live-gated on its own tape. Nothing in Settings turns a ratio off.
              </p>
              <Grid>
                <Toggle label="Auto-recalc live attach" on={overlay.slToTpAuto} onChange={(v) => patch("slToTpAuto", v)} />
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
                <Slider label="SL min" value={overlay.slMinPct} min={0.1} max={3} step={0.1} unit="%" onChange={(v) => patch("slMinPct", v)} />
                <Slider label="SL max" value={overlay.slMaxPct} min={0.1} max={3} step={0.1} unit="%" onChange={(v) => patch("slMaxPct", v)} />
                <Slider label="TP min" value={overlay.tpMinPct} min={0.1} max={3} step={0.1} unit="%" onChange={(v) => patch("tpMinPct", v)} />
                <Slider label="TP max" value={overlay.tpMaxPct} min={0.1} max={3} step={0.1} unit="%" onChange={(v) => patch("tpMaxPct", v)} />
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
                    {slTpGrid().map((r) => {
                      const tp = overlay.positionCostPct * overlay.tpCostRatio;
                      const sl = tp * r;
                      const wr = sl / (sl + tp);
                      return (
                        <tr key={r} className="border-t border-border font-mono">
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
                All {slTpGrid().length} ratios stay in eval. Auto-recalc only chooses the live attach for a new fill from the scored set — it does not disable the others.
              </p>
            </Card>
          )}

          {section === "packs" && (
            <Card title="Strategy types" hint="Each type runs independently · sliders ON=1 OFF=0">
              <Grid>
                <EnableSlider label="Indications" on={overlay.stratIndications} hint="State/Direction/Move/Active/Common/Signals/Trend/Break" onChange={(v) => patch("stratIndications", v)} />
                <EnableSlider label="General pulse" on={overlay.stratGeneral} hint="score() pack" onChange={(v) => patch("stratGeneral", v)} />
                <EnableSlider label="Block strategy" on={overlay.stratBlock && overlay.blockEnabled} hint="counts 1–6 · shared 2× maximum · 0 uses default 6" onChange={(v) => { patch("stratBlock", v); patch("blockEnabled", v); }} />
                <EnableSlider label="Trailing" on={overlay.stratTrailing} hint="independent trail Sets" onChange={(v) => patch("stratTrailing", v)} />
                <EnableSlider label="DCA" on={Boolean(overlay.dcaEnabled) && overlay.stratDca !== false} hint="independent steps" onChange={(v) => { patch("dcaEnabled", v); patch("stratDca", v); }} />
              </Grid>
              <p className="text-sm text-muted">
                Indications and general run in parallel for entries. Block adds on a live parent for every count (live max stack 6; historic evaluation counts 1–6).
                Trailing only moves SL after min-step, only in the protective direction, and retries a failed exchange update while retaining the old stop. Last-{overlay.pfWindow} PositionCost PF must pass before any new risk.
              </p>
            </Card>
          )}

          {section === "sets" && (
            <Card
              title="Independent Sets · 1m historic"
              hint="Settings min/max is the catalog. Every pack × SL in range × TP-step in range is its own book. Live only fires validated +EV Sets."
            >
              <Grid>
                <Toggle label="Historic 1m replay" on={overlay.histEnabled} onChange={(v) => patch("histEnabled", v)} />
                <Toggle label="Gate live on historic" on={overlay.setUseHistoricGate} onChange={(v) => patch("setUseHistoricGate", v)} />
                <Toggle label="Strict validated gate" on={overlay.setStrictGate !== false} onChange={(v) => patch("setStrictGate", v)} />
                <Toggle label="Auto-deactivate" on={overlay.setAutoDeact} onChange={(v) => patch("setAutoDeact", v)} />
                <Toggle label="Reactivate on recovery" on={overlay.setReactivate} onChange={(v) => patch("setReactivate", v)} />
                <Slider
                  label="Lookback bars"
                  value={overlay.histLookbackBars}
                  min={120}
                  max={4320}
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
                  min={1}
                  max={22}
                  step={1}
                  hint={`TP = step × position cost (${overlay.positionCostPct}%) → step ${overlay.setMinStep} = ${(overlay.setMinStep * overlay.positionCostPct).toFixed(2)}%. Every integer step through max is processed.`}
                  onChange={(v) => patch("setMinStep", Math.max(1, Math.min(22, Math.round(v))))}
                />
                <Slider
                  label="Step max"
                  value={overlay.setStepMax}
                  min={1}
                  max={22}
                  step={1}
                  hint="Upper TP step. Every integer from min through max is its own Set."
                  onChange={(v) => patch("setStepMax", Math.max(overlay.setMinStep || 1, Math.min(22, Math.round(v))))}
                />
                <Toggle
                  label="Adapt min step from live"
                  on={overlay.setStepAdapt !== false}
                  onChange={(v) => patch("setStepAdapt", v)}
                />
                <Toggle
                  label="Deactivate set on negative live PF"
                  on={overlay.setLiveNegativeDeact}
                  onChange={(v) => patch("setLiveNegativeDeact", v)}
                />
                <Slider
                  label="Set min PF"
                  value={overlay.setMinPf}
                  min={0.8}
                  max={2.5}
                  step={0.02}
                  hint="Base-stage cost PF. Shared range 0.80–2.50; qualified Sets flow to Main and Real."
                  onChange={(v) => patch("setMinPf", v)}
                />
                <Slider
                  label="Max DD time"
                  value={Math.round(overlay.setMaxDdTimeS / 60)}
                  min={10}
                  max={650}
                  step={10}
                  unit="min"
                  hint="10–650 min · default 450 · a Set must stay under this DDt cap"
                  onChange={(v) => patch("setMaxDdTimeS", Math.max(10, Math.min(650, Math.round(v / 10) * 10)) * 60)}
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
                <Num label="Base stage min PF" value={overlay.baseMinPf} min={0.8} max={2.5} step={0.02} hint="Shared PF range 0.80–2.50 · default 1.05" onChange={(v) => patch("baseMinPf", v)} />
                <Num label="Main stage min PF" value={overlay.mainMinPf} min={0.8} max={2.5} step={0.02} hint="Shared PF range 0.80–2.50 · default 1.10" onChange={(v) => patch("mainMinPf", v)} />
                <Num label="Real stage min PF" value={overlay.realMinPf} min={0.8} max={2.5} step={0.02} hint="Shared PF range 0.80–2.50 · default 1.15" onChange={(v) => patch("realMinPf", v)} />
              </Grid>
              <Grid>
                <KV k="Prev window" v={String(num(cts?.prevPosWindow ?? cts?.prev_pos_window, 25))} />
                <KV k="Prev min count" v={String(num(cts?.prevPosMinCount ?? cts?.prev_pos_min_count, 5))} />
                <KV k="Main eval pos count" v={String(num(cts?.mainEvalPosCount, 5))} />
                <KV k="Real eval pos count" v={String(num(cts?.realEvalPosCount, 3))} />
                <KV k="Min step" v={String(num(cts?.minStep ?? cts?.min_step, 3))} />
                <KV k="Trailing min step" v={String(num(cts?.trailingMinStep, 5))} />
              </Grid>
            </Card>
          )}

          {section === "block" && (
            <Card title="Block strategy" hint="Counts 1–6 are independent · each count retains recovery state until its own positive result · no compounding">
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
                  max={6}
                  step={1}
                  hint="0 = default 6 · six independent evaluations · shared total volume cap"
                  onChange={(v) => patch("blockMaxStack", v)}
                />
                <Num
                  label="Volume ratio"
                  value={overlay.blockVolumeRatio}
                  min={0.05}
                  max={2}
                  step={0.05}
                  onChange={(v) => patch("blockVolumeRatio", v)}
                />
                <Num label="Maximum total volume" value={overlay.blockMaxVolumeMultiplier}
                  min={1} max={2} step={0.05} hint="Base 1 = original parent · maximum 2×"
                  onChange={(v) => patch("blockMaxVolumeMultiplier", v)} />
                {[1, 2, 3, 4, 5, 6].map((count) => (
                  <Toggle key={count} label={`Count ${count}`} on={overlay.blockCounts.includes(count)}
                    onChange={(enabled) => patch("blockCounts", enabled
                      ? [...new Set([...overlay.blockCounts, count])].sort((a, b) => a - b)
                      : overlay.blockCounts.filter((n) => n !== count))} />
                ))}
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
                targetAdd = parentBase × min(count × ratio, totalCap − 1) · requested = max(0, targetAdd − confirmedAdd) ·
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
            <Card title="Trailing · independent recals" hint="All arm × give combos always run as independent Sets · no trail picker">
              <p className="text-sm text-muted">
                Catalog is fixed system-wide: arm 0.3–1.5 step 0.3 × give 0.1–0.5 step 0.1 ({trailGrid().length} trail Sets)
                plus the matching Normal (no-trail) book. Auto-recalc only chooses live attach. Nothing in Settings turns a trail off.
              </p>
              <Grid>
                <Toggle
                  label="Trailing pack"
                  on={overlay.stratTrailing}
                  onChange={(v) => patch("stratTrailing", v)}
                />
                <Toggle label="Auto-recalc live attach" on={overlay.trailAuto} onChange={(v) => patch("trailAuto", v)} />
                <Toggle
                  label="Recalc give from arm × factor"
                  on={overlay.trailRecalcGive}
                  onChange={(v) => {
                    patch("trailRecalcGive", v);
                    if (v) patch("trailGivePct", trailGiveFromArm(overlay.trailArmPct, overlay.trailGiveFactor, 0.1, 0.5));
                  }}
                />
                <Slider
                  label="Give factor"
                  value={overlay.trailGiveFactor}
                  min={0.2}
                  max={0.6}
                  step={0.01}
                  hint={`auto give = arm × factor`}
                  onChange={(v) => {
                    patch("trailGiveFactor", v);
                    if (overlay.trailRecalcGive) {
                      patch("trailGivePct", trailGiveFromArm(overlay.trailArmPct, v, 0.1, 0.5));
                    }
                  }}
                />
                <Slider label="Recalc min samples" value={overlay.trailRecalcN} min={3} max={20} step={1} onChange={(v) => patch("trailRecalcN", v)} />
                <Slider label="Recalc every N closes" value={overlay.trailRecalcEvery} min={3} max={30} step={1} onChange={(v) => patch("trailRecalcEvery", v)} />
              </Grid>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="font-mono text-xs text-muted">
                    <tr>
                      <th className="pb-2 font-medium">Trail</th>
                      <th className="pb-2 font-medium">Arm %</th>
                      <th className="pb-2 font-medium">Give %</th>
                    </tr>
                  </thead>
                  <tbody>
                    {trailGrid().map((t) => (
                      <tr key={t.key} className="border-t border-border font-mono">
                        <td className="py-1.5">{t.key}</td>
                        <td className="py-1.5">{t.arm.toFixed(1)}</td>
                        <td className="py-1.5">{t.give.toFixed(1)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="text-sm text-muted">
                All {trailGrid().length} trail ratios stay in eval, independent of the SL:TP book. Auto-recalc only picks the live attach for a new fill.
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
                on={Boolean(overlay.dcaEnabled)}
                onChange={(v) => {
                  patch("dcaEnabled", v);
                  patch("stratDca", v);
                }}
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
                  max={12}
                  step={1}
                  hint="0 = configured distance list (default 4) · hard max 12"
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
                    min={0.25}
                    max={2.5}
                    step={0.1}
                    hint="Backend clamp 0.25–2.5×"
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
                <Num label="Base min PF" value={overlay.baseMinPf} min={PF_MIN} max={PF_MAX} step={PF_STEP} onChange={(v) => patch("baseMinPf", normalizePf(v, DEFAULT_OVERLAY.baseMinPf))} />
                <Num label="Main min PF" value={overlay.mainMinPf} min={PF_MIN} max={PF_MAX} step={PF_STEP} onChange={(v) => patch("mainMinPf", normalizePf(v, DEFAULT_OVERLAY.mainMinPf))} />
                <Num
                  label="Real min PF"
                  value={overlay.realMinPf}
                  min={PF_MIN}
                  max={PF_MAX}
                  step={PF_STEP}
                  onChange={(v) => {
                    const next = normalizePf(v, DEFAULT_OVERLAY.realMinPf);
                    patch("realMinPf", next);
                    patch("minPf", next);
                  }}
                />
                <Num label="Noise" value={overlay.noise} min={0.01} max={0.2} step={0.01} onChange={(v) => patch("noise", v)} />
                <Num label="Vol weight" value={overlay.volWeight} min={0.05} max={1} step={0.05} onChange={(v) => patch("volWeight", v)} />
                <Num label="Min step" value={Math.max(1, overlay.minStep)} min={1} max={22} step={1} hint="Search floor only; effective minimum requires live evidence" onChange={(v) => patch("minStep", Math.max(1, Math.min(22, Math.round(v))))} />
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
                Last/Prev PF gates entries by position-count windows. Pause halts on a loss streak.
                Cont caps live slots and, on additional strategies (Block / DCA), halves the live
                count stack when last-PF is below min. Count-pos vol ratio trims new-entry size as
                more positions are open. Each Block count keeps its own last-N tape for the add gate.
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
            <Card title="Control orders" hint="Hedge-safe TP/SL protection · quantity-matched by symbol, direction and range">
              <div className="flex flex-col gap-2">
                <Toggle
                  label="Place SL/TP on exchange"
                  on={overlay.controlOrders}
                  onChange={(v) => patch("controlOrders", v)}
                />
                <Toggle
                  label="Separate controls by symbol + direction + range"
                  on={overlay.controlOrdersPerConfig}
                  onChange={(v) => patch("controlOrdersPerConfig", v)}
                />
              </div>
              <Grid>
                <Num label="Stop %" value={overlay.slPct} min={0.1} max={5} step={0.02} onChange={(v) => patch("slPct", v)} />
                <Num label="Take profit %" value={overlay.tpPct} min={0.1} max={8} step={0.05} onChange={(v) => patch("tpPct", v)} />
                <KV k="CTS SL cost ratios" v={arrJoin(cts?.activeStopLossPositionCostRatios, "2, 3, 5")} />
                <KV k="CTS TP multipliers" v={arrJoin(cts?.activeTakeProfitMultipliers, "1.25, 1.5, 1")} />
                <KV k="CTS control_orders" v={bool(cts?.control_orders, true) ? "1" : "0"} />
                <KV k="Applied control mode" v={overlay.controlOrdersPerConfig ? "PER-CONFIG RANGE" : "AGGREGATE"} />
                <KV k="Working type" v="MARK_PRICE" />
              </Grid>
              <ControlsLive stats={stats} />
              <p className="text-sm text-muted">
                {overlay.controlOrdersPerConfig
                  ? "Each symbol + direction + normalized SL/TP range receives its own quantity-matched TP/SL pair. Identical ranges merge by quantity and weighted entry, while Set lineage stays attached."
                  : "Controls use the legacy aggregate symbol + direction pair and widest range. Turn on per-config mode to isolate independent ranges without closePosition fallback."}
              </p>
            </Card>
          )}

          {section === "indication" && (
            <Card title="Indications" hint="CTS types: State is the consensus Indication · Direction / Move / Active / Common / Signals / Trend / Break run independently">
              <Grid>
                <EnableSlider label="Indications on" on={overlay.indEnabled} onChange={(v) => patch("indEnabled", v)} />
                <EnableSlider label="State" on={overlay.indTypeState !== false} hint="tf_combined + low-stop consensus — the Indication" onChange={(v) => patch("indTypeState", v)} />
                <EnableSlider label="Direction" on={overlay.indTypeDirection !== false} hint="post-reversal two-window, independent Long/Short" onChange={(v) => patch("indTypeDirection", v)} />
                <EnableSlider label="Move" on={overlay.indTypeMove !== false} hint="same-dir displacement, independent Long/Short" onChange={(v) => patch("indTypeMove", v)} />
                <EnableSlider label="Active" on={overlay.indTypeActive !== false} hint="outbreak 3/5/10 vs previous window" onChange={(v) => patch("indTypeActive", v)} />
                <EnableSlider label="Common" on={overlay.indTypeCommon !== false} hint="RSI + MACD + EMA + Bollinger" onChange={(v) => patch("indTypeCommon", v)} />
                <EnableSlider label="Signals" on={overlay.indTypeSignals !== false} hint="per-TF evaluateSignalCandles" onChange={(v) => patch("indTypeSignals", v)} />
                <EnableSlider label="Trend" on={overlay.indTypeTrend !== false} hint="trend slope / direction vote" onChange={(v) => patch("indTypeTrend", v)} />
                <EnableSlider label="Break" on={overlay.indTypeBreak !== false} hint="breakout / range vote" onChange={(v) => patch("indTypeBreak", v)} />
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
                State is the actual Indication (combined TF + multi-source consensus). Direction, Move, Active, Common, Signals, Trend and Break
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
                <Num label="Max DD time min" value={Math.round(overlay.maxDdTimeS / 60)} min={10} max={650} step={10} hint="10–650 min · default 450 · force-close a position stuck underwater this long" onChange={(v) => patch("maxDdTimeS", Math.max(10, Math.min(650, Math.round(v / 10) * 10)) * 60)} />
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
              onClick={() => setResetAsk(true)}
              data-testid="reset-overlay"
              className="min-h-11 rounded-lg border border-border px-4 text-sm text-muted"
            >
              Reset from CTS
            </button>
            <button
              type="button"
              data-testid="save-system-preset"
              disabled={userBusy || !ready}
              onClick={() => {
                setSection("presets");
                void onSaveSystemPreset();
              }}
              className="min-h-11 rounded-lg border border-primary px-4 text-sm text-fg disabled:opacity-40"
            >
              Save as Preset
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
      {resetAsk ? (
        <div
          className="fixed inset-0 z-50 flex items-end justify-center bg-bg/80 p-4 sm:items-center"
          role="presentation"
          onClick={() => setResetAsk(false)}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="reset-overlay-title"
            data-testid="reset-confirm"
            className="w-full max-w-md space-y-4 rounded-radius border border-border bg-surface p-4 shadow-lg"
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => {
              if (e.key === "Escape") setResetAsk(false);
            }}
          >
            <div>
              <h2 id="reset-overlay-title" className="text-sm font-medium tracking-wide text-muted uppercase">
                Reset overlay?
              </h2>
              <p className="mt-2 text-sm text-fg">
                This replaces the current sliders with CTS defaults
                {conn === "vst" ? " for VST" : conn === "live" ? " for Live" : ""}.
                Unsaved edits are discarded. Nothing is written until you save.
              </p>
            </div>
            <div className="flex flex-wrap justify-end gap-2">
              <button
                type="button"
                data-testid="reset-cancel"
                onClick={() => setResetAsk(false)}
                className="min-h-11 rounded-lg border border-border px-4 text-sm text-muted"
              >
                Cancel
              </button>
              <button
                type="button"
                data-testid="reset-confirm-yes"
                autoFocus
                onClick={onResetOverlay}
                className="min-h-11 rounded-lg bg-danger px-4 text-sm font-medium text-bg"
              >
                Reset overlay
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {cleanupAsk ? (
        <div className="fixed inset-0 z-50 flex items-end justify-center bg-bg/70 p-4 sm:items-center" role="dialog" aria-modal="true">
          <div className="w-full max-w-md rounded-radius border border-border bg-surface p-4">
            <h3 className="text-sm font-medium">Delete all system presets except Default?</h3>
            <p className="mt-2 text-sm text-muted">
              Every saved preset except the protected Default will be removed. The currently applied overlay is not changed.
            </p>
            <div className="mt-4 flex flex-wrap justify-end gap-2">
              <button type="button" className="min-h-11 rounded-lg border border-border px-4 text-sm" onClick={() => setCleanupAsk(false)}>
                Cancel
              </button>
              <button
                type="button"
                data-testid="user-preset-delete-except-default-yes"
                className="min-h-11 rounded-lg bg-danger px-4 text-sm font-medium text-bg"
                onClick={() => void onDeleteExceptDefault()}
              >
                Delete others
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {deleteId ? (
        <div className="fixed inset-0 z-50 flex items-end justify-center bg-bg/70 p-4 sm:items-center" role="dialog" aria-modal="true">
          <div className="w-full max-w-md rounded-radius border border-border bg-surface p-4">
            <h3 className="text-sm font-medium">Delete system preset?</h3>
            <p className="mt-2 text-sm text-muted">
              {userPresets.find((p) => p.id === deleteId)?.name || "This preset"} will be removed for Live and VST. Overlay on the engine is not changed until you load another.
            </p>
            <div className="mt-4 flex flex-wrap justify-end gap-2">
              <button type="button" className="min-h-11 rounded-lg border border-border px-4 text-sm" onClick={() => setDeleteId(null)}>
                Cancel
              </button>
              <button
                type="button"
                data-testid="user-preset-delete-yes"
                className="min-h-11 rounded-lg bg-danger px-4 text-sm font-medium text-bg"
                onClick={() => void onDeleteSystemPreset()}
              >
                Delete preset
              </button>
            </div>
          </div>
        </div>
      ) : null}
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

function pfLive(stats: LiveStats | null) {
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
  hint,
}: {
  label: string;
  on: boolean;
  onChange: (v: boolean) => void;
  locked?: boolean;
  hint?: string;
}) {
  return (
    <button
      type="button"
      disabled={locked}
      onClick={() => onChange(!on)}
      className="flex min-h-11 items-center justify-between rounded-lg border border-border bg-bg2 px-3 text-left text-sm disabled:opacity-70"
    >
      <span className="pr-3">
        <span>
          {label}
          {locked ? <span className="ml-2 font-mono text-xs text-faint">CTS</span> : null}
        </span>
        {hint ? <span className="mt-0.5 block text-xs text-muted">{hint}</span> : null}
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

function SetsLiveTable({ stats, overlay }: { stats: LiveStats | null; overlay: PulseOverlay }) {
  const sets = stats?.sets;
  const p = sets?.progress;
  const rows = sets?.rows ?? [];
  const liveOv = sets?.liveOverview;
  const pct = Math.max(0, Math.min(100, p?.pct ?? 0));
  const phase = String(p?.phase ?? "idle");
  const updating = ["fetch", "replay", "score", "partial"].includes(phase);
  const gate = p?.ready ? (phase === "ready" ? "" : " · gate ready") : " · gate closed";
  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-border bg-bg2 px-3 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2 font-mono text-xs">
          <span className={p?.ready ? "text-primary" : "text-warn"}>{p?.phase ?? "idle"}{updating ? " · updating" : ""}{gate}</span>
          <span className="text-muted">
            valid {sets?.validatedCount ?? 0}/{sets?.setCount ?? 0} · {sets?.activeCount ?? 0}/{sets?.setCount ?? 0} active · {sets?.histFills ?? 0} hist · last {fmtNum(p?.lastRunMs, 0)}ms
          </span>
        </div>
        <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-border">
          <div className="h-full rounded-full bg-primary" style={{ width: `${pct}%` }} />
        </div>
        <p className="mt-2 font-mono text-[11px] text-muted">
          {p?.detail || "waiting 1m bars"} {p?.symbol ? `· ${p.symbol.replace("-USDT", "")}` : ""} {p?.error ? `· ${p.error}` : ""}
          {p?.symbolsTotal ? ` · history ${p.symbolsDone ?? 0}/${p.symbolsTotal}` : ""}
          {updating && p?.ready ? " · prior gate remains active" : ""}
        </p>
        <p className="mt-1 font-mono text-[11px] text-primary">
          live on-exchange {liveOv?.active ?? sets?.liveActive ?? 0}/{liveOv?.processed ?? sets?.liveProcessed ?? 0} processed · fills {liveOv?.fills ?? sets?.liveFills ?? 0} · PF {Number(liveOv?.last15Ratio ?? 0).toFixed(2)} net {(Number(liveOv?.netAvg ?? 0) * 100).toFixed(3)}% · cost subtracted · deact from live only
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
              rows.slice(0, 40).map((r) => {
                const liveN = r.liveN || r.live?.n || 0;
                const pf = liveN ? Number(r.live?.last15Ratio ?? r.last15Ratio) : r.last15Ratio;
                const ddt = liveN ? Number(r.live?.maxDdS ?? r.maxDdS) : r.maxDdS;
                return (
                <tr key={r.id} className="border-t border-border font-mono text-xs">
                  <td className="py-1.5">
                    {r.pack} · sl{r.slRatio.toFixed(1)} · st{r.step ?? "—"} · {r.trailKey}
                    {liveN ? " · live" : " · hist"}
                  </td>
                  <td className={r.active ? "py-1.5 text-primary" : "py-1.5 text-danger"}>{r.active ? "on" : "off"}</td>
                  <td className="py-1.5 text-right">
                    {r.n}
                    {liveN ? `+${liveN}` : ""}
                  </td>
                  <td className={`py-1.5 text-right ${pf + 1e-9 >= overlay.setMinPf ? "text-primary" : "text-danger"}`}>
                    {pf.toFixed(2)}
                  </td>
                  <td className={`py-1.5 text-right ${r.last25AvgR < 0 ? "text-danger" : "text-primary"}`}>
                    {r.last25AvgR.toFixed(2)}
                  </td>
                  <td className="py-1.5 text-right">{formatDuration(ddt * 1000)}</td>
                  <td className="py-1.5 text-muted">{r.deactReason || "—"}</td>
                </tr>
                );
              })
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
  const groups = c?.groups ?? open.map((p) => ({
    key: p.controlGroupKey ?? `${p.symbol}:${p.side}`,
    symbol: p.symbol,
    side: p.side,
    range: p.controlRangeKey ?? "aggregate",
    qty: p.qty,
    memberCount: p.memberCount ?? 1,
    protected: Boolean(p.controls),
    status: p.controlStatus,
    slOid: p.slOid,
    tpOid: p.tpOid,
    secSlOid: p.secSlOid,
    secTpOid: p.secTpOid,
  }));
  const missing = groups.filter((group) => !group.protected);
  const ok = c?.ok ?? open.filter((p) => p.controls).length;
  const sec = c?.security ?? open.filter((p) => p.secSlOid && p.secTpOid).length;
  const mode = c?.mode ?? (stats?.pulse?.controlOrdersPerConfig === false ? "aggregate" : "per-config");
  return (
    <div className="rounded-lg border border-border bg-bg2 px-3 py-3 font-mono text-xs" data-testid="controls-live">
      <div className="flex flex-wrap justify-between gap-2">
        <span className={missing.length ? "text-danger" : "text-primary"}>
          live controls · {mode} · {ok}/{c?.open ?? open.length} SL+TP · {sec} security
        </span>
        <span className="text-muted">missing {c?.missing ?? missing.length}</span>
      </div>
      <p className="mt-1 text-muted">
        {c?.groupCount ?? groups.length} logical range groups · {c?.mergedMembers ?? groups.reduce((sum, group) => sum + (group.memberCount ?? 1), 0)} merged members
      </p>
      {groups.length ? (
        <div className="mt-2 grid gap-1 sm:grid-cols-2">
          {groups.slice(0, 12).map((group) => {
            const slOid = group.slOid || group.secSlOid || "";
            const tpOid = group.tpOid || group.secTpOid || "";
            const protectedGroup = Boolean(group.protected ?? (slOid && tpOid));
            return (
              <div key={group.key} className="flex min-w-0 items-center justify-between gap-2 rounded-md border border-border px-2 py-1">
                <span className="min-w-0 truncate text-muted">
                  {group.symbol?.replace("-USDT", "")} {group.side === "LONG" ? "L" : "S"} · {group.range ?? "aggregate"} · q {group.qty ?? "—"}
                </span>
                <span className={protectedGroup ? "shrink-0 text-primary" : "shrink-0 text-danger"} title={`${slOid || "missing SL"} / ${tpOid || "missing TP"}`}>
                  {protectedGroup ? `${String(slOid).slice(-8)}/${String(tpOid).slice(-8)}` : group.status ?? "missing"}
                </span>
              </div>
            );
          })}
        </div>
      ) : null}
      {missing.length ? (
        <div className="mt-2 flex flex-wrap gap-2 text-danger">
          {missing.slice(0, 12).map((group) => (
            <span key={group.key}>
              {group.symbol?.replace("-USDT", "")} {group.side === "LONG" ? "L" : "S"} · {group.range ?? "aggregate"}
            </span>
          ))}
        </div>
      ) : (
        <p className="mt-2 text-muted">Every logical group has quantity-matched SL + TP protection</p>
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
  const controls = stats?.coverage?.controls;
  const controlMode = controls?.mode ?? (overlay.controlOrdersPerConfig ? "per-config" : "aggregate");
  const controlGroups = controls?.groupCount ?? stats?.openCount ?? 0;
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
        <span>controls {controlMode} · {controlGroups} groups</span>
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

import { useEffect, useState } from "react";
import { type PulseOverlay } from "@/lib/config-model";
import { SYSTEM_LIMITS, type SystemKey } from "@/lib/system-settings";
import { SystemHealth } from "./system-health";
import { useSystemStatus } from "@/lib/use-system-status";

type Field = [SystemKey, string, string?];
const groups: { title: string; hint: string; fields: Field[] }[] = [
  { title: "Processing and memory", hint: "Worker and cycle budgets distribute work fairly. Every configured Set remains in the calculation catalog.", fields: [
    ["systemWorkers", "Calculation workers"], ["systemEntryBatch", "Candidates per cycle"], ["systemEntryBudgetMs", "Entry cycle budget · ms"],
    ["rssSoftMb", "Memory soft ceiling · MiB", "0 = automatic host / service budget"], ["rssHardMb", "Memory hard ceiling · MiB", "0 = automatic; increases load shedding, preserves controls"],
  ] },
  { title: "Requests and sampling", hint: "Separate request buckets retain the adapter ceilings. Exchange cooldowns still apply.", fields: [
    ["systemPublicRps", "Public requests / second"], ["systemPrivateRps", "Private requests / second"], ["systemOrderRps", "Order requests / second"],
    ["systemStatsIntervalS", "Overview refresh · seconds"], ["systemMetricsIntervalS", "Resource sampling · seconds"],
    ["systemReportIntervalS", "Report export interval · seconds"],
  ] },
  { title: "History and persistence", hint: "Per-connection files survive reinstalling code. Bar retention is raised automatically when the configured replay window needs more bars.", fields: [
    ["systemHistoryRetentionBars", "Retained 1m bars / symbol"], ["systemHistoryPersistS", "Bar checkpoint interval · seconds"],
    ["systemDbMaxMb", "Statistics DB ceiling · MiB", "Main database; WAL adds a bounded checkpoint buffer"],
    ["systemRetentionDays", "Detail retention · days", "Lifetime financial totals are kept until an explicit statistics reset"],
    ["systemTradeMaxRows", "Recent trade rows"], ["systemEventMaxRows", "Recent event rows"], ["systemSampleMaxRows", "Resource sample rows"],
  ] },
  { title: "Logs and backups", hint: "All managed engine logs have both line and byte limits. Statistics backups are verified and rotated independently for each connection.", fields: [
    ["systemLogMaxLines", "Log lines / file"], ["systemLogMaxMb", "Log ceiling / file · MiB"],
    ["systemBackupIntervalHours", "Backup interval · hours"], ["systemBackupKeep", "Backups retained"],
  ] },
  { title: "Redis coordination", hint: "At most four configuration hashes are cached per process, with at most 512 fields per hash. Order histories and lifetime statistics use their individual persistent files. Shared Redis retention remains separate.", fields: [
    ["systemRedisCacheS", "Configuration cache · seconds"], ["systemRedisMaxHashKb", "Configuration hash ceiling · KiB"],
    ["systemRedisMaxFieldKb", "Individual field ceiling · KiB", "Oversized writes are rejected before changing the saved hash"],
    ["systemSetMaxEntries", "Maximum cached calculations / Set", "At 100% retain the latest entries by source timestamp"],
    ["systemTrimTargetPct", "Retention target · %", "350 entries → newest 280 at the default 80% target"],
    ["systemRedisCalcMaxMb", "Calculation cache budget · MiB"], ["systemRedisCalcMaxSets", "Cached Sets ceiling", "Cache eviction never removes a Set from the calculation catalog"],
    ["systemRedisCalcSetMaxKb", "Per-Set cache budget · KiB", "Both the entry count and byte limit retain the newest results"],
    ["systemRedisMinHitPct", "Minimum useful cache reuse · %", "0 = always use Redis; below this rate full calculation continues locally to limit cache writes"],
    ["systemRedisProbeEntries", "Calculations / cache probe"], ["systemRedisRecheckS", "Cache recheck · seconds"],
    ["systemRedisCalcTtlS", "Calculation cache expiry · seconds"],
  ] },
];

type Props = {
  conn: string;
  overlay: PulseOverlay;
  patch: <K extends keyof PulseOverlay>(key: K, value: PulseOverlay[K]) => void;
  navigate: (section: "packs" | "risk" | "sets" | "stages" | "block" | "dca" | "axes" | "volume" | "controls" | "symbols" | "exits") => void;
};

export function SystemSettingsPanel({ conn, overlay, patch, navigate }: Props) {
  const status = useSystemStatus(conn);
  const [scope, setScope] = useState<"telemetry" | "statistics">("telemetry");
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => { setConfirmation(""); setMessage(""); }, [conn, scope]);
  const lane = conn === "vst" ? "bingx-x02" : conn === "live" ? "bingx-x01" : conn;
  const required = `RESET ${scope.toUpperCase()} ${lane}`;
  const available = conn !== "overall" && status?.persistent === true;
  const maintain = async (action: "backup" | "compact" | "reset") => {
    if (busy || !available || (action === "reset" && confirmation !== required)) return;
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(`/system.json?conn=${encodeURIComponent(conn)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, scope, confirmation }), signal: AbortSignal.timeout(15000),
      });
      const result = await response.json() as { ok?: boolean; detail?: string };
      setMessage(result.detail || (response.ok ? "Maintenance complete" : "Maintenance unavailable"));
      if (result.ok && action === "reset") setConfirmation("");
    } catch { setMessage("Maintenance response unavailable. Check the next database status before retrying a reset."); }
    finally { setBusy(false); }
  };
  return <div className="min-w-0 space-y-4" data-testid="system-settings">
    <SystemHealth status={status} />
    <section className="rounded-radius border border-border bg-surface p-4">
      <h2 className="text-sm font-medium">General calculation basis · always active</h2>
      <p className="mt-2 text-sm text-muted">Normal (General) controls effective simulated and live positions and defaults to disabled. Internal General calculations, evaluation counts and coordination continue for Block, DCA and other configured strategies.</p>
      <button type="button" className="mt-3 min-h-11 rounded-lg border border-border px-3 text-sm" onClick={() => navigate("packs")}>Normal (General) and independent strategy switches</button>
    </section>
    {groups.map((group) => <section key={group.title} className="min-w-0 rounded-radius border border-border bg-surface p-4">
      <h2 className="text-sm font-medium">{group.title}</h2><p className="mt-1 text-sm text-muted">{group.hint}</p>
      <div className="mt-4 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {group.fields.map(([key, label, hint]) => {
          const [, min, max, integer] = SYSTEM_LIMITS[key] as [number, number, number, boolean];
          return <label key={key} className="min-w-0 space-y-1 text-sm"><span>{label}</span>
            <input aria-label={label} data-setting={key} type="number" value={overlay[key]} min={min} max={max} step={integer ? 1 : 0.1}
              onChange={(event) => { const n = event.currentTarget.valueAsNumber; if (Number.isFinite(n)) patch(key, Math.min(max, Math.max(min, integer ? Math.trunc(n) : n))); }}
              className="min-h-11 w-full rounded-lg border border-border bg-bg2 px-3 font-mono" />
            <span className="block text-xs text-muted">{hint || `Range ${min}–${max}`}</span>
          </label>;
        })}
      </div>
    </section>)}
    <section className="rounded-radius border border-border bg-surface p-4">
      <h2 className="text-sm font-medium">Execution and strategy limits</h2>
      <p className="mt-1 text-sm text-muted">These limits use the same saved settings as their detailed sections.</p>
      <dl className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
        {[["Open cap", overlay.maxOpen || "Unlimited"], ["Symbols", overlay.symbolCap || "Unlimited"], ["Steps", `${overlay.setMinStep}–${overlay.setStepMax}`], ["Set PF", overlay.setMinPf], ["DDT · minutes", overlay.setMaxDdTimeS / 60], ["TP %", `${overlay.tpMinPct}–${overlay.tpMaxPct || "Unlimited"}`], ["SL %", `${overlay.slMinPct}–${overlay.slMaxPct}`], ["Block level", `${overlay.blockActiveMinLevel}–${overlay.blockMaxStack || 6}`]].map(([label, value]) => <div key={label}><dt className="text-xs text-muted">{label}</dt><dd className="mt-1 font-mono text-sm">{value}</dd></div>)}
      </dl>
      <div className="mt-4 flex flex-wrap gap-2">{(["risk", "sets", "stages", "block", "dca", "axes", "volume", "controls", "symbols", "exits"] as const).map((name) => <button key={name} type="button" className="min-h-11 rounded-lg border border-border px-3 text-sm capitalize" onClick={() => navigate(name)}>{name === "dca" ? "DCA" : name}</button>)}</div>
    </section>
    <section className="min-w-0 rounded-radius border border-border bg-surface p-4">
      <h2 className="text-sm font-medium">Database maintenance</h2>
      <p className="mt-1 text-sm text-muted">Telemetry reset clears resource samples, event details and runtime counters. Statistics reset also clears recorded financial totals and trade details. Each reset first saves a verified backup. Open orders, pending fills, credentials, settings and evaluation histories stay available.</p>
      {conn === "overall" && <p className="mt-3 text-sm text-muted">Select Live or VST to maintain its individual database.</p>}
      {status?.dbFile && <p className="mt-3 break-all font-mono text-xs text-muted">{status.dbFile}</p>}
      <div className="mt-3 flex flex-wrap gap-2">
        <button type="button" disabled={!available || busy} onClick={() => void maintain("backup")} className="min-h-11 rounded-lg border border-border px-3 text-sm disabled:opacity-40">Back up now</button>
        <button type="button" disabled={!available || busy} onClick={() => void maintain("compact")} className="min-h-11 rounded-lg border border-border px-3 text-sm disabled:opacity-40">Prune and compact</button>
      </div>
      <div className="mt-4 grid items-end gap-3 sm:grid-cols-2">
        <label className="space-y-1 text-sm"><span>Reset scope</span><select aria-label="Reset scope" disabled={busy} value={scope} onChange={(e) => setScope(e.target.value as typeof scope)} className="min-h-11 w-full rounded-lg border border-border bg-bg2 px-3"><option value="telemetry">Telemetry only</option><option value="statistics">Statistics database</option></select></label>
        <label className="min-w-0 space-y-1 text-sm"><span className="break-all">Type {required}</span><input aria-label="Confirm database reset" disabled={!available || busy} value={confirmation} onChange={(e) => setConfirmation(e.target.value)} autoComplete="off" className="min-h-11 w-full rounded-lg border border-border bg-bg2 px-3 font-mono text-xs" /></label>
      </div>
      <button type="button" disabled={!available || busy || confirmation !== required} onClick={() => void maintain("reset")} className="mt-3 min-h-11 rounded-lg border border-negative/40 px-3 text-sm text-negative disabled:opacity-40">{busy ? "Working…" : "Back up and reset"}</button>
      {message && <p role="status" className="mt-3 text-sm text-muted">{message}</p>}
    </section>
    <p className="text-xs text-muted">Save settings to apply changed limits. Database maintenance acts immediately on the selected connection.</p>
  </div>;
}

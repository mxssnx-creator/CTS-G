import {
  DEFAULT_SIGNAL_CONFIG,
  SIGNAL_RANGES,
  SIGNAL_SOURCES,
  SIGNAL_TIMEFRAMES,
  type SignalConfig,
} from "@/lib/config-model";

const TYPES = ["base", "exit", "trail"] as const;

function toggle<T>(list: readonly T[], v: T): T[] {
  return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
}

function Chips<T extends string | number>({ all, on, onChange, fmt }: { all: readonly T[]; on: readonly T[]; onChange: (v: T[]) => void; fmt?: (v: T) => string }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {all.map((v) => {
        const active = on.includes(v);
        return (
          <button
            key={String(v)}
            type="button"
            onClick={() => onChange(toggle(on, v))}
            className={`rounded-full border px-2.5 py-0.5 text-xs transition ${active ? "border-primary text-primary" : "border-border text-muted hover:text-fg"}`}
          >
            {fmt ? fmt(v) : String(v)}
          </button>
        );
      })}
    </div>
  );
}

/** Editor of `signalConfigs`: every source x range x timeframe of an entry becomes its own Signals pack (own Sets, stages, lanes), with this entry's TP-step
 * range, SL ratios and Set types. Packs = sources x ranges x timeframes (engine bound 72). */
export function SignalConfigEditor({ value, onChange }: { value: SignalConfig[]; onChange: (v: SignalConfig[]) => void }) {
  const set = (i: number, patch: Partial<SignalConfig>) => onChange(value.map((e, k) => (k === i ? { ...e, ...patch } : e)));
  const packs = value.filter((e) => e.enabled).reduce((n, e) => n + e.sources.length * e.ranges.length * e.timeframes.length, 0);
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between text-xs text-muted">
        <span>{value.length} entries · {packs} Signals packs (each its own Sets, stage chain and lanes; floor minPfSignals)</span>
        <button
          type="button"
          className="rounded-md border border-border px-2 py-1 text-xs hover:text-fg"
          onClick={() => onChange([...value, { ...DEFAULT_SIGNAL_CONFIG, label: `signals ${value.length + 1}` }])}
        >
          Add entry
        </button>
      </div>
      {value.map((e, i) => (
        <div key={i} className="space-y-2 rounded-lg border border-border bg-surface p-3">
          <div className="flex flex-wrap items-center gap-2">
            <input
              className="min-w-0 flex-1 rounded-md border border-border bg-transparent px-2 py-1 text-sm"
              value={e.label}
              onChange={(ev) => set(i, { label: ev.target.value })}
              aria-label="Entry label"
            />
            <label className="flex items-center gap-1 text-xs">
              <input type="checkbox" checked={e.enabled} onChange={(ev) => set(i, { enabled: ev.target.checked })} /> on
            </label>
            <button type="button" className="text-xs text-danger" onClick={() => onChange(value.filter((_, k) => k !== i))}>
              Remove
            </button>
          </div>
          <div className="text-xs text-muted">Sources</div>
          <Chips all={SIGNAL_SOURCES} on={e.sources as (typeof SIGNAL_SOURCES)[number][]} onChange={(v) => set(i, { sources: v })} />
          <div className="grid gap-2 sm:grid-cols-3">
            <div>
              <div className="text-xs text-muted">Ranges</div>
              <Chips all={SIGNAL_RANGES} on={e.ranges as (typeof SIGNAL_RANGES)[number][]} onChange={(v) => set(i, { ranges: v })} />
            </div>
            <div>
              <div className="text-xs text-muted">Timeframes</div>
              <Chips all={SIGNAL_TIMEFRAMES} on={e.timeframes as (typeof SIGNAL_TIMEFRAMES)[number][]} onChange={(v) => set(i, { timeframes: v })} fmt={(v) => `${v}m`} />
            </div>
            <div>
              <div className="text-xs text-muted">Set types</div>
              <Chips all={TYPES} on={e.types as (typeof TYPES)[number][]} onChange={(v) => set(i, { types: v })} />
            </div>
          </div>
          <div className="grid gap-2 sm:grid-cols-3">
            <label className="text-xs text-muted">
              TP step min
              <input type="number" min={2} max={50} className="mt-1 w-full rounded-md border border-border bg-transparent px-2 py-1 text-sm" value={e.stepMin}
                onChange={(ev) => set(i, { stepMin: Math.max(2, Math.min(50, Number(ev.target.value) || 2)) })} />
            </label>
            <label className="text-xs text-muted">
              TP step max
              <input type="number" min={2} max={50} className="mt-1 w-full rounded-md border border-border bg-transparent px-2 py-1 text-sm" value={e.stepMax}
                onChange={(ev) => set(i, { stepMax: Math.max(e.stepMin, Math.min(50, Number(ev.target.value) || e.stepMin)) })} />
            </label>
            <label className="text-xs text-muted">
              SL ratios (x TP)
              <input className="mt-1 w-full rounded-md border border-border bg-transparent px-2 py-1 text-sm" value={e.slRatios.join(", ")}
                onChange={(ev) => set(i, { slRatios: ev.target.value.split(/[,;\s]+/).map(Number).filter((x) => Number.isFinite(x) && x > 0) })} />
            </label>
          </div>
        </div>
      ))}
    </div>
  );
}

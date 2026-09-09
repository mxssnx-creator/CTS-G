import { useId, useMemo, useState, type KeyboardEvent, type ReactNode } from "react";
import type { LiveStats } from "@/lib/live-stats";
import {
  changeSetSelection, INDICATION_GROUPS, INITIAL_SET_SELECTION, matchesSetGroup,
  readSetOverview, STRATEGY_GROUPS, tpRangeLabel,
  type SetGroup, type SetOverviewRow, type SetSelection,
} from "@/lib/set-overview";

type Option = { value: string; label: string; count: number };

function FilterRow({ label, level, value, options, onChange, panelId }: {
  label: string; level: string; value: string; options: Option[]; onChange: (value: string) => void; panelId: string;
}) {
  const keyboard = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const next = event.key === "ArrowRight" ? (index + 1) % options.length
      : event.key === "ArrowLeft" ? (index - 1 + options.length) % options.length
      : event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 : -1;
    if (next < 0) return;
    event.preventDefault();
    onChange(options[next].value);
    event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>("button")[next]?.focus();
  };
  return (
    <div className="min-w-0" data-set-filter={level}>
      <p className="mb-1 font-mono text-[10px] uppercase tracking-wide text-muted">{label}</p>
      <div role="tablist" aria-label={label} className="flex gap-1 overflow-x-auto rounded-lg border border-border bg-bg2 p-1">
        {options.map((option, index) => (
          <button key={option.value} type="button" role="tab" aria-selected={option.value === value}
            aria-controls={panelId} tabIndex={option.value === value ? 0 : -1}
            data-value={option.value} onClick={() => onChange(option.value)} onKeyDown={(event) => keyboard(event, index)}
            className={`min-h-11 shrink-0 rounded-md px-3 py-2 font-mono text-xs transition-colors focus-visible:outline-2 focus-visible:outline-primary ${option.value === value ? "bg-surface text-primary shadow-sm" : "text-muted hover:text-fg"}`}>
            {option.label}<span className="ml-2 text-[10px] tabular-nums">{option.count.toLocaleString("en-US")}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

export type SetGroupContext = { groups: SetGroup[]; selection: SetSelection; select: (selection: SetSelection) => void; version: number };

export function SetGroups({ sets, axesEnabled = false, limit, children, showEmptyPanel = false }: {
  sets: LiveStats["sets"]; axesEnabled?: boolean; limit?: number; showEmptyPanel?: boolean;
  children: (rows: SetOverviewRow[], context: SetGroupContext) => ReactNode;
}) {
  const [storedSelection, setSelection] = useState(INITIAL_SET_SELECTION);
  const selection = !axesEnabled && storedSelection.strategy === "axis" ? { ...storedSelection, strategy: "all" } : storedSelection;
  const overview = useMemo(() => {
    const data = readSetOverview(sets);
    return axesEnabled ? data : { ...data, rows: data.rows.filter((row) => row.strategyType !== "axis"), groups: data.groups.filter((group) => group.strategyType !== "axis") };
  }, [sets, axesEnabled]);
  const panelId = useId();
  const change = (level: keyof SetSelection, value: string) => setSelection((current) => changeSetSelection(current, level, value));
  const count = (filter: SetSelection) => overview.groups.filter((group) => matchesSetGroup(group, filter)).reduce((n, group) => n + group.setCount, 0);
  const ranges = [...new Set(overview.groups.filter((group) => matchesSetGroup(group, { ...selection, range: "all", strategy: "all" })).map((group) => group.tpRange))]
    .sort((a, b) => a === "unknown" ? 1 : b === "unknown" ? -1 : Number(a) - Number(b));
  if (selection.range !== "all" && !ranges.includes(selection.range)) ranges.push(selection.range);
  const options = (level: "indication" | "range" | "strategy", values: string[]): Option[] => ["all", ...values].map((value) => ({
    value, label: value === "all" ? "All" : level === "range" ? tpRangeLabel(value) : value === "axis" ? "Axis" : value === "dca" ? "DCA" : value[0].toUpperCase() + value.slice(1),
    count: count(changeSetSelection(selection, level, value)),
  }));
  const matched = overview.rows.filter((row) => matchesSetGroup(row, selection));
  const rows = limit === undefined ? matched : matched.slice(0, limit);
  return (
    <div className="min-w-0 space-y-3" data-testid="set-groups">
      <FilterRow label="Results source" level="scope" value={selection.scope} panelId={panelId} onChange={(value) => change("scope", value)} options={[
        { value: "system", label: "System", count: count({ ...INITIAL_SET_SELECTION, scope: "system" }) },
        { value: "exchange", label: "Exchange", count: count({ ...INITIAL_SET_SELECTION, scope: "exchange" }) },
      ]} />
      <FilterRow label="Indications" level="indication" value={selection.indication} panelId={panelId} onChange={(value) => change("indication", value)} options={options("indication", INDICATION_GROUPS)} />
      <FilterRow label="Sets · TP range" level="range" value={selection.range} panelId={panelId} onChange={(value) => change("range", value)} options={options("range", ranges)} />
      <FilterRow label="Strategy" level="strategy" value={selection.strategy} panelId={panelId} onChange={(value) => change("strategy", value)} options={options("strategy", STRATEGY_GROUPS.filter((strategy) => axesEnabled || strategy !== "axis"))} />
      <div id={panelId} role="tabpanel" aria-label="Selected set results" tabIndex={0} className="min-w-0 space-y-2 focus-visible:outline-2 focus-visible:outline-primary">
        <p className="font-mono text-[11px] text-muted" role="status">
          {selection.scope === "system" ? "Simulated · system internal calculations" : "Exchange · confirmed completed results"}
          {` · showing ${rows.length} of ${count(selection).toLocaleString("en-US")} ${overview.version ? "configurations" : "available preview rows"}`}
        </p>
        {rows.length || showEmptyPanel ? children(rows, { groups: overview.groups.filter((group) => matchesSetGroup(group, selection)), selection, select: setSelection, version: overview.version }) : <p className="rounded-lg border border-dashed border-border px-3 py-6 text-center text-sm text-muted">No results for this selection yet.</p>}
      </div>
    </div>
  );
}

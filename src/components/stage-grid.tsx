import { type ReactNode } from "react";
import type { LiveStats } from "@/lib/live-stats";
import {
  type StageGridConfig, type StageName, LONG_COUNTS, SHORT_COUNTS, EXEC_SHORT_COUNTS, LAST_COUNTS,
  SIGN_MODES, LONG_MODES, STAGE_ORDER, STAGE_LABEL, stageGridOf,
} from "@/lib/stage-grid";

function Choices<T extends string | number>({ label, choices, values, onChange, display }: {
  label: string;
  choices: readonly T[];
  values: readonly T[];
  onChange: (values: T[]) => void;
  display?: (choice: T) => ReactNode;
}) {
  return (
    <fieldset className="space-y-1">
      <legend className="text-xs font-medium text-muted">{label}</legend>
      <div className="flex flex-wrap gap-1">
        {choices.map((choice) => {
          const selected = values.includes(choice);
          return (
            <label key={choice} className={`flex items-center gap-1.5 rounded border px-2 py-1 text-xs ${selected ? "border-primary/40 text-foreground" : "border-border text-muted"}`}>
              <input
                type="checkbox"
                checked={selected}
                disabled={selected && values.length === 1}
                onChange={() => onChange(selected ? values.filter((v) => v !== choice) : [...values, choice])}
              />
              {display?.(choice) ?? choice}
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

export function StageGridSettings({ value, onChange }: {
  value: StageGridConfig;
  onChange: (value: StageGridConfig) => void;
}) {
  const changeStage = (stage: StageName, update: Record<string, unknown>) =>
    onChange(stageGridOf({ ...value, [stage]: { ...value[stage], ...update } }));
  return (
    <div className="space-y-3">
      <label className="flex items-center gap-2 text-sm font-medium">
        <input type="checkbox" checked={value.enabled} onChange={(e) => onChange({ ...value, enabled: e.target.checked })} />
        Use independent Last-N lanes through Exec
      </label>
      {value.enabled && (
        <>
          <p className="text-xs text-muted">
            Every enabled combination gets its own Set at each stage. The Long, Short and Last blocks use distinct earlier closes.
            Long and positive Short use the Set's PF floor. Negative Short and signed Last compare PF against 1.
            Negative Short is available for 4, 8 and 12 samples.
          </p>
          <div className="grid gap-3 md:grid-cols-2">
            {STAGE_ORDER.map((stage) => {
              const options = value[stage];
              return (
                <section key={stage} className="space-y-2 rounded-md border border-border p-3">
                  <h3 className="text-sm font-medium">{STAGE_LABEL[stage]}</h3>
                  {stage === "base"
                    ? <Choices<number> label="Long" choices={LONG_COUNTS} values={value.base.long} onChange={(long) => changeStage(stage, { long })} />
                    : <Choices label="Long" choices={LONG_MODES} values={value[stage].longModes} onChange={(longModes) => changeStage(stage, { longModes })} display={(mode) => mode === "inherit" ? "Inherit Base Long" : "Without Long"} />}
                  <Choices<number> label="Short" choices={stage === "exec" ? EXEC_SHORT_COUNTS : SHORT_COUNTS} values={options.short} onChange={(short) => changeStage(stage, { short })} />
                  <Choices label="Short sign" choices={options.short.some((n) => n <= 12) ? SIGN_MODES : ["positive"] as const} values={options.shortModes} onChange={(shortModes) => changeStage(stage, { shortModes })} display={signLabel} />
                  <Choices<number> label="Last" choices={LAST_COUNTS} values={options.last} onChange={(last) => changeStage(stage, { last })} />
                  <Choices label="Last sign" choices={SIGN_MODES} values={options.lastModes} onChange={(lastModes) => changeStage(stage, { lastModes })} display={signLabel} />
                </section>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}

function signLabel(mode: string) {
  return mode === "positive" ? "Positive" : "Negative";
}

const count = new Intl.NumberFormat();

export function StageGridPanel({ live }: { live?: LiveStats | null }) {
  const grid = live?.sets?.stageGrid;
  if (!grid) return null;
  const executed = live.execution?.exec ?? live.execStage;
  return (
    <section className="space-y-3 rounded-md border border-border bg-surface p-4" aria-label="Independent Last-N stages">
      <div>
        <h2 className="text-sm font-semibold">Base → Main → Real → Exec → Live</h2>
        <p className="text-xs text-muted">{count.format(grid.baseSets)} Base Sets · sample coverage among reached lanes</p>
      </div>
      {grid.validatedBase && (
        <p className="text-xs">
          Validated Base lanes · {count.format(grid.validatedBase.orders)} paper closes · PF{" "}
          {grid.validatedBase.pf?.toFixed(2) ?? (grid.validatedBase.gain > 0 ? "No losses" : "—")}
        </p>
      )}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[37rem] text-left text-xs">
          <thead className="text-muted">
            <tr>
              {["Stage", "Configured lanes", "Reached", "Full samples", "Qualified", "Window orders", "Window PF", "Post-stage closes", "Post-stage paper PF"].map((label, i) => (
                <th key={label} scope="col" className={i ? "px-2 pb-2 text-right font-medium" : "pb-2 font-medium"}>{label}</th>
              ))}
            </tr>
          </thead>
          <tbody className="font-mono">
            {STAGE_ORDER.map((stage) => {
              const row = grid.stages[stage];
              const post = grid.postStages?.[stage];
              return (
                <tr key={stage} className="border-t border-border">
                  <th scope="row" className="py-2 font-medium">{STAGE_LABEL[stage]}</th>
                  <td className="px-2 text-right">{count.format(row.configured)}</td>
                  <td className="px-2 text-right">{count.format(row.evaluated)}</td>
                  <td className="px-2 text-right">{count.format(row.sampleFull)} · {row.evaluated ? `${row.samplePct.toFixed(1)}%` : "—"}</td>
                  <td className="px-2 text-right">
                    {count.format(row.qualified)}
                    {row.negativeShort != null && row.negativeLast != null && (
                      <div className="whitespace-nowrap text-[10px] text-muted">
                        {count.format(row.negativeShort)} Short− · {count.format(row.negativeLast)} Last−
                      </div>
                    )}
                  </td>
                  <td className="px-2 text-right">{count.format(row.orders)}</td>
                  <td className="px-2 text-right">{row.pf?.toFixed(2) ?? (row.gain > 0 && row.loss === 0 ? "No losses" : "—")}</td>
                  <td className="px-2 text-right">{post ? count.format(post.orders) : "—"}</td>
                  <td className="px-2 text-right">{post?.pf?.toFixed(2) ?? (post && post.gain > 0 && post.loss === 0 ? "No losses" : "—")}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {grid.postStages && <p className="text-xs text-muted">Post-stage PF uses the closed paper positions in the selected path of each Base Set.</p>}
      {executed && (
        <p className="text-xs text-muted">
          Live mirrors Exec · {count.format(executed.activePositions)} active positions · {count.format(executed.activeFilledOrders)} filled orders on active positions
          {executed.activeControlOrders != null && ` · ${count.format(executed.activeControlOrders)} live controls`}
          {" · "}{count.format(executed.closedFilledOrders)} filled orders on closed positions · {executed.orders == null || !executed.orders ? "Exec PF awaiting settled positions" : executed.noLoss ? `Exec PF no losses (${executed.orders} closed positions)` : `Exec PF ${executed.pf?.toFixed(2) ?? "—"} (${executed.orders} closed positions)`}
          {!!executed.unverifiedPositions && ` · ${executed.unverifiedPositions} closed positions awaiting verified returns`}
        </p>
      )}
    </section>
  );
}

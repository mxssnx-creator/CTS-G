import { useState } from "react";
import type { LiveStats } from "@/lib/live-stats";

type Grid = NonNullable<NonNullable<LiveStats["sets"]>["indicationGrid"]>;
const PAGE_SIZE = 100;

export function IndependentCalculations({ grid }: { grid?: Grid }) {
  const [page, setPage] = useState(0);
  if (!grid) return null;
  const rows = grid.configurations ?? [];
  const totals = rows.reduce((sum, row) => ({
    requested: sum.requested + row.requested,
    completed: sum.completed + row.completed,
    base: sum.base + row.qualifiedBase,
    real: sum.real + row.qualifiedReal,
    errors: sum.errors + Number(Boolean(row.error)),
  }), { requested: 0, completed: 0, base: 0, real: 0, errors: 0 });
  const lastPage = Math.max(0, Math.ceil(rows.length / PAGE_SIZE) - 1);
  const currentPage = Math.min(page, lastPage);
  const shown = rows.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);
  return (
    <details className="min-w-0 rounded-lg border border-border bg-bg2 p-3" data-testid="independent-calculations">
      <summary className="cursor-pointer font-mono text-xs">
        Independent calculations · {totals.completed.toLocaleString()}/{totals.requested.toLocaleString()} exits evaluated
        {` · Base ${totals.base.toLocaleString()} · Real ${totals.real.toLocaleString()} · ${grid.complete ? "requested work complete" : "processing / waiting for evidence"}`}
      </summary>
      <p className="mt-2 text-xs text-muted">
        Each symbol, indication configuration, exit and direction uses its own return-PF evidence. Counts describe historical qualification; live admission also applies current execution gates.
        {totals.errors ? ` ${totals.errors} calculation errors.` : ""}
      </p>
      {rows.length ? (
        <>
          <div className="mt-2 max-w-full overflow-x-auto">
            <table className="w-full min-w-[48rem] font-mono text-xs">
              <thead><tr>{["Symbol", "Indication / configuration", "Phase", "Evaluated exits", "Base", "Real", "Min return PF", "Seconds"].map(label => <th key={label} className="p-2 text-left font-medium text-muted">{label}</th>)}</tr></thead>
              <tbody>{shown.map(row => (
                <tr key={JSON.stringify([row.symbol, row.kind, row.config])} className="border-t border-border">
                  <td className="p-2">{row.symbol || "—"}</td>
                  <td className="p-2">{row.kind} · {row.config || "default"}</td>
                  <td className={`p-2 ${row.error ? "text-danger" : ""}`} title={row.error}>{row.phase}</td>
                  <td className="p-2">{row.completed.toLocaleString()}/{row.requested.toLocaleString()}</td>
                  <td className="p-2">{row.qualifiedBase.toLocaleString()}</td>
                  <td className="p-2">{row.qualifiedReal.toLocaleString()}</td>
                  <td className="p-2">{row.minPf.toFixed(2)}</td>
                  <td className="p-2">{row.elapsedMs != null ? (row.elapsedMs / 1000).toFixed(1) : "—"}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          <div className="mt-2 flex items-center gap-3 text-xs">
            <button className="rounded border border-border px-3 py-1 disabled:opacity-40" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>Previous</button>
            <span>Page {currentPage + 1}/{lastPage + 1} · {rows.length} independent configurations</span>
            <button className="rounded border border-border px-3 py-1 disabled:opacity-40" disabled={currentPage === lastPage} onClick={() => setPage(currentPage + 1)}>Next</button>
          </div>
        </>
      ) : <p className="mt-2 text-xs text-muted">No independent calculation has been requested yet.</p>}
    </details>
  );
}

import { compactSetLabel, setLabel, type SetOverviewRow } from "@/lib/set-overview";

/** Short overview text; the full identity remains accessible on touch screens. */
export function SetIdentity({ row }: { row: SetOverviewRow }) {
  const fullLabel = setLabel(row);
  return <details className="group min-w-0 max-w-full text-left" data-testid="set-identity">
    <summary title={fullLabel} className="flex min-h-11 cursor-pointer list-none items-center gap-1 text-fg [&::-webkit-details-marker]:hidden">
      <span aria-hidden="true" className="shrink-0 transition-transform group-open:rotate-90">›</span>
      <span className="min-w-0 truncate">{compactSetLabel(row)}</span>
    </summary>
    <div className="max-w-full space-y-1 pb-2 text-[10px] leading-relaxed text-muted [overflow-wrap:anywhere]">
      <p>{fullLabel}</p>
      <p>{row.setId || row.id}</p>
    </div>
  </details>;
}

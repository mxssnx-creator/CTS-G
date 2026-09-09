import { compactSetLabel, setLabel, type SetOverviewRow } from "@/lib/set-overview";

/** Short overview text; the full identity remains accessible on touch screens. */
export function SetIdentity({ row }: { row: SetOverviewRow }) {
  const fullLabel = setLabel(row);
  return <details className="min-w-0 max-w-full text-left" data-testid="set-identity">
    <summary title={fullLabel} className="min-h-11 cursor-pointer content-center overflow-hidden text-ellipsis whitespace-nowrap text-fg">
      {compactSetLabel(row)}
    </summary>
    <div className="max-w-full space-y-1 pb-2 text-[10px] leading-relaxed text-muted [overflow-wrap:anywhere]">
      <p>{fullLabel}</p>
      <p>{row.setId || row.id}</p>
    </div>
  </details>;
}

import type { ReactNode } from "react";
import { posOrdersCounts } from "@/lib/live-stats";

const TITLE = "Positions = unique symbol+direction (both sides count twice). Orders = complete working book.";

function shown(value: number | null | undefined): string {
  return value == null ? "—" : String(value);
}

export function PosOrdersLine({
  real,
  live,
  extra,
  className = "",
  stats,
}: {
  real?: string;
  live?: string;
  extra?: ReactNode;
  className?: string;
  stats?: Parameters<typeof posOrdersCounts>[0];
}) {
  const parts = stats ? posOrdersCounts(stats) : null;
  return (
    <span className={`inline-flex min-w-0 flex-wrap items-baseline gap-x-1.5 ${className}`} title={TITLE}>
      <span className="whitespace-nowrap">Positions/Orders</span>
      {parts ? (
        <>
          <span className="whitespace-nowrap">Pos R {shown(parts.realPositions)} L {shown(parts.livePositions)}</span>
          <span className="whitespace-nowrap">Ord R {shown(parts.realOrders)} L {shown(parts.liveOrders)}</span>
        </>
      ) : (
        <>
          <span className="whitespace-nowrap">R {real}</span>
          <span className="whitespace-nowrap">L {live}</span>
        </>
      )}
      {extra}
    </span>
  );
}

export function PosOrdersBlock({
  real,
  live,
  realExtra,
  liveClassName = "text-fg",
  stats,
}: {
  real?: string;
  live?: string;
  realExtra?: string;
  liveClassName?: string;
  stats?: Parameters<typeof posOrdersCounts>[0];
}) {
  const parts = stats ? posOrdersCounts(stats) : null;
  return (
    <div className="min-w-0" title={TITLE}>
      <p className="font-mono text-[10px] tracking-wide text-muted uppercase">Positions/Orders</p>
      <dl className="stat-rows mt-1 font-mono text-xs text-muted">
        <dt>Positions</dt>
        <dd className="text-fg">
          R {parts ? shown(parts.realPositions) : (real ?? "—").split("/")[0]}
          <span className={`ml-2 ${liveClassName}`}>L {parts ? shown(parts.livePositions) : (live ?? "—").split("/")[0]}</span>
          {realExtra ? <span className="ml-1 text-muted">{realExtra}</span> : null}
        </dd>
        <dt>Orders</dt>
        <dd className="text-fg">
          R {parts ? shown(parts.realOrders) : (real ?? "—").split("/")[1] ?? "—"}
          <span className={`ml-2 ${liveClassName}`}>L {parts ? shown(parts.liveOrders) : (live ?? "—").split("/")[1] ?? "—"}</span>
        </dd>
      </dl>
    </div>
  );
}
import type { OverallScope } from "./live-stats.ts";

export function scopeStatus(allow: boolean | undefined): string {
  return allow === true ? "open" : allow === false ? "blocked" : "not evaluated";
}

/** A legacy sidecar's `pf` was cash-weighted; never present it as return PF. */
export function executionScopeText(executed: OverallScope["executed"]): string {
  if (executed.basis !== "net-position-return") {
    const legacy = executed.pf == null ? "—" : executed.pf.toFixed(2);
    return `position return PF — · legacy PF ${legacy} (basis unverified) (${executed.orders})`;
  }
  const result = (pf: number | null | undefined, noLoss: boolean | undefined) =>
    noLoss ? "no observed losses" : pf == null ? "—" : pf.toFixed(2);
  const missing = executed.missingReturns ?? 0;
  const missingCash = executed.missingCash ?? 0;
  const sourceNames: Record<string, string> = {
    "reported-fee": "reported",
    "exchange-rate-estimate": "rate estimate",
    "configured-estimate": "configured estimate",
    unknown: "unknown",
  };
  const fees = Object.entries(executed.feeSources ?? {})
    .filter(([, n]) => n > 0)
    .map(([key, n]) => `${n} ${sourceNames[key] ?? "unknown"}`)
    .join(", ");
  return `position return PF ${result(executed.pf, executed.noLoss)} (${executed.returnOrders ?? 0}/${executed.orders} recorded closes${missing ? `, ${missing} missing` : ""})`
    + ` · cash PF ${result(executed.cashPf, executed.cashNoLoss)} (${executed.cashOrders ?? 0}/${executed.orders}${missingCash ? `, ${missingCash} missing` : ""})`
    + (fees ? ` · fees: ${fees}` : "");
}

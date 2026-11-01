import type { LiveStats } from "./live-stats.ts";
import { rowNetPnlPct } from "./analytics.ts";

type Closed = NonNullable<LiveStats["closed"]>[number];

/** O(closes + hours). Closed-return and cash-weighted evidence are distinct.
 * This is NOT account equity P&L: no historic open-position/funding marks exist.
 * Missing, flat and no-loss hours have no finite PF; exclude future closes. */
export function hourlyPf(closed: Closed[], costPct: number, now: number, hours = 24) {
  const nowS = now > 1e11 ? now / 1000 : now;
  if (!Number.isFinite(nowS) || !Number.isInteger(hours) || hours < 1 || hours > 168) return [];
  const hourStart = Math.floor(nowS / 3600) * 3600;
  const first = hourStart - (hours - 1) * 3600;
  const buckets = Array.from({ length: hours }, () => ({ gp: 0, gl: 0, n: 0, missing: 0,
    cashGp: 0, cashGl: 0, cashMissing: 0, estimated: 0 }));
  for (const c of closed) {
    const t = c.t > 1e11 ? c.t / 1000 : c.t;
    if (!Number.isFinite(t) || t < first || t > nowS) continue;
    const b = buckets[Math.floor((t - first) / 3600)];
    if (!b) continue;
    const hasNet = c.net_pnl_pct != null && Number.isFinite(Number(c.net_pnl_pct));
    const hasGross = c.pnl_pct != null && Number.isFinite(Number(c.pnl_pct));
    if (!hasNet && !hasGross) { b.missing += 1; continue; }
    const net = rowNetPnlPct(c, costPct);
    b.n += 1;
    b.gp += Math.max(0, net); b.gl += Math.max(0, -net);
    if (!hasNet || c.fee_source !== "reported-fee") b.estimated += 1;
    // pnl is legacy/ambiguous. Reconstruct cash only from a measurable net and
    // its own actually closed quantity/entry, never an average/global notional.
    const notional = Number(c.entry) * Number(c.qty);
    if (!(Number(c.entry) > 0 && Number(c.qty) > 0 && Number.isFinite(notional) && Number.isFinite(notional * net))) {
      b.cashMissing += 1; continue;
    }
    b.cashGp += Math.max(0, net * notional); b.cashGl += Math.max(0, -net * notional);
  }
  return buckets.map((b, i) => {
    const from = first + i * 3600;
    const to = from + 3600;
    const complete = b.n > 0 && b.missing === 0;
    const cashComplete = complete && b.cashMissing === 0;
    return { hour: new Date(from * 1000).toISOString().slice(11, 13) + "h", from, to, partial: nowS < to,
      pf: complete && b.gl > 0 ? b.gp / b.gl : null, n: b.n, missing: b.missing,
      noLoss: complete && b.gp > 0 && b.gl === 0, flat: complete && b.gp === 0 && b.gl === 0,
      netReturnSum: complete ? b.gp - b.gl : null, netCash: cashComplete ? b.cashGp - b.cashGl : null,
      cashPf: cashComplete && b.cashGl > 0 ? b.cashGp / b.cashGl : null,
      cashNoLoss: cashComplete && b.cashGp > 0 && b.cashGl === 0,
      cashMissing: b.cashMissing, estimated: b.estimated };
  });
}

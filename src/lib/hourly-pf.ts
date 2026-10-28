import type { LiveStats } from "./live-stats.ts";

type Closed = NonNullable<LiveStats["closed"]>[number];

/** Net PF of the closes in each of the last `hours` clock hours (oldest first). Net = gross fraction less one
 * PositionCost; hours without a decided trade are null so the line breaks instead of reading as 0. */
export function hourlyPf(closed: Closed[], costPct: number, now: number, hours = 24) {
  const out: { hour: string; pf: number | null; n: number }[] = [];
  const nowS = now > 1e11 ? now / 1000 : now;
  const hourStart = Math.floor(nowS / 3600) * 3600;
  for (let h = hours - 1; h >= 0; h--) {
    const from = hourStart - h * 3600;
    const to = from + 3600;
    let gp = 0;
    let gl = 0;
    let n = 0;
    for (const c of closed) {
      const t = c.t > 1e11 ? c.t / 1000 : c.t;
      if (t < from || t >= to || c.pnl_pct === null || c.pnl_pct === undefined) continue;
      const net = Number(c.pnl_pct) - costPct / 100;
      n += 1;
      if (net > 0) gp += net;
      else if (net < 0) gl += -net;
    }
    const pf = n === 0 ? null : gl > 0 ? Math.min(gp / gl, 9.99) : gp > 0 ? 9.99 : 0;
    out.push({ hour: new Date(from * 1000).toISOString().slice(11, 13) + "h", pf, n });
  }
  return out;
}

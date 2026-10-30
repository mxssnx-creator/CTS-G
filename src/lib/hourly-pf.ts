import type { LiveStats } from "./live-stats.ts";
import { rowNetPnlPct } from "./analytics.ts";

type Closed = NonNullable<LiveStats["closed"]>[number];

/** Net PF of recorded closes in each clock hour (oldest first). Missing,
 * flat and no-loss hours have no finite PF; never count a future close. */
export function hourlyPf(closed: Closed[], costPct: number, now: number, hours = 24) {
  const out: { hour: string; pf: number | null; n: number; missing: number; noLoss: boolean; flat: boolean }[] = [];
  const nowS = now > 1e11 ? now / 1000 : now;
  const hourStart = Math.floor(nowS / 3600) * 3600;
  for (let h = hours - 1; h >= 0; h--) {
    const from = hourStart - h * 3600;
    const to = from + 3600;
    let gp = 0;
    let gl = 0;
    let n = 0;
    let missing = 0;
    for (const c of closed) {
      const t = c.t > 1e11 ? c.t / 1000 : c.t;
      if (!Number.isFinite(t) || t < from || t >= to || t > nowS) continue;
      const hasNet = c.net_pnl_pct != null && Number.isFinite(Number(c.net_pnl_pct));
      const hasGross = c.pnl_pct != null && Number.isFinite(Number(c.pnl_pct));
      if (!hasNet && !hasGross) { missing += 1; continue; }
      const net = rowNetPnlPct(c, costPct);
      n += 1;
      if (net > 0) gp += net;
      else if (net < 0) gl += -net;
    }
    const complete = n > 0 && missing === 0;
    const pf = complete && gl > 0 ? gp / gl : null;
    out.push({ hour: new Date(from * 1000).toISOString().slice(11, 13) + "h", pf, n, missing,
      noLoss: complete && gp > 0 && gl === 0, flat: complete && gp === 0 && gl === 0 });
  }
  return out;
}

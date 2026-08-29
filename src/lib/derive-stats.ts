import type { LiveClosed, LiveStats } from "@/lib/live-stats";

export type Derived = {
  equityCurve: { i: number; t: number; pnl: number; eq: number; symbol: string }[];
  tradeBars: { i: number; symbol: string; pnl: number; hold: number; reason: string }[];
  bySymbol: { symbol: string; pnl: number; n: number; wins: number }[];
  byReason: { reason: string; n: number; pnl: number }[];
  gp: number;
  gl: number;
  pf: number;
  avgWin: number;
  avgLoss: number;
  avgHold: number;
  expectancy: number;
  best: LiveClosed | null;
  worst: LiveClosed | null;
  longs: number;
  shorts: number;
  marginPct: number;
  ddPct: number;
};

export function derive(stats: LiveStats | null): Derived {
  const closed = [...(stats?.closed ?? [])].slice().reverse();
  const equityCurve: Derived["equityCurve"] = [];
  let acc = 0;
  closed.forEach((c, i) => {
    acc += c.pnl;
    equityCurve.push({ i, t: c.t, pnl: acc, eq: acc, symbol: c.symbol.replace("-USDT", "") });
  });
  const tradeBars = closed.map((c, i) => ({
    i,
    symbol: c.symbol.replace("-USDT", ""),
    pnl: c.pnl,
    hold: c.hold_s,
    reason: c.reason,
  }));
  const symMap = new Map<string, { pnl: number; n: number; wins: number }>();
  const reasonMap = new Map<string, { n: number; pnl: number }>();
  let gp = 0;
  let gl = 0;
  let holdSum = 0;
  let longs = 0;
  let shorts = 0;
  for (const c of closed) {
    const s = c.symbol.replace("-USDT", "");
    const cur = symMap.get(s) ?? { pnl: 0, n: 0, wins: 0 };
    cur.pnl += c.pnl;
    cur.n += 1;
    if (c.pnl > 0) cur.wins += 1;
    symMap.set(s, cur);
    const r = c.reason || "other";
    const rr = reasonMap.get(r) ?? { n: 0, pnl: 0 };
    rr.n += 1;
    rr.pnl += c.pnl;
    reasonMap.set(r, rr);
    if (c.pnl > 0) gp += c.pnl;
    if (c.pnl < 0) gl += Math.abs(c.pnl);
    holdSum += c.hold_s;
    if (c.side === "LONG") longs += 1;
    else shorts += 1;
  }
  const wins = closed.filter((c) => c.pnl > 0);
  const losses = closed.filter((c) => c.pnl < 0);
  const used = stats?.usedMargin ?? 0;
  const avail = stats?.available ?? 0;
  const best = closed.reduce<LiveClosed | null>((a, c) => (!a || c.pnl > a.pnl ? c : a), null);
  const worst = closed.reduce<LiveClosed | null>((a, c) => (!a || c.pnl < a.pnl ? c : a), null);
  return {
    equityCurve,
    tradeBars,
    bySymbol: [...symMap.entries()]
      .map(([symbol, v]) => ({ symbol, ...v }))
      .sort((a, b) => b.pnl - a.pnl),
    byReason: [...reasonMap.entries()]
      .map(([reason, v]) => ({ reason, ...v }))
      .sort((a, b) => b.n - a.n),
    gp,
    gl,
    pf: stats?.pfCost?.ratio ?? (gl > 0 ? gp / gl : gp > 0 ? 99 : 0),
    avgWin: wins.length ? gp / wins.length : 0,
    avgLoss: losses.length ? gl / losses.length : 0,
    avgHold: closed.length ? holdSum / closed.length : 0,
    expectancy: closed.length ? (gp - gl) / closed.length : 0,
    best,
    worst,
    longs,
    shorts,
    marginPct: used + avail > 0 ? (used / (used + avail)) * 100 : 0,
    ddPct: stats?.drawdownPct ?? 0,
  };
}

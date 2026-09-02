/** CTS live-trading analytics: profit factor windows + drawdown-time average. */

export type PfRow = { pnl: number; t: number; notional?: number; pnl_pct?: number };

export type ProfitFactorMetric = {
  trades: number;
  wins: number;
  losses: number;
  flat: number;
  grossProfit: number;
  grossLoss: number;
  netPnl: number;
  winRate: number;
  profitFactor: number | null;
  infinite: boolean;
};

export type DrawdownTimeMetric = {
  lookbackDays: number;
  samples: number;
  episodes: number;
  maxDurationMs: number;
  averageDurationMs: number;
  currentDurationMs: number;
  totalDurationMs: number;
  maxDepth: number;
  currentDepth: number;
  inDrawdown: boolean;
};

export type CostPfMetric = {
  n: number;
  count: number;
  avgR: number;
  ratio: number;
  classicPf: number;
  costPct: number;
  netPct: number;
  grossPct: number;
  netAvg?: number;
  costSubtracted?: boolean;
  minPf: number;
  pass: boolean;
};

export const POSITION_COST_PCT_DEFAULT = 0.15;
export const COST_RATIO_SCALE = 0.1;

function finite(v: unknown): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function round(value: number, precision = 4): number {
  const scale = 10 ** precision;
  return Math.round((value + Number.EPSILON) * scale) / scale;
}

export function calculateProfitFactorMetric(rows: PfRow[]): ProfitFactorMetric {
  let wins = 0;
  let losses = 0;
  let flat = 0;
  let grossProfit = 0;
  let grossLoss = 0;
  for (const row of rows) {
    const pnl = finite(row.pnl);
    if (pnl > 0) {
      wins += 1;
      grossProfit += pnl;
    } else if (pnl < 0) {
      losses += 1;
      grossLoss += Math.abs(pnl);
    } else {
      flat += 1;
    }
  }
  const decided = wins + losses;
  const infinite = grossProfit > 0 && grossLoss === 0;
  return {
    trades: rows.length,
    wins,
    losses,
    flat,
    grossProfit: round(grossProfit),
    grossLoss: round(grossLoss),
    netPnl: round(grossProfit - grossLoss),
    winRate: decided > 0 ? round((wins / decided) * 100, 2) : 0,
    profitFactor: grossLoss > 0 ? round(grossProfit / grossLoss) : null,
    infinite,
  };
}

export function calculateDrawdownTime(
  rows: PfRow[],
  now = Date.now(),
  lookbackDays = 3,
): DrawdownTimeMetric {
  const cutoff = now - lookbackDays * 24 * 60 * 60 * 1000;
  const ordered = rows
    .filter((row) => {
      const ts = row.t < 10_000_000_000 ? row.t * 1000 : row.t;
      return ts >= cutoff && ts <= now;
    })
    .sort((a, b) => a.t - b.t);

  let equity = 0;
  let peak = 0;
  let drawdownStartedAt: number | null = null;
  let maxDurationMs = 0;
  let totalDurationMs = 0;
  let maxDepth = 0;
  let episodes = 0;

  for (const row of ordered) {
    const closedAt = row.t < 10_000_000_000 ? row.t * 1000 : row.t;
    equity += finite(row.pnl);
    if (equity >= peak) {
      if (drawdownStartedAt !== null) {
        const duration = Math.max(0, closedAt - drawdownStartedAt);
        maxDurationMs = Math.max(maxDurationMs, duration);
        totalDurationMs += duration;
        drawdownStartedAt = null;
      }
      peak = equity;
      continue;
    }
    if (drawdownStartedAt === null) {
      drawdownStartedAt = closedAt;
      episodes += 1;
    }
    maxDepth = Math.max(maxDepth, peak - equity);
  }

  const currentDurationMs =
    drawdownStartedAt === null ? 0 : Math.max(0, now - drawdownStartedAt);
  if (drawdownStartedAt !== null) {
    maxDurationMs = Math.max(maxDurationMs, currentDurationMs);
    totalDurationMs += currentDurationMs;
  }

  return {
    lookbackDays,
    samples: ordered.length,
    episodes,
    maxDurationMs,
    averageDurationMs: episodes > 0 ? Math.round(totalDurationMs / episodes) : 0,
    currentDurationMs,
    totalDurationMs,
    maxDepth: round(maxDepth),
    currentDepth: round(Math.max(0, peak - equity)),
    inDrawdown: drawdownStartedAt !== null,
  };
}

/** CTS Main-trade scale: 1.00 = Neutral after cost, 1.10 = +1× PositionCost net. */
export function costAsFrac(costPct = POSITION_COST_PCT_DEFAULT): number {
  const c = Math.max(0, finite(costPct));
  return c > 0.05 ? c / 100 : c;
}

export function netPnlPct(pnlPctFraction: number, costPct = POSITION_COST_PCT_DEFAULT): number {
  return finite(pnlPctFraction) - costAsFrac(costPct);
}

export function signedResultR(pnlPctFraction: number, costPct = POSITION_COST_PCT_DEFAULT): number {
  const cost = Math.max(1e-9, finite(costPct));
  return (finite(pnlPctFraction) * 100 - cost) / cost;
}

export function ratioFromR(signedR: number): number {
  return 1 + finite(signedR) * COST_RATIO_SCALE;
}

export function lastNCostPf(
  rows: PfRow[],
  n = 15,
  costPct = POSITION_COST_PCT_DEFAULT,
  minPf = 1.1,
): CostPfMetric {
  const window = rows.slice(0, Math.max(1, n));
  const rs: number[] = [];
  const nets: number[] = [];
  let gp = 0;
  let gl = 0;
  for (const row of window) {
    rs.push(signedResultR(finite(row.pnl_pct), costPct));
    const net = row.pnl_pct != null ? netPnlPct(finite(row.pnl_pct), costPct) : finite(row.pnl);
    nets.push(net);
    if (net > 0) gp += net;
    else if (net < 0) gl += Math.abs(net);
  }
  const count = rs.length;
  const avgR = count ? rs.reduce((a, b) => a + b, 0) / count : 0;
  const ratio = count ? ratioFromR(avgR) : 1;
  const classic = gl > 0 ? gp / gl : gp > 0 ? 99 : 0;
  const netAvg = count ? nets.reduce((a, b) => a + b, 0) / count : 0;
  return {
    n,
    count,
    avgR: round(avgR),
    ratio: round(ratio),
    classicPf: round(classic),
    costPct,
    netPct: round(costPct * ((ratio - 1) / COST_RATIO_SCALE)),
    grossPct: round(costPct + costPct * ((ratio - 1) / COST_RATIO_SCALE)),
    netAvg: round(netAvg, 6),
    costSubtracted: true,
    minPf,
    pass: count < 8 || ratio + 1e-9 >= minPf,
  };
}

export function buildOverview(rows: PfRow[], now = Date.now()) {
  const newestFirst = [...rows].sort((a, b) => b.t - a.t);
  const withinHours = (hours: number) =>
    newestFirst.filter((row) => {
      const ts = row.t < 10_000_000_000 ? row.t * 1000 : row.t;
      return ts >= now - hours * 60 * 60 * 1000 && ts <= now;
    });
  return {
    generatedAt: now,
    last4: lastNCostPf(newestFirst, 4),
    costPf: lastNCostPf(newestFirst, 15, POSITION_COST_PCT_DEFAULT, 1.1),
    positionWindows: {
      "12": lastNCostPf(newestFirst, 12),
      "15": lastNCostPf(newestFirst, 15),
      "25": lastNCostPf(newestFirst, 25),
      "75": lastNCostPf(newestFirst, 75),
      "150": lastNCostPf(newestFirst, 150),
    } as const,
    timeWindows: {
      "4h": lastNCostPf(withinHours(4), withinHours(4).length || 1),
      "12h": lastNCostPf(withinHours(12), withinHours(12).length || 1),
      "48h": lastNCostPf(withinHours(48), withinHours(48).length || 1),
    } as const,
    drawdown3d: calculateDrawdownTime(newestFirst, now, 3),
    drawdownAll: calculateDrawdownTime(newestFirst, now, 3650),
  };
}

export function formatPf(m: ProfitFactorMetric): string {
  if (m.infinite) return "∞";
  if (m.profitFactor == null) return "—";
  return m.profitFactor.toFixed(2);
}

export function formatDuration(ms: number): string {
  if (!ms || ms < 0) return "0s";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  if (m < 60) return rs ? `${m}m ${rs}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm ? `${h}h ${rm}m` : `${h}h`;
}

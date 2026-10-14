/** CTS live-trading analytics: profit factor windows + drawdown-time average. */

import { BASE_EVAL_DEFAULT } from "./stage-windows.ts";

/** Same measured-cost aliases accepted by the engine's position_cost module. */
export type PositionCostRow = {
  notional?: number;
  quoteQty?: number;
  quoteQuantity?: number;
  cumQuote?: number;
  cumQuoteQty?: number;
  qty?: number;
  executedQty?: number;
  filledQty?: number;
  cumQty?: number;
  quantity?: number;
  entry?: number;
  avgPrice?: number;
  averagePrice?: number;
  price?: number;
  exit?: number;
  position_cost_pct?: number;
  positionCostPct?: number;
  cost_pct?: number;
  costPct?: number;
  cost_source?: string;
  costSource?: string;
  source?: string;
  fee_total?: unknown;
  feeTotal?: unknown;
  totalFee?: unknown;
  totalCommission?: unknown;
  entry_fee?: unknown;
  entryFee?: unknown;
  openFee?: unknown;
  openCommission?: unknown;
  exit_fee?: unknown;
  exitFee?: unknown;
  closeFee?: unknown;
  closeCommission?: unknown;
  fee?: unknown;
  fees?: unknown;
  feeAmount?: unknown;
  commission?: unknown;
  commissionAmount?: unknown;
  transactionFee?: unknown;
  realizedFee?: unknown;
  fee_scope?: string;
  feeScope?: string;
  fee_type?: string;
  feeType?: string;
  fee_rate?: number;
  feeRate?: number;
  commissionRate?: number;
  makerFeeRate?: number;
  takerFeeRate?: number;
};

export type PfRow = PositionCostRow & { pnl: number; t: number; symbol?: string; pnl_pct?: number };

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
  requiredSamples: number;
  pfReferenceCostPct?: number;
  zeroCostSamples?: number;
  costSamples?: number;
  costSource?: string;
  unavailableReturnSamples?: number;
  pass: boolean;
};

export type CostPfPolicy = { n?: number; minPf?: number; requiredSamples?: number };

export const POSITION_COST_PCT_DEFAULT = 0.10;
export const COST_RATIO_SCALE = 0.1;

function finite(v: unknown, fallback = 0): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
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
  costPct = POSITION_COST_PCT_DEFAULT,
): DrawdownTimeMetric {
  const cutoff = now - lookbackDays * 24 * 60 * 60 * 1000;
  // stats.closed arrives newest-first. Restore arrival order before the stable
  // sort so tied timestamps resolve exactly like the engine's chronological tape.
  const ordered = [...rows]
    .reverse()
    .filter((row) => {
      const ts = timestampMs(row.t);
      return ts >= cutoff && ts <= now;
    })
    .sort((a, b) => timestampMs(a.t) - timestampMs(b.t));

  const symbols = new Set(ordered.map((row) => row.symbol || "?"));
  if (symbols.size > 1) {
    const parts = [...symbols].map((symbol) =>
      calculateDrawdownTimeSingle(
        ordered.filter((row) => (row.symbol || "?") === symbol),
        now,
        lookbackDays,
        costPct,
      ),
    );
    const totalDurationMs = parts.reduce((sum, part) => sum + part.totalDurationMs, 0);
    const episodes = parts.reduce((sum, part) => sum + part.episodes, 0);
    return {
      lookbackDays,
      samples: ordered.length,
      episodes,
      maxDurationMs: Math.max(0, ...parts.map((part) => part.maxDurationMs)),
      averageDurationMs: episodes ? Math.round(totalDurationMs / episodes) : 0,
      currentDurationMs: Math.max(0, ...parts.map((part) => part.currentDurationMs)),
      totalDurationMs,
      maxDepth: round(Math.max(0, ...parts.map((part) => part.maxDepth))),
      currentDepth: round(Math.max(0, ...parts.map((part) => part.currentDepth))),
      inDrawdown: parts.some((part) => part.inDrawdown),
    };
  }
  return calculateDrawdownTimeSingle(ordered, now, lookbackDays, costPct);
}

function timestampMs(value: number): number {
  return value < 10_000_000_000 ? value * 1000 : value;
}

function rowEquityPnl(row: PfRow, costPct = POSITION_COST_PCT_DEFAULT): number {
  const pnl = finite(row.pnl);
  if (row.pnl != null && Math.abs(pnl) > 1e-15) return pnl;
  if (row.pnl_pct != null) return netPnlPct(finite(row.pnl_pct), rowPositionCostPct(row, costPct));
  return pnl;
}

function calculateDrawdownTimeSingle(
  ordered: PfRow[],
  now: number,
  lookbackDays: number,
  costPct: number,
): DrawdownTimeMetric {
  const lastTs = ordered.length ? timestampMs(ordered[ordered.length - 1].t) : 0;
  const observationNow = lastTs > 0 && now - lastTs > 3_600_000 ? lastTs : now;

  let equity = 0;
  let peak = 0;
  let drawdownStartedAt: number | null = null;
  let maxDurationMs = 0;
  let totalDurationMs = 0;
  let maxDepth = 0;
  let episodes = 0;

  for (const row of ordered) {
    const closedAt = timestampMs(row.t);
    equity += rowEquityPnl(row, costPct);
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
    drawdownStartedAt === null ? 0 : Math.max(0, observationNow - drawdownStartedAt);
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

/** CTS return PF: 1.00 = neutral after cost, 1.10 = +1× PositionCost net.
 * PositionCost is always a percent, including measured costs below 0.02%. */
export function costAsFrac(costPct = POSITION_COST_PCT_DEFAULT): number {
  return Math.max(0, finite(costPct, POSITION_COST_PCT_DEFAULT)) / 100;
}

export function netPnlPct(pnlPctFraction: number, costPct = POSITION_COST_PCT_DEFAULT): number {
  return finite(pnlPctFraction) - costAsFrac(costPct);
}

export function signedResultR(pnlPctFraction: number, costPct = POSITION_COST_PCT_DEFAULT, referenceCostPct = POSITION_COST_PCT_DEFAULT): number {
  const cost = Math.max(0, finite(costPct, POSITION_COST_PCT_DEFAULT));
  // A free fill keeps a finite reference unit while subtracting its actual zero cost.
  return netPnlPct(pnlPctFraction, cost) * 100 / (cost || Math.max(0, finite(referenceCostPct)) || POSITION_COST_PCT_DEFAULT);
}

export function ratioFromR(signedR: number): number {
  return 1 + finite(signedR) * COST_RATIO_SCALE;
}

/** Shared admission boundary: strict PF > 1.02 plus the configured floor. */
export function clearsReturnPf(ratio: number, minPf: number): boolean {
  return Number.isFinite(ratio) && ratio > 1.02 && ratio + 1e-9 >= minPf;
}

function rowValue(row: PositionCostRow, ...keys: (keyof PositionCostRow)[]): unknown {
  for (const key of keys) if (row[key] != null) return row[key];
  return undefined;
}

function normalizeCostPct(value: unknown, fallback = POSITION_COST_PCT_DEFAULT): number {
  let cost = Math.max(0, finite(value, fallback));
  if (cost > 2) cost /= 100;
  return round(cost <= 1 ? cost : fallback, 8);
}

function feeAmount(value: unknown): number {
  if (value && typeof value === "object") {
    const obj = value as Record<string, unknown>;
    for (const key of ["amount", "value", "qty", "quantity", "fee", "commission"]) {
      if (obj[key] != null) return Math.abs(finite(obj[key]));
    }
  }
  return Math.abs(finite(value));
}

export function rowNotional(row: PositionCostRow): number {
  const direct = finite(rowValue(row, "notional", "quoteQty", "quoteQuantity", "cumQuote", "cumQuoteQty"));
  if (direct > 1e-12) return direct;
  const qty = finite(rowValue(row, "qty", "executedQty", "filledQty", "cumQty", "quantity"));
  const entry = finite(rowValue(row, "entry", "avgPrice", "averagePrice", "price", "exit"));
  return Math.max(0, qty * entry);
}

const FEE_RATE_KEYS = ["fee_rate", "feeRate", "commissionRate", "makerFeeRate", "takerFeeRate"] as const;

function rowFee(row: PositionCostRow): number {
  const total = rowValue(row, "fee_total", "feeTotal", "totalFee", "totalCommission", "fee", "fees", "feeAmount", "commission", "commissionAmount", "transactionFee", "realizedFee");
  if (total != null) return feeAmount(total);
  return feeAmount(rowValue(row, "entry_fee", "entryFee", "openFee", "openCommission"))
    + feeAmount(rowValue(row, "exit_fee", "exitFee", "closeFee", "closeCommission"));
}

function rowHasMeasuredCost(row: PositionCostRow): boolean {
  const source = String(rowValue(row, "cost_source", "costSource") ?? "").toLowerCase();
  return source.includes("exchange")
    || (rowValue(row, "position_cost_pct", "positionCostPct", "cost_pct") != null && (source.includes("cost") || source.includes("live")))
    || (row.costPct != null && (source.includes("cost") || source.includes("exchange")))
    || rowFee(row) > 0 || rowValue(row, ...FEE_RATE_KEYS) != null;
}

/** Round-trip cost precedence mirrors position_cost.row_position_cost_pct. */
export function rowPositionCostPct(row: PositionCostRow, fallback = POSITION_COST_PCT_DEFAULT): number {
  const explicit = rowValue(row, "position_cost_pct", "positionCostPct", "cost_pct");
  if (explicit != null && finite(explicit, -1) >= 0) return normalizeCostPct(explicit);
  const source = String(rowValue(row, "cost_source", "costSource", "source") ?? "").toLowerCase();
  if (row.costPct != null && (source.includes("cost") || source.includes("exchange"))) return normalizeCostPct(row.costPct, fallback);
  const notional = rowNotional(row);
  if (notional <= 1e-12) return normalizeCostPct(fallback);
  const entryFee = feeAmount(rowValue(row, "entry_fee", "entryFee", "openFee", "openCommission"));
  const exitFee = feeAmount(rowValue(row, "exit_fee", "exitFee", "closeFee", "closeCommission"));
  const total = rowValue(row, "fee_total", "feeTotal", "totalFee", "totalCommission");
  const measuredTotal = total != null && (feeAmount(total) > 0 || ["exchange", "live", "cost"].some((key) => source.includes(key)));
  if (measuredTotal || (entryFee > 0 && exitFee > 0)) {
    return normalizeCostPct((total != null ? feeAmount(total) : entryFee + exitFee) / notional * 100, fallback);
  }
  const fee = rowFee(row);
  if (fee > 0) {
    const scope = String(rowValue(row, "fee_scope", "feeScope", "fee_type", "feeType") ?? "").toLowerCase();
    const legs = ["roundtrip", "position", "total", "both"].includes(scope) ? 1 : 2;
    return normalizeCostPct(fee / notional * 100 * legs, fallback);
  }
  const rate = rowValue(row, ...FEE_RATE_KEYS);
  if (rate != null && finite(rate, -1) >= 0) {
    const parsed = finite(rate);
    return normalizeCostPct((parsed <= 0.02 ? parsed * 100 : parsed) * 2);
  }
  return normalizeCostPct(fallback);
}

/** Missing gross return can be reconstructed from already net cash PnL. */
export function rowGrossReturn(row: PfRow, costPct = POSITION_COST_PCT_DEFAULT): number {
  if (row.pnl_pct != null) return finite(row.pnl_pct);
  const notional = rowNotional(row);
  return notional > 1e-12 ? finite(row.pnl) / notional + costAsFrac(costPct) : 0;
}

export function lastNCostPf(
  rows: PfRow[],
  n = 15,
  costPct = POSITION_COST_PCT_DEFAULT,
  minPf = 1.1,
  requiredSamples = n,
): CostPfMetric {
  const window = [...rows]
    .sort((a, b) => timestampMs(b.t) - timestampMs(a.t))
    .slice(0, Math.max(1, n));
  return costPfFromWindow(window, n, costPct, minPf, requiredSamples);
}

function costPfFromWindow(window: PfRow[], n: number, costPct: number, minPf: number, requiredSamples: number): CostPfMetric {
  const rs: number[] = [];
  const nets: number[] = [];
  const grosses: number[] = [];
  let weightedCost = 0;
  let weightedNotional = 0;
  let references = 0;
  let zeroCostSamples = 0;
  let costSamples = 0;
  let costTotal = 0;
  let unavailableReturnSamples = 0;
  let gp = 0;
  let gl = 0;
  for (const row of window) {
    const knownReturn = row.pnl_pct != null
      ? Number.isFinite(Number(row.pnl_pct))
      : rowNotional(row) > 1e-12 && row.pnl != null && Number.isFinite(Number(row.pnl));
    if (!knownReturn) {
      unavailableReturnSamples += 1;
      continue;
    }
    const actualCost = rowPositionCostPct(row, costPct);
    costTotal += actualCost;
    const gross = rowGrossReturn(row, actualCost);
    const reference = actualCost || normalizeCostPct(costPct) || POSITION_COST_PCT_DEFAULT;
    rs.push(signedResultR(gross, actualCost, reference));
    const net = netPnlPct(gross, actualCost);
    nets.push(net);
    grosses.push(gross);
    references += reference;
    if (actualCost === 0) zeroCostSamples += 1;
    if (rowHasMeasuredCost(row)) {
      costSamples += 1;
      const notional = rowNotional(row);
      if (notional > 1e-12) {
        weightedCost += actualCost * notional;
        weightedNotional += notional;
      }
    }
    if (net > 0) gp += net;
    else if (net < 0) gl += Math.abs(net);
  }
  const count = rs.length;
  const avgR = count ? rs.reduce((a, b) => a + b, 0) / count : 0;
  const ratio = count ? ratioFromR(avgR) : 1;
  const classic = gl > 0 ? gp / gl : gp > 0 ? 99 : 0;
  const netAvg = count ? nets.reduce((a, b) => a + b, 0) / count : 0;
  const required = Math.max(1, Math.trunc(finite(requiredSamples, n)));
  return {
    n,
    count,
    avgR: round(avgR),
    ratio: round(ratio),
    classicPf: round(classic),
    costPct: weightedNotional > 1e-12 ? round(weightedCost / weightedNotional, 8) : count ? round(costTotal / count, 8) : normalizeCostPct(costPct),
    netPct: round(netAvg * 100),
    grossPct: count ? round(grosses.reduce((a, b) => a + b, 0) / count * 100) : 0,
    netAvg: round(netAvg, 6),
    costSubtracted: true,
    minPf,
    requiredSamples: required,
    pfReferenceCostPct: count ? round(references / count, 8) : normalizeCostPct(costPct) || POSITION_COST_PCT_DEFAULT,
    zeroCostSamples,
    costSamples,
    unavailableReturnSamples,
    costSource: costSamples === count && count ? "live-exchange" : costSamples ? "mixed-cost" : "manual-fallback",
    pass: count >= required && clearsReturnPf(round(ratio), minPf),
  };
}

export function buildOverview(rows: PfRow[], now = Date.now(), costPct = POSITION_COST_PCT_DEFAULT, policy: CostPfPolicy = {}) {
  const newestFirst = [...rows].sort((a, b) => timestampMs(b.t) - timestampMs(a.t));
  const withinHours = (hours: number) =>
    newestFirst.filter((row) => {
      const ts = timestampMs(row.t);
      return ts >= now - hours * 60 * 60 * 1000 && ts <= now;
    });
  const cost = Number.isFinite(costPct) && costPct >= 0 ? costPct : POSITION_COST_PCT_DEFAULT;
  const windowN = Math.max(1, Math.trunc(policy.n ?? BASE_EVAL_DEFAULT));
  const minPf = policy.minPf ?? 1.1;
  // Reuse the one sorted tape across all windows; independent windows need no extra sorts.
  const positionMetric = (n: number, required = n) => costPfFromWindow(newestFirst.slice(0, n), n, cost, minPf, required);
  const timeMetric = (hours: number) => {
    const window = withinHours(hours);
    return costPfFromWindow(window, window.length || 1, cost, minPf, policy.requiredSamples ?? windowN);
  };
  return {
    generatedAt: now,
    last4: positionMetric(4),
    costPf: positionMetric(windowN, policy.requiredSamples ?? windowN),
    positionWindows: {
      "12": positionMetric(12),
      "15": positionMetric(15),
      "25": positionMetric(25),
      "30": positionMetric(30),
      "50": positionMetric(50),
      "75": positionMetric(75),
      "150": positionMetric(150),
    } as const,
    timeWindows: {
      "4h": timeMetric(4),
      "12h": timeMetric(12),
      "48h": timeMetric(48),
    } as const,
    drawdown3d: calculateDrawdownTime(newestFirst, now, 3, cost),
    drawdownAll: calculateDrawdownTime(newestFirst, now, 3650, cost),
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

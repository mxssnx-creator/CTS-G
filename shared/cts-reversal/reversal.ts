/** Direction on a completed signal candle. All baselines end before that candle. */
export type ReversalDirection = -1 | 0 | 1;
export type ReversalTactic = "reclaim" | "structure" | "impulse" | "pullback" | "rsi" | "psar" | "confluence";

export const REVERSAL_TACTICS = [
  { id: "reclaim", label: "Failed break reclaim" },
  { id: "structure", label: "Swing structure break" },
  { id: "impulse", label: "Relative volume impulse" },
  { id: "pullback", label: "Reversal into trend" },
  { id: "rsi", label: "RSI recovery" },
  { id: "psar", label: "PSAR flip" },
  { id: "confluence", label: "Trend and RSI swing break" },
] as const satisfies readonly { id: ReversalTactic; label: string }[];

export const REVERSAL_PERIODS = [6, 10, 16] as const;
export const REVERSAL_SOURCES = REVERSAL_TACTICS.flatMap(({ id: tactic, label }) =>
  REVERSAL_PERIODS.map(period => ({
    id: `trend-reversal-${tactic}-${period}`,
    kind: "trend-reversal" as const,
    label: `Trend-Reversal · ${label} · ${period} bars`,
    tactic,
    period,
  })),
);
export const REVERSAL_BY_ID = new Map(REVERSAL_SOURCES.map(source => [source.id, source]));

export interface ReversalBars {
  t: ArrayLike<number>;
  o: ArrayLike<number>;
  h: ArrayLike<number>;
  l: ArrayLike<number>;
  c: ArrayLike<number>;
  v: ArrayLike<number>;
}

function validBar(b: ReversalBars, i: number): boolean {
  const {t, o, h, l, c, v} = b;
  return Number.isFinite(t[i]) && Number.isFinite(h[i]) && Number.isFinite(o[i]) &&
    Number.isFinite(c[i]) && Number.isFinite(v[i]) && v[i] >= 0 &&
    l[i] > 0 && l[i] <= Math.min(o[i], c[i]) && h[i] >= Math.max(o[i], c[i]);
}

function psarTurn(b: ReversalBars, start: number, end: number): ReversalDirection {
  let side: 1 | -1 = b.c[start + 1] >= b.c[start] ? 1 : -1;
  let extreme = side === 1 ? Math.max(b.h[start], b.h[start + 1]) : Math.min(b.l[start], b.l[start + 1]);
  let stop = side === 1 ? Math.min(b.l[start], b.l[start + 1]) : Math.max(b.h[start], b.h[start + 1]);
  let acceleration = .02;
  let flip: ReversalDirection = 0;
  for (let j = start + 2; j <= end; j++) {
    stop += acceleration * (extreme - stop);
    stop = side === 1 ? Math.min(stop, b.l[j - 1], b.l[j - 2]) : Math.max(stop, b.h[j - 1], b.h[j - 2]);
    const next = side === 1 ? b.h[j] : b.l[j];
    flip = 0;
    if (side === 1 ? b.l[j] < stop : b.h[j] > stop) {
      side = side === 1 ? -1 : 1;
      stop = extreme;
      extreme = side === 1 ? b.h[j] : b.l[j];
      acceleration = .02;
      flip = side;
    } else if (side * (next - extreme) > 0) {
      extreme = next;
      acceleration = Math.min(.2, acceleration + .02);
    }
  }
  return flip;
}

/** A finite, bounded window gives the same answer on full, replayed and restarted series. */
export function trendReversalAt(
  b: ReversalBars, i: number, period: number, tactic: ReversalTactic, barMs: number,
): ReversalDirection {
  if (!Number.isInteger(period) || period < 4 || period > 60 || !(barMs > 0)) return 0;
  const related = tactic === "pullback" || tactic === "confluence";
  const recent = i - period - 1;
  const start = related ? recent - period : recent;
  if (start < 0 || i >= b.t.length || !validBar(b, start)) return 0;

  let atr = 0, volume = 0, up = 0, down = 0;
  let upper = -Infinity, lower = Infinity, swingHigh = -Infinity, swingLow = Infinity;
  const swing = Math.max(2, Math.round(period / 5));
  for (let j = start + 1; j <= i; j++) {
    if (!validBar(b, j) || b.t[j] - b.t[j - 1] !== barMs) return 0;
    if (j <= recent || j === i) continue;
    const prior = b.c[j - 1];
    const delta = b.c[j] - prior;
    up += Math.max(0, delta);
    down += Math.max(0, -delta);
    atr += Math.max(b.h[j] - b.l[j], Math.abs(b.h[j] - prior), Math.abs(b.l[j] - prior));
    volume += b.v[j];
    upper = Math.max(upper, b.h[j]);
    lower = Math.min(lower, b.l[j]);
    if (j >= i - swing) {
      swingHigh = Math.max(swingHigh, b.h[j]);
      swingLow = Math.min(swingLow, b.l[j]);
    }
  }
  atr /= period;
  if (!(atr > 0) || !(up + down > 0)) return 0;
  const oldMove = b.c[i - 1] - b.c[recent];
  const side = oldMove < 0 ? 1 : -1;
  const body = side * (b.c[i] - b.o[i]);
  const change = side * (b.c[i] - b.c[i - 1]);
  const candleRange = b.h[i] - b.l[i];
  if (!(body > 0 && change > 0 && candleRange > 0) || change > 3.25 * atr ||
      Math.abs(oldMove) < 1.1 * atr || Math.abs(oldMove) < .22 * (up + down)) return 0;
  const directionalClose = side > 0 ? b.c[i] - b.l[i] : b.h[i] - b.c[i];
  if (directionalClose < .58 * candleRange) return 0;
  const swingBreak = side > 0 ? b.c[i] > swingHigh : b.c[i] < swingLow;
  const trendAligned = related && side * (b.c[recent] - b.c[start]) >= 1.35 * atr;

  switch (tactic) {
    case "reclaim":
      if (side > 0 ? b.l[i] < lower && b.c[i] >= lower + .10 * atr
          : b.h[i] > upper && b.c[i] <= upper - .10 * atr) return side;
      break;
    case "structure":
      if (swingBreak && body >= .22 * atr) return side;
      break;
    case "impulse":
      if (change >= .85 * atr && body >= .45 * atr && volume > 0 && b.v[i] >= 1.20 * volume / period)
        return side;
      break;
    case "pullback":
      if (trendAligned && swingBreak && body >= .18 * atr) return side;
      break;
    case "rsi":
    case "confluence": {
      // Use a fixed-window RSI. The prior and current windows share exactly period-1 returns.
      const oldRsi = 100 * up / (up + down);
      const drop = b.c[recent + 1] - b.c[recent];
      const gain = up - Math.max(0, drop) + Math.max(0, b.c[i] - b.c[i - 1]);
      const loss = down - Math.max(0, -drop) + Math.max(0, b.c[i - 1] - b.c[i]);
      const rsi = gain + loss > 0 ? 100 * gain / (gain + loss) : 50;
      const recovered = side > 0 ? oldRsi <= 40 && rsi > 40 : oldRsi >= 60 && rsi < 60;
      if (recovered && (tactic === "rsi" || (trendAligned && swingBreak))) return side;
      break;
    }
    case "psar":
      if (psarTurn(b, start, i) === side) return side;
      break;
  }
  return 0;
}

export function trendReversalSeries(
  b: ReversalBars, period: number, tactic: ReversalTactic, barMs: number,
): Int8Array {
  const out = new Int8Array(b.t.length);
  for (let i = period + 1; i < out.length; i++) out[i] = trendReversalAt(b, i, period, tactic, barMs);
  return out;
}

/**
 * Wide-trail configs that cleared the desk gate on the x02 paper book
 * through 2026-09-30. Lane scale removed. Signals excluded.
 * Profit factor ≥ 1.1 and at least 12 closes. Added on top of the full catalog.
 */
export interface ProvenWideTrail {
  pair: string;
  pf: number;
  n: number;
  netR: number;
}

export const PROVEN_WIDE_TRAIL: readonly ProvenWideTrail[] = [
  { pair: "sweep|dir-vwap-240@m15c", pf: 3.11, n: 23, netR: 2.02 },
  { pair: "follow|rsi-mom-21-20@m1", pf: 2.36, n: 14, netR: 0.17 },
  { pair: "sweep|ema-50-100@m15c", pf: 2.34, n: 27, netR: 1.34 },
  { pair: "follow|rsi-mom-21-25@m1", pf: 2.3, n: 211, netR: 3.87 },
  { pair: "sweep|ema-50-100@m30", pf: 2.28, n: 13, netR: 0.95 },
  { pair: "follow|r-vol-regime-m@m15", pf: 2.11, n: 18, netR: 0.71 },
  { pair: "sweep|trend-ema-50-200@m15c", pf: 2.1, n: 25, netR: 1.89 },
  { pair: "sweep|hma-55@m30", pf: 1.76, n: 26, netR: 0.78 },
  { pair: "follow|move-swing-32@m30", pf: 1.68, n: 18, netR: 0.64 },
  { pair: "follow|rsi-mom-14-20@m1", pf: 1.64, n: 268, netR: 2.92 },
  { pair: "sweep|dir-emax-12-26@m30", pf: 1.49, n: 54, netR: 1.38 },
  { pair: "sweep|ema-slope@m30", pf: 1.41, n: 38, netR: 0.8 },
  { pair: "revert|dir-emax-5-13@m15", pf: 1.37, n: 94, netR: 1.98 },
  { pair: "follow|r-connors-m@m15", pf: 1.33, n: 37, netR: 0.75 },
  { pair: "sweep|macd-zero@m30", pf: 1.26, n: 48, netR: 0.73 },
  { pair: "follow|ichi-cloud-20@m5c", pf: 1.26, n: 14, netR: 0.08 },
  { pair: "sweep|ichi-cloud-9@m30", pf: 1.18, n: 34, netR: 0.28 },
  { pair: "sweep|dir-vwap-240@m30", pf: 1.18, n: 19, netR: 0.35 },
  { pair: "sweep|ema-9-21@m30", pf: 1.15, n: 111, netR: 1.05 },
  { pair: "sweep|dir-emax@m30", pf: 1.15, n: 111, netR: 1.05 },
  { pair: "sweep|dir-emax-5-13@m30", pf: 1.14, n: 60, netR: 0.4 },
  { pair: "sweep|trend-ema-20-50@m30", pf: 1.14, n: 40, netR: 0.37 },
  { pair: "sweep|ichi-tk-9@m30", pf: 1.14, n: 55, netR: 0.45 },
  { pair: "follow|ema-stoch@m15", pf: 1.14, n: 45, netR: 0.27 },
];

export const PROVEN_WIDE_TRAIL_PAIRS: readonly string[] = PROVEN_WIDE_TRAIL.map((p) => p.pair);

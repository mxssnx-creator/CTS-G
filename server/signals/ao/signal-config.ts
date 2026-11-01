// Signals processing settings and sources (pure, no engine imports: shared by the engine, config and the UI).
// The engine side (combos, configs, active ranking, guard) is in signals.ts.
import { INDICATION_KINDS, type IndicationKind } from "./domain/types.ts";
import { SIGNAL_HISTORY_H, SIGNAL_VALID_LAST_N } from "./signal-policy.ts";
import { volumeRatio } from "./volume-ratio.ts";

/**
 * Proven classic signals, each in a short and a medium parameter range (registry ids of the computations they
 * reuse under their own "sig-…" ids).
 */
export const SIGNAL_SOURCES: ReadonlyArray<{
  name: string;
  label: string;
  short: string;
  medium: string;
}> = [
  ...["apz-reclaim", "frama-pullback", "st-pullback"].map((name) => ({
    name: `r-${name}`, label: `${name} (research)`, short: `r-${name}`, medium: `r-${name}-m`,
  })),
  {
    name: "ema-cross",
    label: "EMA cross",
    short: "ema-9-21",
    medium: "ema-21-55",
  },
  {
    name: "macd-cross",
    label: "MACD signal cross",
    short: "macd-cross-8-21-5",
    medium: "macd-cross",
  },
  {
    name: "rsi-reversal",
    label: "RSI reversal",
    short: "rsi-7-15-85",
    medium: "rsi-14-25-75",
  },
  {
    name: "bollinger",
    label: "Bollinger bounce",
    short: "bb-bounce",
    medium: "bb-bounce-20-2.5",
  },
  {
    name: "supertrend",
    label: "Supertrend flip",
    short: "trend-st-7-2",
    medium: "trend-st-14-4",
  },
  {
    name: "donchian",
    label: "Donchian breakout",
    short: "break-don10",
    medium: "break-don20",
  },
  {
    name: "sar",
    label: "Parabolic SAR flip",
    short: "sar-fast",
    medium: "sar-std",
  },
  {
    name: "ichimoku",
    label: "Ichimoku TK cross",
    short: "ichi-tk-9",
    medium: "ichi-tk-20",
  },
  {
    name: "stoch-rsi",
    label: "Stoch RSI",
    short: "srsi-14-20",
    medium: "srsi-14-10",
  },
  {
    name: "keltner",
    label: "Keltner breakout",
    short: "kelt-20-1.5",
    medium: "kelt-20-2",
  },
  {
    name: "adx",
    label: "ADX / DMI",
    short: "trend-adx-20",
    medium: "trend-adx-30",
  },
  {
    name: "vwap",
    label: "VWAP reclaim",
    short: "dir-vwap-30",
    medium: "dir-vwap-120",
  },
  { name: "hma", label: "Hull MA", short: "hma-16", medium: "hma-55" },
  { name: "cci", label: "CCI", short: "cci-14-100", medium: "cci-20-200" },
  {
    name: "squeeze",
    label: "TTM squeeze",
    short: "squeeze-20",
    medium: "squeeze-30",
  },
  { name: "aroon", label: "Aroon", short: "aroon-14", medium: "aroon-25" },
  {
    name: "williams-r",
    label: "Williams %R",
    short: "willr-14-90",
    medium: "willr-28-80",
  },
  {
    name: "mfi",
    label: "Money Flow Index",
    short: "mfi-14-10",
    medium: "mfi-14-20",
  },
  {
    name: "obv",
    label: "On-Balance Volume",
    short: "obv-20",
    medium: "obv-50",
  },
  {
    name: "cmf",
    label: "Chaikin Money Flow",
    short: "cmf-20-0.1",
    medium: "cmf-20-0.05",
  },
  { name: "trix", label: "TRIX", short: "trix-9", medium: "trix-15" },
  {
    name: "kama",
    label: "Kaufman adaptive MA",
    short: "kama-10",
    medium: "kama-20",
  },
  { name: "heikin-ashi", label: "Heikin-Ashi", short: "ha-1", medium: "ha-3" },
  {
    name: "zscore",
    label: "Z-score reversion",
    short: "z-20-2",
    medium: "z-50-2.5",
  },
  {
    name: "ema-slope",
    label: "EMA slope",
    short: "ema-slope-20",
    medium: "ema-slope-100",
  },
  {
    name: "macd-hist",
    label: "MACD histogram",
    short: "macd-hist-8-21-5",
    medium: "macd-hist",
  },
  {
    name: "volume-break",
    label: "Volume breakout",
    short: "break-vol-2",
    medium: "break-vol",
  },
  {
    name: "ichi-cloud",
    label: "Ichimoku cloud",
    short: "ichi-cloud-9",
    medium: "ichi-cloud-20",
  },
  {
    name: "rsi-momentum",
    label: "RSI momentum",
    short: "rsi-mom-14-20",
    medium: "rsi-mom-21-20",
  },
  // registry computations added as sources (the stability gate decides at every step which of them trade)
  {
    name: "ema-trend",
    label: "EMA trend",
    short: "trend-ema-12-26",
    medium: "trend-ema-20-50",
  },
  {
    name: "atr-break",
    label: "ATR breakout",
    short: "break-atr-0.9",
    medium: "break-atr-1.5",
  },
  {
    name: "act-burst",
    label: "Activity burst",
    short: "act-burst-1.5",
    medium: "act-burst-2.5",
  },
  {
    name: "thrust",
    label: "Directional thrust",
    short: "dir-thrust-4",
    medium: "dir-thrust",
  },
  {
    name: "impulse",
    label: "Impulse move",
    short: "move-impulse-4-1.2",
    medium: "move-impulse-10-2",
  },
  {
    name: "swing",
    label: "Swing move",
    short: "move-swing-16",
    medium: "move-swing-32",
  },
  {
    name: "rsi-mid",
    label: "RSI mid cross",
    short: "rsi-mid-52-48",
    medium: "rsi-mid-60-40",
  },
  {
    name: "bb-walk",
    label: "Bollinger walk",
    short: "bb-walk",
    medium: "bb-walk-50",
  },
  {
    name: "ema-pullback",
    label: "EMA pullback",
    short: "ema-pullback",
    medium: "ema-pullback-50",
  },
  {
    name: "ema-cross-fast",
    label: "EMA cross fast",
    short: "dir-emax-5-13",
    medium: "dir-emax-12-26",
  },
  {
    name: "reclaim",
    label: "Level reclaim",
    short: "dir-reclaim",
    medium: "dir-reclaim-50",
  },
  {
    name: "act-hf",
    label: "High-frequency activity",
    short: "act-hf-5",
    medium: "act-hf-8",
  },
  {
    name: "macd-slow",
    label: "MACD cross slow",
    short: "macd-cross-5-35-5",
    medium: "macd-cross-19-39-9",
  },
  {
    name: "st-slow",
    label: "Supertrend slow",
    short: "trend-st",
    medium: "trend-st-21-5",
  },
  // Stable-02 desk entry signals ("s2-…", src/core/indications/stable02.ts): short = the desk's own parameters,
  // medium = the slower alternate
  {
    name: "s2-ema-cross",
    label: "S2 EMA cross pulse",
    short: "s2-ema-cross",
    medium: "s2-ema-cross-m",
  },
  {
    name: "s2-rsi-revert",
    label: "S2 RSI mean revert",
    short: "s2-rsi-revert",
    medium: "s2-rsi-revert-m",
  },
  {
    name: "s2-st-trail",
    label: "S2 Supertrend trail",
    short: "s2-st-trail",
    medium: "s2-st-trail-m",
  },
  {
    name: "s2-bb-bounce",
    label: "S2 Bollinger bounce",
    short: "s2-bb-bounce",
    medium: "s2-bb-bounce-m",
  },
  {
    name: "s2-vwap-axis",
    label: "S2 VWAP axis",
    short: "s2-vwap-axis",
    medium: "s2-vwap-axis-m",
  },
  {
    name: "s2-vol-break",
    label: "S2 Volume breakout",
    short: "s2-vol-break",
    medium: "s2-vol-break-m",
  },
  {
    name: "s2-adx-gate",
    label: "S2 ADX trend gate",
    short: "s2-adx-gate",
    medium: "s2-adx-gate-m",
  },
  {
    name: "s2-stoch-swing",
    label: "S2 Stoch swing",
    short: "s2-stoch-swing",
    medium: "s2-stoch-swing-m",
  },
  {
    name: "s2-confluence",
    label: "S2 Confluence 3 of 5",
    short: "s2-confluence",
    medium: "s2-confluence-m",
  },
  {
    name: "s2-range-break",
    label: "S2 Range break",
    short: "s2-range-break",
    medium: "s2-range-break-m",
  },
  {
    name: "s2-atr-break",
    label: "S2 ATR break",
    short: "s2-atr-break",
    medium: "s2-atr-break-m",
  },
  {
    name: "s2-active-hf",
    label: "S2 Active high-freq",
    short: "s2-active-hf",
    medium: "s2-active-hf-m",
  },
  {
    name: "s2-range-shift",
    label: "S2 Range shift",
    short: "s2-range-shift",
    medium: "s2-range-shift-m",
  },
  {
    name: "s2-block-stack",
    label: "S2 Block stack",
    short: "s2-block-stack",
    medium: "s2-block-stack-m",
  },
  {
    name: "s2-block-scale",
    label: "S2 Block scale",
    short: "s2-block-scale",
    medium: "s2-block-scale-m",
  },
  // research signals (literature on short-term crypto entries; src/core/indications/research.ts)
  {
    name: "r-squeeze",
    label: "R Squeeze release",
    short: "r-squeeze",
    medium: "r-squeeze-m",
  },
  {
    name: "r-donch-vol",
    label: "R Donchian + volume + CLV",
    short: "r-donch-vol",
    medium: "r-donch-vol-m",
  },
  {
    name: "r-sweep",
    label: "R Stop-run reversal",
    short: "r-sweep",
    medium: "r-sweep-m",
  },
  {
    name: "r-capit",
    label: "R Forced-flow fade",
    short: "r-capit",
    medium: "r-capit-m",
  },
  {
    name: "r-vwap-reclaim",
    label: "R VWAP reclaim",
    short: "r-vwap-reclaim",
    medium: "r-vwap-reclaim-m",
  },
  {
    name: "r-cvd-div",
    label: "R Volume-delta divergence",
    short: "r-cvd-div",
    medium: "r-cvd-div-m",
  },
  {
    name: "r-rsi2",
    label: "R RSI(2) reversion + trend",
    short: "r-rsi2",
    medium: "r-rsi2-m",
  },
  {
    name: "r-bb-adx",
    label: "R Bollinger extreme, ADX low",
    short: "r-bb-adx",
    medium: "r-bb-adx-m",
  },
  {
    name: "r-bbw-expand",
    label: "R Bandwidth expansion",
    short: "r-bbw-expand",
    medium: "r-bbw-expand-m",
  },
  {
    name: "r-session-trend",
    label: "R Session trend",
    short: "r-session-trend",
    medium: "r-session-trend-m",
  },
  {
    name: "r-zdist",
    label: "R Distance z-score fade",
    short: "r-zdist",
    medium: "r-zdist-m",
  },
  {
    name: "r-vol-regime",
    label: "R Regime breakout",
    short: "r-vol-regime",
    medium: "r-vol-regime-m",
  },
  {
    name: "r-clv-thrust",
    label: "R CLV thrust",
    short: "r-clv-thrust",
    medium: "r-clv-thrust-m",
  },
  {
    name: "r-nr-break",
    label: "R NR7 breakout",
    short: "r-nr-break",
    medium: "r-nr-break-m",
  },
  // research signals, second batch (src/core/indications/research2.ts)
  { name: "r-roofing", label: "R Roofing filter cross", short: "r-roofing", medium: "r-roofing-m" },
  { name: "r-fisher", label: "R Fisher turn", short: "r-fisher", medium: "r-fisher-m" },
  { name: "r-connors", label: "R Connors RSI fade", short: "r-connors", medium: "r-connors-m" },
  { name: "r-stc", label: "R Schaff trend cycle", short: "r-stc", medium: "r-stc-m" },
  { name: "r-vortex", label: "R Vortex cross", short: "r-vortex", medium: "r-vortex-m" },
  { name: "r-tsi", label: "R TSI cross", short: "r-tsi", medium: "r-tsi-m" },
  { name: "r-elder", label: "R Elder ray pullback", short: "r-elder", medium: "r-elder-m" },
  { name: "r-awesome", label: "R AO saucer", short: "r-awesome", medium: "r-awesome-m" },
  { name: "r-linreg", label: "R Regression channel", short: "r-linreg", medium: "r-linreg-m" },
  { name: "r-fractal", label: "R Fractal breakout", short: "r-fractal", medium: "r-fractal-m" },
  {
    name: "r-camarilla",
    label: "R Camarilla pivots",
    short: "r-camarilla",
    medium: "r-camarilla-m",
  },
  { name: "r-fakeout", label: "R Failed breakout", short: "r-fakeout", medium: "r-fakeout-m" },
  { name: "r-spring", label: "R Wyckoff spring", short: "r-spring", medium: "r-spring-m" },
  {
    name: "r-valuearea",
    label: "R Value-area re-entry",
    short: "r-valuearea",
    medium: "r-valuearea-m",
  },
  { name: "r-orb", label: "R Opening-range breakout", short: "r-orb", medium: "r-orb-m" },
  { name: "r-pdhl", label: "R Prev-day break and retest", short: "r-pdhl", medium: "r-pdhl-m" },
  { name: "r-bos", label: "R Break of structure", short: "r-bos", medium: "r-bos-m" },
  { name: "r-choch", label: "R Change of character", short: "r-choch", medium: "r-choch-m" },
  { name: "r-fvg", label: "R Fair value gap retest", short: "r-fvg", medium: "r-fvg-m" },
  { name: "r-inside", label: "R Inside-bar breakout", short: "r-inside", medium: "r-inside-m" },
  { name: "r-engulf", label: "R Engulfing at extreme", short: "r-engulf", medium: "r-engulf-m" },
  { name: "r-pin", label: "R Pin bar at extreme", short: "r-pin", medium: "r-pin-m" },
  { name: "r-star", label: "R Morning / evening star", short: "r-star", medium: "r-star-m" },
  { name: "r-streak", label: "R Exhaustion streak", short: "r-streak", medium: "r-streak-m" },
  // Frozen research candidates (15 Oct): existing computations retain their original parameters and identity.
  { name: "r-laguerre", label: "R Laguerre RSI turn", short: "r-laguerre", medium: "r-laguerre-m" },
  { name: "r-chand", label: "R ATR Chandelier channel flip", short: "r-chand", medium: "r-chand-m" },
  { name: "r-klinger", label: "R Klinger volume-force cross", short: "r-klinger", medium: "r-klinger-m" },
  { name: "r-rvi", label: "R Relative Vigor Index cross", short: "r-rvi", medium: "r-rvi-m" },
];

/** Registry id of a signal source in a range. */
export const signalId = (name: string, range: "short" | "medium") =>
  `sig-${name}-${range === "short" ? "s" : "m"}`;

/**
 * Acceptance by profit factor: a signal trades only while its group (full source indication, parameter range and
 * lane × symbol × direction × type: Normal / Trailing / …) had a profit factor of at least `minPf` over
 * the closed candidates of the last `hours` hours before the entry — every candidate of the signal tapes, active or
 * not, also those closed before the simulated run started (SignalAcceptIndex). The PF is over every close; the count
 * (`minTrades`) is over signal ENTRIES: the k configs of one entry (indication × symbol × direction × entry time)
 * count once, or one onset alone reached the minimum. A window with fewer than `minTrades`
 * closes is widened to twice its hours, and a group still under `minTrades` there counts as valid (no sample to judge
 * yet); otherwise it is judged on the wider window (`acceptOnWindow`, the rule every hour-window validation shares).
 * Causal (only results closed before the entry); the candidates keep being computed while a group is not accepted.
 */
export interface SignalAccept {
  enabled: boolean;
  minPf: number;
  hours: number;
  minTrades: number;
  /**
   * engine direction acceptance only: ranges ("mc" … "lg", "wide") whose direction group is judged per indication
   * (with its lane) instead of pooled over the range — one losing indication no longer blocks a winning one
   */
  perInd?: string[];
}

export interface SignalClusterSettings {
  enabled: boolean;
  /** look-back of closed signal results (minutes) */
  windowMin: number;
  /** at least this many losing closes in the window */
  minLosses: number;
  /** and at least this share of the window's closes losing */
  lossShare: number;
}

/** Independent majority-vote coordinators, one ID per input-range mix and domination threshold. */
export interface SignalCoordinationSettings {
  enabled: boolean;
  /** Minimum number of active, non-conflicting source votes on a bar. */
  minSources: number;
  /** Active pair × type × symbol × direction cap, applied separately from classic signals. */
  count: number;
  /** External sizing ratio, 0.1–5 in 0.1 steps; neutral default 1. Applied once after Block and relation volume. */
  volumeFactor: number;
  /** Exit ranges: Normal here maps to the existing General range in the grid. */
  ranges: { short: boolean; normal: boolean; long: boolean };
}

/**
 * Source stability gate: a source's signals pause while its own executed signal orders of the latest `days`
 * (24-hour buckets back from each entry) are unstable — at least `minTrades` of them and negative in sum, or
 * positive in fewer than `minShare` of the buckets. Causal (only orders closed before the entry); a source
 * without enough executed history trades.
 */
export interface SignalSourceGate {
  enabled: boolean;
  days: number;
  minShare: number;
  minTrades: number;
}

/**
 * ATR exits (the Stable-02 model): stop = sl × ATR(14) at entry, target = stop × tpRatio, optional trailing
 * (Stable-02 trail %), max hold in 15m-reference bars. Every sl × tpRatio cell runs without trail and once per
 * trail value.
 */
export interface SignalAtrExits {
  /** stop in ATR(14) multiples, 0.2–2 (Stable-02 SL_ATR) */
  sl: number[];
  /** target ÷ stop, 0.2–3 (Stable-02 TP_SL_RATIOS) */
  tpRatio: number[];
  /** trailing variants (Stable-02 TRAIL_PCTS, 0.4–2.4 %); empty = no trailing variants */
  trail: number[];
  /** max hold in 15m-reference bars (Stable-02 DEFAULT_MAX_HOLD_BARS 3); 0 = the signal hold (holdH) */
  holdBars: number;
}

export type SignalExitModel = "pct" | "atr" | "both";

/**
 * Historic trailing configs (signal-hist.ts): per signal, Trailing configs of its own from its historic entries — the
 * move each reached within the hold (MFE), at `quantiles`, becomes a target; each target trails at every share of
 * it and stops at every multiple of it. Causal (entries whose hold ended before the stage cut).
 */
export interface SignalHistTrail {
  enabled: boolean;
  /** quantiles of the historic MFE that become targets (0.2–0.95) */
  quantiles: number[];
  /** trailing distance as a share of the target (0.1–0.95) */
  trailOfTp: number[];
  /** stop as a multiple of the target (0.25–5) */
  slOfTp: number[];
  /** historic entries a signal needs before it gets its own configs (pooled over symbols and sides) */
  minN: number;
  /** hours of history before each window's start the historic entries come from (0 = the whole history) */
  lookbackH: number;
}

export interface SignalSettings {
  enabled: boolean;
  /** active config units (source indication × protect × type × symbol × direction); 0 = unlimited */
  count: number;
  /** sources switched on (by name); missing = on (sourcesMode "deny") or off (sourcesMode "allow") */
  sources: Record<string, boolean>;
  /**
   * how `sources` reads: "deny" (default) = a source runs unless it is set false (missing = on); "allow" = only the
   * sources set true run (a desk that lists the sources it wants runs exactly those)
   */
  sourcesMode?: "deny" | "allow";
  ranges: { short: boolean; medium: boolean };
  /** Per-signal-range execution multipliers; applied to the balance-based unit before the account caps. */
  rangeVolumeFactors: { short: number; medium: number };
  /** timeframe lanes signals run on (of the engine's lanes) */
  lanes: number[];
  normal: { tp: number[]; slOfTp: number[] };
  trailing: { tp: number[]; trailOfTp: number[]; slOfTp: number };
  /** Additional independent sets with a hard stop and peak trail, without any take-profit. 15m reference. */
  trailingOnly: { enabled: boolean; sl: number[]; trail: number[]; trailStep: number };
  /** Independent peak-gap multipliers for capped, ATR and no-TP trailing (absent = existing scalar). */
  trailRatios?: number[];
  /** Signal protection ceiling from entry, no more than 3%; legacy configs retain their own ceiling. */
  trailMax?: number;
  holdH: number;
  /** exit model of the signal configs: percent grid (Normal + Trailing), ATR grid, or both */
  exits: SignalExitModel;
  atr: SignalAtrExits;
  /** per-signal trailing configs from its own history (SignalHistTrail) */
  hist: SignalHistTrail;
  guard: { enabled: boolean; lastN: number };
  /** loss-cluster guard: pause signal executions while many signals just lost together (see SignalGuard) */
  cluster: SignalClusterSettings;
  coordination: SignalCoordinationSettings;
  /** Per-indication multiplier on standalone and combined lanes. Legacy storage key; neutral default 1×. */
  combinedVolumeFactors: Record<IndicationKind, number>;
  /** Base minimum per signal to be ranked: closed trades on that symbol */
  minTrades: number;
  /**
   * How the active signals are ranked: "drawdown" (default) = net ÷ max drawdown (recovery), only signals whose
   * 4-hour blocks were positive at least `minBlockShare` of the time; "lowdd" = the same filter, net ÷ drawdown²
   * and at least as much net as drawdown (prefers the smallest drawdowns); "net" = the former ranking by net then PF
   */
  rank: "drawdown" | "lowdd" | "net";
  minBlockShare: number;
  /**
   * automatic validation before a signal goes active: besides its whole Base history, its result over the most
   * recent 24 h of that history must be positive (a signal that stopped working is not started)
   */
  validate: boolean;
  /** hard floors of every signal config's stop / trail (fractions; minTrail defaults to 0.002 = 0.2 %) */
  minSl: number;
  minTrail: number;
  /** only stable sources trade (see SignalSourceGate) */
  sourceGate: SignalSourceGate;
  /**
   * hours of the latest results the validation judges (2–72): the recent sub-window a unit must be positive in (with
   * at least one close per config). The per-step ranking itself judges the last max(longH, preH) hours.
   */
  validateH: number;
  /** signal orders' own caps (they add to the engine's orders): open per symbol, open overall; 0 = no limit */
  perSymbol: number;
  /** strategy sets each signal runs besides its Normal / Trailing configs: DCA (+ DCA Active) and Axis */
  strategies: { dca: boolean; axis: boolean };
  /**
   * signals trade their own Normal / Trailing base: the engine's Normal switch and Block Active's skip do not hold
   * them back (Block still raises them; Axis / DCA sets run beside them). Off: signals follow the engine toggles.
   */
  ownBase: boolean;
  /** entry filters: trend direction (EMA over trendH hours, 0 = off) and a volatility floor (ATR ÷ price, 0 = off) */
  filter: { trendH: number; volFloor: number };
  /** only groups with a profit factor of at least minPf trade (full signal indication × symbol × direction × type) */
  accept: SignalAccept;
  /**
   * direction acceptance: a side (long / short) trades signals only while the run's signal candidates of that side
   * (the active units' candidates, fed as they close — not every tape), pooled over every source and symbol, had a PF
   * of at least minPf over the last `hours` hours (at least minTrades signal entries)
   */
  sideAccept: SignalAccept;
  /**
   * true: a signal pair needs its default-protect Base result to pass before any of its configs is computed;
   * false: every configured initialized signal pair computes all its configs, each qualified on its own history
   */
  baseGate?: boolean;
  maxOpen: number;
  /** Signal-simulation positions (symbol × direction, long and short apart); independent of the live exchange cap. */
  maxPositions: number;
}

/** Execution multipliers consumed by the simulation, paper book and live order sizing. */
export interface SignalVolumeFactors {
  /** Signals Coordination multiplier relative to a normal order. */
  coordination: number;
  /** Per-kind multipliers on standalone and combined lanes; applied once. */
  combined: Readonly<Partial<Record<IndicationKind, number>>>;
}

export function signalVolumeFactorsOf(settings: SignalSettings): SignalVolumeFactors {
  return {
    coordination: settings.coordination.volumeFactor,
    combined: settings.combinedVolumeFactors,
  };
}

/** Exact former factory deny-list; custom source selections never inherit its migration. */
export const PRE_EXPANDED_SIGNAL_SOURCES: Readonly<Record<string, boolean>> = {
  "r-apz-reclaim": false,
  "r-frama-pullback": false,
  "r-st-pullback": false,
  aroon: false,
  "ichi-cloud": false,
  "bb-walk": false,
  "r-squeeze": false,
  "r-donch-vol": false,
  "r-sweep": false,
  "r-capit": false,
  "r-vwap-reclaim": false,
  "r-cvd-div": false,
  "r-rsi2": false,
  "r-bb-adx": false,
  "r-bbw-expand": false,
  "r-zdist": false,
  "r-clv-thrust": false,
  "r-roofing": false,
  "r-fisher": false,
  "r-stc": false,
  "r-vortex": false,
  "r-tsi": false,
  "r-elder": false,
  "r-camarilla": false,
  "r-fakeout": false,
  "r-spring": false,
  "r-valuearea": false,
  "r-orb": false,
  "r-pdhl": false,
  "r-bos": false,
  "r-choch": false,
  "r-fvg": false,
  "r-engulf": false,
  "r-pin": false,
  "r-star": false,
  "r-streak": false,
  "r-laguerre": false,
  "r-chand": false,
  "r-klinger": false,
  "r-rvi": false,
};

export const DEFAULT_SIGNALS: SignalSettings = {
  enabled: true,
  // Every independently qualified config context may process; explicit nonzero counts retain ranked selection.
  count: 0,
  sourcesMode: "deny",
  // Expanded: calculate every registered source, then qualify each exact config/symbol/direction independently.
  sources: {},
  ranges: { short: true, medium: true },
  rangeVolumeFactors: { short: 1, medium: 1 },
  // All four independent Expanded lanes retain minute-based pricing and causal entry/exit calculations.
  lanes: [1, 5, 15, 30],
  // 7 targets × 6 stop ratios = 42 Normal configs; include 1 % / 1.5 % targets and tighter risk ratios.
  normal: { tp: [0.01, 0.015, 0.025, 0.03, 0.04, 0.05, 0.06], slOfTp: [0.5, 0.75, 1, 1.5, 2, 3] },
  // 7 targets × 5 trail widths = 35 capped Trailing configs, stops at 3 × target.
  trailing: {
    tp: [0.01, 0.015, 0.03, 0.04, 0.05, 0.06, 0.08],
    trailOfTp: [0.2, 0.3, 0.4, 0.6, 0.8],
    slOfTp: 3,
  },
  trailingOnly: {
    enabled: true,
    sl: [0.015, 0.03, 0.06],
    trail: [0.005, 0.01, 0.02],
    trailStep: 1,
  },
  trailRatios: [0.5, 1, 1.5],
  holdH: 48,
  trailMax: 0.03,
  // Expanded builds percent and ATR protection cells, with independent peak-gap ratios.
  exits: "both",
  // Stable-02 defaults: SL 0.7 × ATR (its default), 1.15 (SHORT_SL_ATR), 1.5 · TP 1 / 1.6 / 2.2 R (2.2 its default)
  // · trail 0.8 % (its default) · hold 3 × 15m bars (DEFAULT_MAX_HOLD_BARS)
  atr: {
    sl: [0.7, 1.15, 1.5],
    tpRatio: [1, 1.6, 2.2],
    trail: [0.8],
    holdBars: 3,
  },
  // operator, 13 Oct: signals run multiple trailing configs from their own historic calcs — the median and upper
  // quartile of the move their entries reached as targets, trailed at ¼ / ½ / ¾ of it, stops 1× / 2× (up to 12 configs
  // per signal beside its 77 percent ones; each validated on its own)
  hist: {
    enabled: true,
    quantiles: [0.5, 0.75],
    trailOfTp: [0.25, 0.5, 0.75],
    slOfTp: [1, 2],
    minN: 30,
    lookbackH: SIGNAL_HISTORY_H,
  },
  guard: { enabled: true, lastN: SIGNAL_VALID_LAST_N },
  cluster: { enabled: true, windowMin: 60, minLosses: 8, lossShare: 0.6 },
  coordination: {
    enabled: true,
    minSources: 3,
    count: 0,
    volumeFactor: 1,
    ranges: { short: true, normal: true, long: true },
  },
  combinedVolumeFactors: Object.fromEntries(INDICATION_KINDS.map((kind) => [kind, 1])) as Record<
    IndicationKind,
    number
  >,
  // minTrades counts two things, and each is named where it is used: the activation (signals.ts activeSignals) takes the
  // own config trades of a symbol and side; the acceptance groups (acceptOnWindow) count the signal ENTRIES of their window
  minTrades: 3,
  // causal validation (8 days, with signal confirmation): lowdd PF 1.49 dd 490 · drawdown 1.49 / 521 · net 1.17 / 2565
  rank: "lowdd",
  // 4-day validation: halves drawdown, PF up on 3 of 4 days (docs/signals-validation.md)
  minBlockShare: 0.6,
  validate: true,
  validateH: 24,
  minSl: 0.005,
  minTrail: 0.002,
  // off: pausing a source after its executed orders lost cost net at every tested setting (continuous 8 days,
  // 43 sources: no gate PF 1.53 net 3470 · best gate 2 d / 67 % PF 1.50 net 2671; docs/signals-validation.md)
  sourceGate: { enabled: false, days: 2, minShare: 0.5, minTrades: 5 },
  // Orders (every order and partial) are unlimited. Signal simulation permits 250 open positions per direction,
  // separately from live exchange admission (100 total slots across all strategies).
  perSymbol: 0,
  maxOpen: 0,
  maxPositions: 250,
  // off: signals run Normal + Trailing; DCA / Axis sets per signal are selectable (not validated as better)
  strategies: { dca: false, axis: false },
  // signals have their own switch: the engine's Normal off / Block Active skip left them ~16 closes in a day on x01
  ownBase: true,
  // volatility floor 0.3 %: the expected move must be worth the 0.2 % round trip (worst drawdown 523 vs 660)
  filter: { trendH: 0, volFloor: 0.003 },
  // the historical source-pooled acceptance was the edge (off: unit PF 0.4–0.7); thresholds barely mattered (pooled closes are nearly all wins or
  // all losses: PF 1.05 / 1.5 / 2 / 3 gave identical results on 3 Oct / 2 Oct / 23 Sep, PR #65); the operator's
  // setting: PF 1.3 over 48 h. Current qualification uses each exact config's own record; historical pooled evidence
  // does not certify this independent-config correction. Sparse windows retain acceptOnWindow semantics.
  accept: { enabled: true, minPf: 1.3, hours: 48, minTrades: 6 },
  // on (a positive coordination, docs/positive-coordinations.md; x01 runs it): the pooled side record split cleanly
  // (48 h, 5 Oct: long PF 3–44, short PF 0.1–0.3 per 12 h); the operator's signal evaluation: PF 1.3, as the acceptance
  sideAccept: { enabled: true, minPf: 1.3, hours: 24, minTrades: 20 },
  // signals judged on their own exits: the Base gate at the engine's default exit (TP 2.6 %, SL 3.9 %, 8 h) passed only
  // 6–11 of ~380 signal pairs (3 Oct: 51 orders at PF 0.44; 2 Oct: none)
  baseGate: false,
};

/** active signal units: 0 = no cap (every validated unit), then 10 … 200 in tens and up to 10,000 */
export const SIGNAL_COUNT_CHOICES = [
  0,
  ...Array.from({ length: 20 }, (_, i) => (i + 1) * 10),
  300,
  400,
  500,
  750,
  1000,
  1500,
  2000,
  3000,
  5000,
  7500,
  10_000,
];

export function signalSettings(s?: Partial<SignalSettings> | null): SignalSettings {
  const out: SignalSettings = {
    ...DEFAULT_SIGNALS,
    ...(s ?? {}),
    ranges: { ...DEFAULT_SIGNALS.ranges, ...(s?.ranges ?? {}) },
    rangeVolumeFactors: { ...DEFAULT_SIGNALS.rangeVolumeFactors, ...(s?.rangeVolumeFactors ?? {}) },
    normal: { ...DEFAULT_SIGNALS.normal, ...(s?.normal ?? {}) },
    trailing: { ...DEFAULT_SIGNALS.trailing, ...(s?.trailing ?? {}) },
    trailingOnly: { ...DEFAULT_SIGNALS.trailingOnly, ...(s?.trailingOnly ?? {}) },
    atr: {
      ...DEFAULT_SIGNALS.atr,
      ...(s?.atr ?? {}),
    },
    hist: { ...DEFAULT_SIGNALS.hist, ...(s?.hist ?? {}) },
    guard: { ...DEFAULT_SIGNALS.guard, ...(s?.guard ?? {}) },
    cluster: { ...DEFAULT_SIGNALS.cluster, ...(s?.cluster ?? {}) },
    coordination: {
      ...DEFAULT_SIGNALS.coordination,
      ...(s?.coordination ?? {}),
      ranges: { ...DEFAULT_SIGNALS.coordination.ranges, ...(s?.coordination?.ranges ?? {}) },
    },
    combinedVolumeFactors: {
      ...DEFAULT_SIGNALS.combinedVolumeFactors,
      ...(s?.combinedVolumeFactors ?? {}),
    },
    sourceGate: { ...DEFAULT_SIGNALS.sourceGate, ...(s?.sourceGate ?? {}) },
    strategies: { ...DEFAULT_SIGNALS.strategies, ...(s?.strategies ?? {}) },
    filter: { ...DEFAULT_SIGNALS.filter, ...(s?.filter ?? {}) },
    accept: { ...DEFAULT_SIGNALS.accept, ...(s?.accept ?? {}) },
    sideAccept: { ...DEFAULT_SIGNALS.sideAccept, ...(s?.sideAccept ?? {}) },
    sources: { ...DEFAULT_SIGNALS.sources, ...(s?.sources ?? {}) },
    lanes: s?.lanes?.length ? [...s.lanes] : [...DEFAULT_SIGNALS.lanes],
  };
  // active signal units: 0 = no cap (every validated unit is active), otherwise 10–10,000 in steps of 10
  out.sourcesMode = out.sourcesMode === "allow" ? "allow" : "deny";
  const c = Number(out.count);
  out.count = !Number.isFinite(c)
    ? DEFAULT_SIGNALS.count
    : c <= 0
      ? 0
      : Math.min(10_000, Math.max(10, Math.round(c / 10) * 10));
  const sg = out.sourceGate;
  sg.enabled = sg.enabled === true;
  sg.days = Math.min(14, Math.max(1, Math.round(Number(sg.days) || 2)));
  sg.minTrades = Math.min(100, Math.max(1, Math.round(Number(sg.minTrades) || 5)));
  sg.minShare = Math.min(
    1,
    Math.max(0, Number.isFinite(Number(sg.minShare)) ? Number(sg.minShare) : 0.5),
  );
  out.filter.trendH = Math.min(48, Math.max(0, Number(out.filter.trendH) || 0));
  out.filter.volFloor = Math.min(0.02, Math.max(0, Number(out.filter.volFloor) || 0));
  out.accept.enabled = out.accept.enabled !== false;
  out.accept.minPf = Math.min(
    5,
    Math.max(1, Number(out.accept.minPf) || DEFAULT_SIGNALS.accept.minPf),
  );
  out.accept.hours = Math.min(
    336,
    Math.max(6, Math.round(Number(out.accept.hours) || DEFAULT_SIGNALS.accept.hours)),
  );
  out.accept.minTrades = Math.min(
    200,
    Math.max(1, Math.round(Number(out.accept.minTrades) || DEFAULT_SIGNALS.accept.minTrades)),
  );
  const sa = out.sideAccept;
  sa.enabled = sa.enabled === true;
  sa.minPf = Math.min(5, Math.max(1, Number(sa.minPf) || DEFAULT_SIGNALS.sideAccept.minPf));
  sa.hours = Math.min(
    336,
    Math.max(6, Math.round(Number(sa.hours) || DEFAULT_SIGNALS.sideAccept.hours)),
  );
  sa.minTrades = Math.min(
    1000,
    Math.max(1, Math.round(Number(sa.minTrades) || DEFAULT_SIGNALS.sideAccept.minTrades)),
  );
  out.strategies.dca = out.strategies.dca === true;
  out.strategies.axis = out.strategies.axis === true;
  out.coordination.enabled = out.coordination.enabled !== false;
  out.coordination.minSources = Math.min(
    SIGNAL_SOURCES.length,
    Math.max(2, Math.round(Number(out.coordination.minSources) || DEFAULT_SIGNALS.coordination.minSources)),
  );
  const coordinationCount = Number(out.coordination.count);
  out.coordination.count = !Number.isFinite(coordinationCount)
    ? DEFAULT_SIGNALS.coordination.count
    : coordinationCount <= 0
      ? 0
      : Math.min(10_000, Math.max(10, Math.round(coordinationCount / 10) * 10));
  out.coordination.ranges.short = out.coordination.ranges.short !== false;
  out.coordination.ranges.normal = out.coordination.ranges.normal !== false;
  out.coordination.ranges.long = out.coordination.ranges.long !== false;
  out.coordination.volumeFactor = volumeRatio(out.coordination.volumeFactor);
  for (const kind of INDICATION_KINDS)
    out.combinedVolumeFactors[kind] = volumeRatio(out.combinedVolumeFactors[kind]);
  out.ownBase = out.ownBase !== false;
  if (!["pct", "atr", "both"].includes(out.exits)) out.exits = DEFAULT_SIGNALS.exits;
  const a = out.atr;
  const nums = (xs: unknown, lo: number, hi: number, def: readonly number[], empty = false) => {
    const v = Array.isArray(xs)
      ? xs.map(Number).filter((x) => Number.isFinite(x) && x >= lo && x <= hi)
      : [];
    return v.length || (empty && Array.isArray(xs)) ? [...new Set(v)].slice(0, 64) : [...def];
  };
  out.trailRatios = nums(out.trailRatios, 0.1, 2, DEFAULT_SIGNALS.trailRatios!);
  const trailMax = Number(out.trailMax);
  out.trailMax = Number.isFinite(trailMax)
    ? Math.min(0.03, Math.max(0.002, trailMax))
    : DEFAULT_SIGNALS.trailMax;
  a.sl = nums(a.sl, 0.2, 2, DEFAULT_SIGNALS.atr.sl);
  const to = out.trailingOnly;
  to.enabled = to.enabled !== false;
  to.sl = nums(to.sl, 0.002, 0.2, DEFAULT_SIGNALS.trailingOnly.sl);
  to.trail = nums(to.trail, 0.002, 0.1, DEFAULT_SIGNALS.trailingOnly.trail);
  const step = Number(to.trailStep);
  to.trailStep = Number.isFinite(step)
    ? Math.min(2, Math.max(0.1, step))
    : DEFAULT_SIGNALS.trailingOnly.trailStep;
  a.tpRatio = nums(a.tpRatio, 0.2, 3, DEFAULT_SIGNALS.atr.tpRatio);
  a.trail = nums(a.trail, 0.4, 2.4, DEFAULT_SIGNALS.atr.trail, true);
  const hs = out.hist;
  hs.enabled = hs.enabled !== false;
  hs.quantiles = nums(hs.quantiles, 0.2, 0.95, DEFAULT_SIGNALS.hist.quantiles);
  hs.trailOfTp = nums(hs.trailOfTp, 0.1, 0.95, DEFAULT_SIGNALS.hist.trailOfTp);
  hs.slOfTp = nums(hs.slOfTp, 0.25, 5, DEFAULT_SIGNALS.hist.slOfTp);
  hs.minN = Math.min(1000, Math.max(5, Math.round(Number(hs.minN) || DEFAULT_SIGNALS.hist.minN)));
  // the lookback in hours (0 = the whole history), never negative, at most 30 days
  const lb = Number(hs.lookbackH);
  hs.lookbackH = Number.isFinite(lb)
    ? Math.min(720, Math.max(0, lb))
    : DEFAULT_SIGNALS.hist.lookbackH;
  const hb = Number(a.holdBars);
  a.holdBars = Number.isFinite(hb) ? Math.min(384, Math.max(0, Math.round(hb))) : 3;
  const fl = (v: unknown, d: number) =>
    Number.isFinite(Number(v)) ? Math.min(0.1, Math.max(0, Number(v))) : d;
  out.minSl = fl(out.minSl, DEFAULT_SIGNALS.minSl);
  out.minTrail = fl(out.minTrail, DEFAULT_SIGNALS.minTrail);
  out.rangeVolumeFactors.short = volumeRatio(out.rangeVolumeFactors.short);
  out.rangeVolumeFactors.medium = volumeRatio(out.rangeVolumeFactors.medium);
  const vh = Number(out.validateH);
  out.validateH = Number.isFinite(vh) ? Math.min(72, Math.max(2, Math.round(vh))) : 24;
  out.guard.lastN = Math.min(
    50,
    Math.max(2, Math.round(Number(out.guard.lastN) || DEFAULT_SIGNALS.guard.lastN)),
  );
  return out;
}

/** The independent short/medium range encoded at the end of a signal id, before any timeframe suffix. */
export function signalRangeOf(ind: string): "short" | "medium" | null {
  const base = ind.split("@", 1)[0];
  if (!base.startsWith("sig-")) return null;
  return base.endsWith("-s") ? "short" : base.endsWith("-m") ? "medium" : null;
}

/** A partial signals patch applied onto current settings (nested groups merged, lists replaced). */
export function mergeSignals(
  cur: SignalSettings | undefined,
  p?: Partial<SignalSettings> | null,
): SignalSettings {
  const a = signalSettings(cur);
  if (!p) return a;
  return signalSettings({
    ...a,
    ...p,
    ranges: { ...a.ranges, ...(p.ranges ?? {}) },
    normal: { ...a.normal, ...(p.normal ?? {}) },
    trailing: { ...a.trailing, ...(p.trailing ?? {}) },
    trailingOnly: { ...a.trailingOnly, ...(p.trailingOnly ?? {}) },
    atr: { ...a.atr, ...(p.atr ?? {}) },
    hist: { ...a.hist, ...(p.hist ?? {}) },
    guard: { ...a.guard, ...(p.guard ?? {}) },
    cluster: { ...a.cluster, ...(p.cluster ?? {}) },
    coordination: {
      ...a.coordination,
      ...(p.coordination ?? {}),
      ranges: { ...a.coordination.ranges, ...(p.coordination?.ranges ?? {}) },
    },
    combinedVolumeFactors: {
      ...a.combinedVolumeFactors,
      ...(p.combinedVolumeFactors ?? {}),
    },
    filter: { ...a.filter, ...(p.filter ?? {}) },
    accept: { ...a.accept, ...(p.accept ?? {}) },
    sideAccept: { ...a.sideAccept, ...(p.sideAccept ?? {}) },
    strategies: { ...a.strategies, ...(p.strategies ?? {}) },
    sourceGate: { ...a.sourceGate, ...(p.sourceGate ?? {}) },
    sources: { ...a.sources, ...(p.sources ?? {}) },
    lanes: p.lanes?.length ? [...p.lanes] : a.lanes,
  });
}

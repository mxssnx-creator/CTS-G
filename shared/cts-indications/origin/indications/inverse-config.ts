/** A bounded research catalogue; every source/profile/lane keeps its own qualification. */
export const INVERSE_TIMEFRAMES = [1, 5, 15] as const;
export const INVERSE_SOURCES = [
  "trend-ema",
  "trend-adx",
  "trend-st",
  "break-don20",
  "break-atr",
  "act-burst",
  "dir-vwap",
  "move-impulse",
  "rsi-mom-14-20",
  "bb-walk",
  "sar-std",
  "macd-hist",
  "ema-9-21",
  "cci-14-100",
  "obv-20",
  "kelt-20-1.5",
  "ichi-tk-9",
  "hma-16",
  "tactic-trend-s",
] as const;
export const INVERSE_MATH = [
  "zscore",
  "impulse",
  "acceleration",
  "regression",
  "wick",
  "failed-break",
  "consensus",
  "market-residual",
  "market-shock",
  "market-aligned",
  "rsi2-turn",
  "vwap-deviation",
] as const;
export const INVERSE_WINDOWS = [10, 20, 40, 80] as const;
/** Separate Micro identities reuse shared signals, but qualify only their own Micro/Minimal cells. */
export const INVERSE_MICRO_MATH = [
  "zscore", "impulse", "wick", "failed-break", "consensus", "market-residual", "rsi2-turn", "vwap-deviation",
] as const;
export const INVERSE_MICRO_WINDOWS = [10, 20] as const;
/** Bounded reference pairs avoid an O(symbols²) catalogue. Each traded leg qualifies independently. */
export const INVERSE_ANCHORS = ["BTC-USDT", "ETH-USDT"] as const;
export const INVERSE_PAIR_METHODS = ["spread", "residual"] as const;
export const INVERSE_PAIR_WINDOWS = [10, 20, 40] as const;
export const INVERSE_CONFIG_IDS = [
  ...INVERSE_SOURCES.flatMap((id) => [`inverse-known-${id}`, `inverse-turn-${id}`]),
  ...INVERSE_MATH.flatMap((method) => INVERSE_WINDOWS.map((p) => `inverse-${method}-${p}`)),
  ...INVERSE_MICRO_MATH.flatMap((method) => INVERSE_MICRO_WINDOWS.map((p) => `inverse-mc-${method}-${p}`)),
  ...INVERSE_ANCHORS.flatMap((anchor) => INVERSE_PAIR_METHODS.flatMap((method) =>
    INVERSE_PAIR_WINDOWS.map((p) => `inverse-pair-${anchor.split("-")[0].toLowerCase()}-${method}-${p}`))),
];
export const isInverseInd = (id: string) => id.startsWith("inverse-");
/** Exact published v36 catalogue, for additive factory-selection upgrades. */
export const INVERSE_PREVIOUS_CONFIG_IDS = INVERSE_CONFIG_IDS.filter((id) =>
  !id.includes("rsi2-turn-") && !id.includes("vwap-deviation-"));

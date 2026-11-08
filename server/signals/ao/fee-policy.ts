/** Rates are fractions; 0.1 slippageFeeRatio means 10% of the applicable fee. */
export const DEFAULT_FEE_POLICY = Object.freeze({ maker: 0.0005, taker: 0.0008, slippageFeeRatio: 0.1 });
export type FeePolicy = { maker: number; taker: number; slippageFeeRatio: number };
export type FeeSnapshot = { exchange: string; account: string; symbol: string; at: number; maker: number; taker: number };
export type FeeContext = Pick<FeeSnapshot, "exchange" | "account" | "symbol"> & { now: number; maxAgeMs?: number };
export function feePolicy(p: Partial<FeePolicy> = {}): FeePolicy {
  const out = { ...DEFAULT_FEE_POLICY, ...p };
  for (const k of ["maker", "taker", "slippageFeeRatio"] as const)
    if (!Number.isFinite(out[k]) || out[k] < 0 || out[k] > (k === "slippageFeeRatio" ? 10 : 0.05))
      throw new RangeError(`Invalid ${k}`);
  return out;
}
export function resolveFeePolicy(p: Partial<FeePolicy>, snapshot?: FeeSnapshot, context?: FeeContext) {
  const policy = feePolicy(p);
  const valid = snapshot && context && snapshot.exchange === context.exchange && snapshot.account === context.account &&
    snapshot.symbol === context.symbol && Number.isFinite(snapshot.at) && snapshot.at <= context.now &&
    context.now - snapshot.at <= (context.maxAgeMs ?? 7_200_000) &&
    [snapshot.maker, snapshot.taker].every(r => Number.isFinite(r) && r >= 0 && r <= 0.05);
  return { ...(valid ? { ...policy, maker: snapshot.maker, taker: snapshot.taker } : policy),
    source: valid ? "account-rate" as const : "configured-estimate" as const };
}
export function roundTripFeeRate(p: Partial<FeePolicy> = {}, entry: "maker" | "taker" = "taker", exit: "maker" | "taker" = "taker") {
  const policy = feePolicy(p);
  return (policy[entry] + policy[exit]) * (1 + policy.slippageFeeRatio);
}
/** Actual fill prices contain slippage already. Explicit zero and signed rebates remain measured. */
export function actualFillFee(qty: number, price: number, p: Partial<FeePolicy> = {}, isMaker = false, reported?: number) {
  if (![qty, price].every(x => Number.isFinite(x) && x > 0)) throw new RangeError("Invalid fill");
  if (reported !== undefined) {
    if (!Number.isFinite(reported)) throw new RangeError("Invalid reported fee");
    return { fee: reported, source: "reported-fee" as const };
  }
  const policy = feePolicy(p);
  return { fee: qty * price * (isMaker ? policy.maker : policy.taker), source: "rate-estimate" as const };
}
export function parseCommission(exchange: "bingx" | "bybit", payload: unknown): { maker: number; taker: number } | null {
  const body = payload as Record<string, any> | null;
  if (!body || (exchange === "bingx" ? Number(body.code) !== 0 : Number(body.retCode) !== 0)) return null;
  const row = exchange === "bingx" ? (body.data?.commission ?? body.data) : body.result?.list?.[0];
  const values = exchange === "bingx" ? [row?.makerCommissionRate, row?.takerCommissionRate] : [row?.makerFeeRate, row?.takerFeeRate];
  if (values.some(v => v === null || v === undefined || v === "")) return null;
  const [maker, taker] = values.map(Number);
  if (![maker, taker].every(r => Number.isFinite(r) && r >= 0 && r <= 0.05)) return null;
  return { maker, taker };
}

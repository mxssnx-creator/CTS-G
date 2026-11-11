// Inverse is an indication, not a sizing multiplier or a post-hoc negation of returns.
// Direction is inverted BEFORE next-bar entry and the ordinary independent Base/config/live gates.
import type { Bars } from "../domain/types.ts";
import type { IndicationSpec } from "./registry.ts";
import type { MarketRef } from "./market.ts";
import { SeriesCache } from "./cache.ts";
import { stdev, zscore } from "../math/indicators.ts";
import {
  INVERSE_MATH,
  INVERSE_SOURCES,
  INVERSE_WINDOWS,
  INVERSE_TIMEFRAMES,
  INVERSE_MICRO_MATH,
  INVERSE_MICRO_WINDOWS,
  INVERSE_ANCHORS,
  INVERSE_PAIR_METHODS,
  INVERSE_PAIR_WINDOWS,
} from "./inverse-config.ts";

const WARMUP = 80;
const sign = (x: number) => (x > 0 ? 1 : x < 0 ? -1 : 0);
const valid = (b: Bars, i: number) =>
  Number.isFinite(b.t[i]) &&
  Number.isFinite(b.o[i]) &&
  Number.isFinite(b.h[i]) &&
  Number.isFinite(b.l[i]) &&
  Number.isFinite(b.c[i]) &&
  Number.isFinite(b.v[i]) &&
  b.l[i] > 0 &&
  b.h[i] >= Math.max(b.o[i], b.c[i]) &&
  b.l[i] <= Math.min(b.o[i], b.c[i]) &&
  b.v[i] >= 0;

/** Segment caches are shared by all variants; invalid data/gaps reset recursive indicators. */
function segments(k: SeriesCache): Array<{ from: number; to: number; k: SeriesCache }> {
  return k.memo("inverse:segments:v1", () => {
    const b = k.b;
    if (k.streamMath) {
      const check = k.streamMath.frame(
        "inverse:valid",
        b.n,
        ["valid"],
        () => ({ invalid: false }),
        ["valid"],
      );
      for (let i = check.from; i < b.n; i++)
        if (!valid(b, i) || (i > 0 && b.t[i] - b.t[i - 1] !== b.tfMin * 60_000))
          check.state.invalid = true;
      check.commit(check.state);
      if (!check.state.invalid) return [{ from: 0, to: b.n, k }];
    }
    const spans: Array<[number, number]> = [];
    let start = -1;
    for (let i = 0; i < b.n; i++) {
      const good = valid(b, i);
      const adjacent = i > 0 && b.t[i] - b.t[i - 1] === b.tfMin * 60_000;
      if (start >= 0 && (!good || !adjacent)) {
        spans.push([start, i]);
        start = -1;
      }
      if (good && start < 0) start = i;
    }
    if (start >= 0) spans.push([start, b.n]);
    return spans.map(([from, to]) => ({
      from,
      to,
      k:
        from === 0 && to === b.n
          ? k
          : new SeriesCache({
              ...b,
              n: to - from,
              t: b.t.subarray(from, to),
              o: b.o.subarray(from, to),
              h: b.h.subarray(from, to),
              l: b.l.subarray(from, to),
              c: b.c.subarray(from, to),
              v: b.v.subarray(from, to),
            }),
    }));
  });
}

function safeState(
  k: SeriesCache,
  fn: (k: SeriesCache, market: MarketRef | null) => Int8Array,
  reference?: MarketRef | null,
): Int8Array {
  if (!(INVERSE_TIMEFRAMES as readonly number[]).includes(k.b.tfMin)) return new Int8Array(k.b.n);
  const market = reference === undefined ? k.market() : reference;
  const parts = segments(k);
  // Immutable, gap-free arrays can be shared by the standard and Micro identities.
  // Their gates/configs remain separate; only the causal calculation is reused.
  if (parts.length === 1 && parts[0].k === k && k.b.n > WARMUP) return fn(k, market);
  const out = new Int8Array(k.b.n);
  for (const s of parts) {
    if (s.to - s.from <= WARMUP) continue;
    if (k.streamMath && s.k !== k)
      s.k.enableStreaming(k.streamMath.child(`inverse-segment:${k.b.t[s.from]}`));
    const m = market && {
      syms: market.syms,
      ret: market.ret.subarray(s.from, s.to),
      idx: market.idx.subarray(s.from, s.to),
      act: market.act.subarray(s.from, s.to),
    };
    const values = fn(s.k, m);
    out.set(values.subarray(WARMUP), s.from + WARMUP);
  }
  return out;
}

/** Shared O(n) math. Rolling OLS uses bounded sums; no window scan per bar/config. */
function context(k: SeriesCache, p: number) {
  return k.memo(`inverse:context:${p}:v1`, () => {
    const b = k.b;
    const log = k.memo("inverse:log", () =>
      k.streamMath
        ? k.streamMath.vector(
            "inverse:log",
            b.n,
            () => 0,
            (i, out) => {
              out[i] = Math.log(b.c[i]);
            },
          )
        : Float64Array.from(b.c, Math.log),
    );
    const z = k.streamMath?.z(log, p, "inverse:log") ?? zscore(log, p);
    const atr = k.atr(14);
    const frame = k.streamMath?.frame<{ value: Float64Array }, { sy: number; sxy: number }>(
      `inverse:reg:${p}`,
      b.n,
      ["value"],
      () => ({ sy: 0, sxy: 0 }),
    );
    const reg = frame?.cols.value ?? new Float64Array(b.n).fill(NaN);
    const sx = (p * (p - 1)) / 2;
    const sxx = (p * (p - 1) * (2 * p - 1)) / 6;
    const den = p * sxx - sx * sx;
    let sy = frame?.state.sy ?? 0,
      sxy = frame?.state.sxy ?? 0;
    for (let i = frame?.from ?? 0; i < b.n; i++) {
      if (i < p) {
        sy += log[i];
        sxy += i * log[i];
      } else {
        const previousSum = sy;
        sy += log[i] - log[i - p];
        sxy += (p - 1) * log[i] - (previousSum - log[i - p]);
      }
      if (i >= p - 1) {
        const slope = (p * sxy - sx * sy) / den;
        reg[i] = log[i] - (sy / p + slope * (p - 1 - sx / p));
      }
    }
    frame?.commit({ sy, sxy });
    return {
      log,
      z,
      reg,
      atr,
      ema: k.ema(p),
      adx: k.dmi(14).adx,
      don: k.don(p),
      volume: k.volSma(p),
      macd: k.macd().hist,
      rsi: k.rsi(14),
      rsi2: k.rsi(2),
      vwap: k.vwap(p),
      dmi: k.dmi(14),
      stoch: k.stoch().k,
    };
  });
}

function mathStates(k: SeriesCache, market: MarketRef | null, p: number) {
  // Market is immutable for a SeriesCache lifetime; setMarket clears all memoized states.
  return k.memo(`inverse:math:${p}:v1`, () => {
    const b = k.b,
      x = context(k, p);
    const frame = k.streamMath?.frame<
      Record<(typeof INVERSE_MATH)[number], Int8Array>,
      { sm: number; ss: number; smm: number; sms: number; count: number }
    >(
      `inverse:math:${p}`,
      b.n,
      INVERSE_MATH,
      () => ({ sm: 0, ss: 0, smm: 0, sms: 0, count: 0 }),
      INVERSE_MATH,
    );
    const out =
      frame?.cols ??
      (Object.fromEntries(INVERSE_MATH.map((m) => [m, new Int8Array(b.n)])) as Record<
        (typeof INVERSE_MATH)[number],
        Int8Array
      >);
    const lr =
      k.streamMath?.vector(
        "inverse:lr",
        b.n,
        () => 0,
        (i, out) => {
          out[i] = i ? x.log[i] - x.log[i - 1] : 0;
        },
      ) ?? new Float64Array(b.n);
    if (!k.streamMath) for (let i = 1; i < b.n; i++) lr[i] = x.log[i] - x.log[i - 1];
    const mr = market?.ret;
    // A missing initial return must not poison rolling sums forever. count below requires
    // a complete measured previous window before any market signal can use this series.
    const marketReturns =
      mr &&
      (k.streamMath?.vector(
        "inverse:market-finite",
        b.n,
        () => 0,
        (i, out) => {
          out[i] = Number.isFinite(mr[i]) ? mr[i] : 0;
        },
      ) ??
        Float64Array.from(mr, (v) => (Number.isFinite(v) ? v : 0)));
    const mdev = marketReturns
      ? (k.streamMath?.stdev(marketReturns, p, "inverse:market") ?? stdev(marketReturns, p))
      : null;
    // Covariance/variance for the PREVIOUS p returns, excluding the signal bar.
    let sm = frame?.state.sm ?? 0,
      ss = frame?.state.ss ?? 0,
      smm = frame?.state.smm ?? 0,
      sms = frame?.state.sms ?? 0,
      count = frame?.state.count ?? 0;
    const threshold = p <= 10 ? 1.25 : p === 20 ? 1.5 : p === 40 ? 2 : 2.5;
    for (let i = Math.max(1, frame?.from ?? 1); i < b.n; i++) {
      if (mr) {
        const j = i - 1;
        if (Number.isFinite(mr[j]) && Number.isFinite(lr[j])) {
          sm += mr[j];
          ss += lr[j];
          smm += mr[j] ** 2;
          sms += mr[j] * lr[j];
          count++;
        }
        if (i > p) {
          const old = i - p - 1;
          if (Number.isFinite(mr[old]) && Number.isFinite(lr[old])) {
            sm -= mr[old];
            ss -= lr[old];
            smm -= mr[old] ** 2;
            sms -= mr[old] * lr[old];
            count--;
          }
        }
      }
      if (
        i < WARMUP ||
        !(x.atr[i - 1] > 0) ||
        !Number.isFinite(x.z[i]) ||
        !Number.isFinite(x.adx[i]) ||
        x.adx[i] > 50
      )
        continue;
      const a = x.atr[i - 1],
        c = b.c[i];
      const turn = sign(c - b.c[i - 1]);
      const extension = (c - x.ema[i]) / a;
      const stretch = sign(extension);
      const reversing = turn !== 0 && turn === -stretch;
      const volume = x.volume[i - 1];
      if (!(volume > 0) || b.v[i] / volume > 5) continue;
      if (Math.abs(x.z[i]) >= threshold && turn === -sign(x.z[i])) out.zscore[i] = turn;
      const impulse = (b.c[i - 1] - b.c[i - 4]) / a;
      if (Math.abs(impulse) > threshold && turn === -sign(impulse)) out.impulse[i] = turn;
      const previousMove = b.c[i - 1] - b.c[i - 2];
      const acceleration = (c - b.c[i - 1] - previousMove) / a;
      if (
        Math.abs(extension) >= 1 &&
        reversing &&
        sign(acceleration) === turn &&
        Math.abs(acceleration) >= 0.2
      )
        out.acceleration[i] = turn;
      const residual = x.reg[i] / (a / c);
      if (Math.abs(residual) >= threshold / 2 && turn === -sign(residual)) out.regression[i] = turn;
      const range = b.h[i] - b.l[i];
      const upper = b.h[i] - Math.max(b.o[i], c),
        lower = Math.min(b.o[i], c) - b.l[i];
      if (range >= a && Math.abs(extension) >= 0.5) {
        if (upper > range * 0.55 && c < b.o[i] && stretch > 0) out.wick[i] = -1;
        if (lower > range * 0.55 && c > b.o[i] && stretch < 0) out.wick[i] = 1;
      }
      const upFail = b.h[i] > x.don.hi[i] && c < x.don.hi[i] && c < b.o[i];
      const dnFail = b.l[i] < x.don.lo[i] && c > x.don.lo[i] && c > b.o[i];
      if (upFail !== dnFail) out["failed-break"][i] = upFail ? -1 : 1;
      const votes =
        sign(extension) +
        sign(x.macd[i]) +
        sign(x.rsi[i] - 50) +
        sign(x.dmi.pdi[i] - x.dmi.mdi[i]) +
        sign(x.stoch[i] - 50);
      if (Math.abs(votes) >= 3 && Math.abs(extension) >= 0.75 && turn === -sign(votes))
        out.consensus[i] = turn;
      // Fade an already observed fast-RSI extreme only after the next completed-bar turn.
      const rsiBand = p <= 10 ? 20 : p === 20 ? 15 : p === 40 ? 10 : 5;
      if ((x.rsi2[i - 1] < rsiBand && turn > 0) || (x.rsi2[i - 1] > 100 - rsiBand && turn < 0))
        out["rsi2-turn"][i] = turn;
      // Price/volume mean, not an unweighted price average. The prior stretch is known before the turn bar.
      const vwStretch = (b.c[i - 1] - x.vwap[i - 1]) / a;
      if (Math.abs(vwStretch) >= threshold && turn === -sign(vwStretch))
        out["vwap-deviation"][i] = turn;
      if (
        !market ||
        market.syms < 2 ||
        count !== p ||
        !mdev ||
        !(mdev[i - 1] > 0) ||
        !Number.isFinite(mr![i]) ||
        !Number.isFinite(market.idx[i]) ||
        !Number.isFinite(market.idx[i - p]) ||
        !Number.isFinite(market.act[i]) ||
        market.act[i] > 4
      )
        continue;
      const variance = smm - (sm * sm) / p;
      if (!(variance > 1e-16)) continue;
      const beta = Math.max(-3, Math.min(3, (sms - (sm * ss) / p) / variance));
      const marketMove = market.idx[i] - market.idx[i - p];
      const relative = (x.log[i] - x.log[i - p] - beta * marketMove) / (a / c);
      if (Math.abs(relative) >= threshold && turn === -sign(relative)) {
        out["market-residual"][i] = turn;
        if (turn === sign(marketMove)) out["market-aligned"][i] = turn;
      }
      const shock = (market.idx[i] - market.idx[i - 3]) / (mdev[i - 1] * Math.sqrt(3));
      if (
        Math.abs(shock) >= threshold &&
        stretch === sign(shock) &&
        reversing &&
        Math.abs(extension) >= 1
      )
        out["market-shock"][i] = turn;
    }
    frame?.commit({ sm, ss, smm, sms, count });
    return out;
  });
}

/** Causal pair fit: previous p return pairs estimate beta; the signal bar never fits itself. */
function pairStates(k: SeriesCache, peer: MarketRef | null, anchor: string, p: number) {
  return k.memo(`inverse:pair:${anchor}:${p}`, () => {
    const b = k.b;
    if (!peer) return { spread: new Int8Array(b.n), residual: new Int8Array(b.n) };
    const names = ["spread", "residual"] as const;
    const frame = k.streamMath?.frame<
      Record<(typeof names)[number], Int8Array>,
      {
        sx: number;
        sy: number;
        sxx: number;
        syy: number;
        sxy: number;
        count: number;
        sz: number;
        szz: number;
        nz: number;
      }
    >(
      `inverse:pair:${anchor}:${p}`,
      b.n,
      names,
      () => ({ sx: 0, sy: 0, sxx: 0, syy: 0, sxy: 0, count: 0, sz: 0, szz: 0, nz: 0 }),
      names,
    );
    const out = frame?.cols ?? { spread: new Int8Array(b.n), residual: new Int8Array(b.n) };
    const log = k.memo("inverse:log", () =>
      k.streamMath
        ? k.streamMath.vector(
            "inverse:log",
            b.n,
            () => 0,
            (i, out) => {
              out[i] = Math.log(b.c[i]);
            },
          )
        : Float64Array.from(b.c, Math.log),
    );
    const lr =
      k.streamMath?.vector(
        "inverse:pair-lr",
        b.n,
        () => 0,
        (i, out) => {
          if (i) out[i] = log[i] - log[i - 1];
        },
      ) ?? new Float64Array(b.n).fill(NaN);
    if (!k.streamMath) for (let i = 1; i < b.n; i++) lr[i] = log[i] - log[i - 1];
    let sx = frame?.state.sx ?? 0,
      sy = frame?.state.sy ?? 0,
      sxx = frame?.state.sxx ?? 0,
      syy = frame?.state.syy ?? 0,
      sxy = frame?.state.sxy ?? 0,
      count = frame?.state.count ?? 0;
    let sz = frame?.state.sz ?? 0,
      szz = frame?.state.szz ?? 0,
      nz = frame?.state.nz ?? 0;
    const add = (j: number, d: number) => {
      const x = peer.ret[j],
        y = lr[j];
      if (!Number.isFinite(x) || !Number.isFinite(y)) return;
      sx += d * x;
      sy += d * y;
      sxx += d * x * x;
      syy += d * y * y;
      sxy += d * x * y;
      count += d;
    };
    const spread = (j: number) => (Number.isFinite(peer.ret[j]) ? log[j] - peer.idx[j] : NaN);
    const threshold = p <= 10 ? 1.25 : p === 20 ? 1.5 : 2;
    for (let i = Math.max(1, frame?.from ?? 1); i < b.n; i++) {
      add(i - 1, 1);
      if (i > p) add(i - p - 1, -1);
      const z = spread(i);
      if (Number.isFinite(z)) {
        sz += z;
        szz += z * z;
        nz++;
      }
      if (i > p) {
        const old = spread(i - p);
        if (Number.isFinite(old)) {
          sz -= old;
          szz -= old * old;
          nz--;
        }
      }
      if (i < WARMUP || count !== p || nz !== p || !Number.isFinite(z)) continue;
      const vx = sxx - (sx * sx) / p,
        vy = syy - (sy * sy) / p,
        cov = sxy - (sx * sy) / p;
      if (!(vx > 1e-16) || !(vy > 1e-16) || cov / Math.sqrt(vx * vy) < 0.2) continue;
      const turn = sign(lr[i]);
      const variance = szz / p - (sz / p) ** 2;
      if (variance > 1e-16) {
        const zs = (z - sz / p) / Math.sqrt(variance);
        if (
          Math.abs(zs) >= threshold &&
          sign(z - spread(i - 1)) === -sign(zs) &&
          turn === -sign(zs)
        )
          out.spread[i] = turn;
      }
      const beta = cov / vx;
      const residualVar = (vy - (cov * cov) / vx) / p;
      if (residualVar > 1e-16 && Number.isFinite(peer.idx[i - 3])) {
        const rs =
          (log[i] - log[i - 3] - beta * (peer.idx[i] - peer.idx[i - 3])) /
          Math.sqrt(3 * residualVar);
        if (Math.abs(rs) >= threshold && turn === -sign(rs)) out.residual[i] = turn;
      }
    }
    frame?.commit({ sx, sy, sxx, syy, sxy, count, sz, szz, nz });
    return out;
  });
}

export function inverseSpecs(known: readonly IndicationSpec[]): IndicationSpec[] {
  const byId = new Map(known.map((s) => [s.id, s]));
  const out: IndicationSpec[] = [];
  for (const id of INVERSE_SOURCES) {
    const source = byId.get(id);
    if (!source) throw new Error(`Inverse source missing: ${id}`);
    for (const confirmed of [false, true])
      out.push({
        id: `inverse-${confirmed ? "turn" : "known"}-${id}`,
        kind: "inverse",
        label: `Inverse · ${source.label}${confirmed ? " · confirmed turn" : " · opposite state"}`,
        params: { ...source.params, confirmed: Number(confirmed) },
        fn: (k) =>
          safeState(k, (part) => {
            const states = part.memo(`ind:${id}`, () => source.fn(part));
            const frame = part.streamMath?.frame<{ value: Int8Array }, number>(
              `inverse-known:${id}:${confirmed}`,
              part.b.n,
              ["value"],
              () => 0,
              ["value"],
            );
            const values = frame?.cols.value ?? new Int8Array(part.b.n);
            const x = confirmed ? context(part, 20) : null;
            for (let i = Math.max(WARMUP, frame?.from ?? WARMUP); i < values.length; i++) {
              const side = states[i];
              if (side === 0) continue;
              if (
                x &&
                (!(x.atr[i - 1] > 0) ||
                  !Number.isFinite(x.adx[i]) ||
                  x.adx[i] > 45 ||
                  Math.abs(part.b.c[i] - x.ema[i]) < 0.5 * x.atr[i - 1] ||
                  sign(part.b.c[i] - part.b.c[i - 1]) !== -side)
              )
                continue;
              values[i] = -side;
            }
            frame?.commit(0);
            return values;
          }),
      });
  }
  for (const method of INVERSE_MATH)
    for (const p of INVERSE_WINDOWS)
      out.push({
        id: `inverse-${method}-${p}`,
        kind: "inverse",
        label: `Inverse · ${method} · ${p}`,
        params: { p, warmup: WARMUP },
        fn: (k) => safeState(k, (part, market) => mathStates(part, market, p)[method]),
      });
  for (const method of INVERSE_MICRO_MATH)
    for (const p of INVERSE_MICRO_WINDOWS)
      out.push({
        id: `inverse-mc-${method}-${p}`,
        kind: "inverse",
        label: `Inverse · Micro · ${method} · ${p}`,
        params: { p, warmup: WARMUP },
        fn: (k) => safeState(k, (part, market) => mathStates(part, market, p)[method]),
      });
  for (const anchor of INVERSE_ANCHORS)
    for (const method of INVERSE_PAIR_METHODS)
      for (const p of INVERSE_PAIR_WINDOWS)
        out.push({
          id: `inverse-pair-${anchor.split("-")[0].toLowerCase()}-${method}-${p}`,
          kind: "inverse",
          label: `Inverse · ${anchor} pair ${method} · ${p}`,
          params: { p, warmup: WARMUP },
          fn: (k) =>
            safeState(
              k,
              (part, peer) => pairStates(part, peer, anchor, p)[method],
              k.reference(anchor),
            ),
        });
  return out;
}

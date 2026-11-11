import { SIGNAL_SOURCES } from "./signal-config.ts";
import type { SeriesCache } from "./indications/cache.ts";
import type { Protect } from "./domain/types.ts";

export const SIGNAL_COORDINATION_RATIOS = [0.6, 0.65, 0.7, 0.75, 0.8] as const;
export type SignalCoordinationRatio = (typeof SIGNAL_COORDINATION_RATIOS)[number];
type LegacySignalCoordinationRatio = 0.85 | 0.9 | 0.95 | 1;
type ParsedSignalCoordinationRatio = SignalCoordinationRatio | LegacySignalCoordinationRatio;
export type SignalInputMask = 1 | 2 | 3;
export type SignalCoordinationRanges = { short: boolean; normal: boolean; long: boolean };

export interface SignalCoordinationId {
  /** v1 identities retain their historical cumulative-threshold behavior for held positions. */
  version: 1 | 2;
  ratio: ParsedSignalCoordinationRatio;
  minSources: number;
  inputMask: SignalInputMask;
  sourceNames: string[];
}

const sourceIndex = new Map(SIGNAL_SOURCES.map((s, i) => [s.name, i] as const));
const ALL_COORDINATION_RATIOS: readonly ParsedSignalCoordinationRatio[] = [
  ...SIGNAL_COORDINATION_RATIOS, 0.85, 0.9, 0.95, 1,
];
const ratioPercent = new Map<number, ParsedSignalCoordinationRatio>(
  ALL_COORDINATION_RATIOS.map((r): [number, ParsedSignalCoordinationRatio] => [Math.round(r * 100), r]),
);

function toBase36(value: bigint): string {
  return value.toString(36);
}

function fromBase36(value: string): bigint | null {
  let out = 0n;
  for (const c of value) {
    const digit = Number.parseInt(c, 36);
    if (!Number.isInteger(digit) || digit < 0 || digit >= 36) return null;
    out = out * 36n + BigInt(digit);
  }
  return out;
}

function stripLane(ind: string): string {
  return ind.replace(/@m\d+c?$/, "");
}

/** A stable, compact identity for one ratio band × source pool × parameter-range mix. */
export function signalCoordinationId(
  sources: readonly string[],
  ratio: SignalCoordinationRatio,
  minSources: number,
  inputMask: SignalInputMask,
): string {
  const selected = new Set(sources);
  let mask = 0n;
  for (const [i, source] of SIGNAL_SOURCES.entries())
    if (selected.has(source.name)) mask |= 1n << BigInt(i);
  if (mask === 0n) throw new Error("signal coordination needs at least one source");
  if (!SIGNAL_COORDINATION_RATIOS.includes(ratio)) throw new Error("unsupported domination ratio");
  if (!Number.isInteger(minSources) || minSources < 2 || minSources > SIGNAL_SOURCES.length)
    throw new Error("signal coordination minimum sources out of range");
  if (inputMask !== 1 && inputMask !== 2 && inputMask !== 3)
    throw new Error("signal coordination input range out of range");
  return `sig-coord-v2-r${Math.round(ratio * 100)}-n${minSources}-i${inputMask}-b${toBase36(mask)}`;
}

/** Decode a lane-independent or lane-qualified coordinator indication identity. */
export function parseSignalCoordinationId(ind: string): SignalCoordinationId | null {
  const base = stripLane(ind);
  const m = /^sig-coord-v([12])-r(60|65|70|75|80|85|90|95|100)-n(\d+)-i([123])-b([0-9a-z]+)$/.exec(base);
  if (!m) return null;
  const version = Number(m[1]) as 1 | 2;
  const ratio = ratioPercent.get(Number(m[2]));
  const mask = fromBase36(m[5]);
  const minSources = Number(m[3]);
  const inputMask = Number(m[4]) as SignalInputMask;
  if (!ratio || (version === 2 && ratio > 0.8) || !mask || !Number.isInteger(minSources) || minSources < 2) return null;
  const sourceNames = SIGNAL_SOURCES.flatMap((s, i) => (mask & (1n << BigInt(i)) ? [s.name] : []));
  if (mask >> BigInt(SIGNAL_SOURCES.length) || sourceNames.length < minSources) return null;
  return { version, ratio, minSources, inputMask, sourceNames };
}

export function isSignalCoordinationInd(ind: string): boolean {
  return parseSignalCoordinationId(ind) !== null;
}

export function signalCoordinationSourceLabel(ind: string): string {
  const x = parseSignalCoordinationId(ind);
  if (!x) return "coordination";
  return x.version === 1
    ? `coordination-legacy-threshold-r${Math.round(x.ratio * 100)}-i${x.inputMask}`
    : `coordination-band-r${Math.round(x.ratio * 100)}-i${x.inputMask}`;
}

interface VoteCounts {
  positive: Uint8Array;
  negative: Uint8Array;
}

/** Count at most one vote per source; a source whose enabled variants conflict abstains. */
export function signalCoordinationVoteCounts(
  sourceStates: readonly (readonly (ArrayLike<number> | null)[])[],
  length: number,
): VoteCounts {
  const positive = new Uint8Array(length);
  const negative = new Uint8Array(length);
  for (let i = 0; i < length; i++) {
    for (const variants of sourceStates) {
      let direction = 0;
      let conflict = false;
      for (const states of variants) {
        const value = states?.[i] ?? 0;
        if (!value) continue;
        if (direction && direction !== value) {
          conflict = true;
          break;
        }
        direction = value;
      }
      if (conflict) continue;
      if (direction > 0) positive[i]++;
      else if (direction < 0) negative[i]++;
    }
  }
  return { positive, negative };
}

/** Return a directional state only when the side clears its own domination ratio and minimum vote count. */
export function signalCoordinationStateFromVotes(
  counts: VoteCounts,
  ratio: ParsedSignalCoordinationRatio,
  minSources: number,
  version: 1 | 2 = 2,
): Int8Array {
  const out = new Int8Array(counts.positive.length);
  const threshold = Math.round(ratio * 100);
  for (let i = 0; i < out.length; i++) {
    const pos = counts.positive[i];
    const neg = counts.negative[i];
    const total = pos + neg;
    if (total < minSources) continue;
    if (version === 1) {
      if (pos * 100 >= threshold * total) out[i] = 1;
      else if (neg * 100 >= threshold * total) out[i] = -1;
      continue;
    }
    // Each identity owns one disjoint band. The final 80% identity covers [80%, 100%],
    // including unanimity. Use integer comparisons to avoid float drift.
    const bandStart = threshold;
    const bandEnd = ratio === 0.8 ? 100 : threshold + 5;
    const posPct = pos * 100;
    const negPct = neg * 100;
    if (posPct >= bandStart * total && (ratio === 0.8 ? posPct <= bandEnd * total : posPct < bandEnd * total)) out[i] = 1;
    else if (negPct >= bandStart * total && (ratio === 0.8 ? negPct <= bandEnd * total : negPct < bandEnd * total)) out[i] = -1;
  }
  return out;
}

/**
 * Build one causal direction per source and bar. If both parameter variants are enabled, a source votes when they
 * agree or only one is directional; conflicting variants abstain. Quiet sources do not dilute domination.
 */
function voteCounts(
  x: SignalCoordinationId,
  k: SeriesCache,
  stateOf: (ind: string, cache: SeriesCache) => Int8Array | null,
): VoteCounts {
  const variants: Array<Array<Int8Array | null>> = x.sourceNames.map(() => []);
  for (let n = 0; n < x.sourceNames.length; n++) {
    const source = x.sourceNames[n];
    if (x.inputMask & 1) variants[n].push(stateOf(`sig-${source}-s`, k));
    if (x.inputMask & 2) variants[n].push(stateOf(`sig-${source}-m`, k));
  }
  return signalCoordinationVoteCounts(variants, k.b.n);
}

/** Directional consensus at each bar; minimum sample and each domination threshold are evaluated independently. */
export function signalCoordinationState(
  ind: string,
  k: SeriesCache,
  stateOf: (ind: string, cache: SeriesCache) => Int8Array | null,
): Int8Array {
  const config = parseSignalCoordinationId(ind);
  if (!config) throw new Error(`invalid signal coordination indication ${ind}`);
  const mask = config.sourceNames
    .map((name) => (1n << BigInt(sourceIndex.get(name)!)).toString(36))
    .join(".");
  const counts = k.memo(`sigcoord:v1:vote-counts:${config.inputMask}:${mask}`, () =>
    voteCounts(config, k, stateOf),
  );
  return signalCoordinationStateFromVotes(counts, config.ratio, config.minSources, config.version);
}

/** Short / Normal(General) / Long protection families enabled for the independent coordinator type. */
export function signalCoordinationRanges(
  ranges?: Partial<SignalCoordinationRanges> | null,
  heldTags: Iterable<string> = [],
): SignalCoordinationRanges {
  const held = new Set(heldTags);
  return {
    short: ranges?.short !== false || held.has("sh"),
    normal: ranges?.normal !== false || held.has("gn"),
    long: ranges?.long !== false || held.has("lg"),
  };
}

/** Clone historic protections into each enabled exit-range identity so their PF and order tags stay independent. */
export function signalCoordinationHistory(
  history: Record<string, Protect[]>,
  pairs: ReadonlySet<string>,
  protections: readonly Protect[],
): Record<string, Protect[]> {
  const tags = [...new Set(protections.flatMap((p) => (p.tag ? [p.tag] : [])))];
  const out = { ...history };
  if (!tags.length) return out;
  for (const pair of pairs) {
    const ps = history[pair];
    if (!ps?.length) continue;
    out[pair] = ps.flatMap((p) => (p.tag ? [p] : tags.map((tag) => ({ ...p, tag }))));
  }
  return out;
}

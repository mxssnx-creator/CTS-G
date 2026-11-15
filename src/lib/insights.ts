/** Pure view helpers of the desk insight sections (indications history, strategies, stages, live, Signals). No React here: every figure is built from the
 * engine's statistics and tested in insights.test.ts. */
import type { Execution, LiveClosed, PfBase } from "./live-stats.ts";

export type PackKind = "signals" | "indications" | "general" | "shortrange" | "signal pack" | "extra" | "other";

/** The family a pack belongs to, for grouping: Signals packs are sig-<source>-<range>-t<tf>. */
export function packKind(pack: string): PackKind {
  if (pack.startsWith("sig-")) return "signals";
  if (pack === "indications" || pack === "general" || pack === "shortrange") return pack;
  if (pack.startsWith("extra.")) return "extra";
  if (["trend", "breakout", "reversion", "flow", "multi"].includes(pack)) return "signal pack";
  return "other";
}

/** sig-<source>-<range>-t<tf> -> its parts, null for any other pack. */
export function parseSignalPack(pack: string): { source: string; range: string; tf: number } | null {
  const m = /^sig-(.+)-(short|medium)-t(1|5|15|30)$/.exec(pack);
  return m ? { source: m[1], range: m[2], tf: Number(m[3]) } : null;
}

export type PackRow = {
  pack: string; kind: PackKind; total: number; valid: number; validatedPct: number; basePf: number | null; baseOrders: number;
  unfilteredPf: number | null; open: number;
};

/** One row per pack: Sets, validated share, PF after Base (the selection PF) and before it, open positions. Sorted by validated Sets, then PF. */
export function packRows(pfBase: PfBase | undefined, exec: Execution | undefined): PackRow[] {
  const by = pfBase?.byPack ?? {};
  const open = exec?.open?.byPack ?? {};
  const rows = Object.entries(by).map(([pack, v]) => ({
    pack, kind: packKind(pack), total: v.total ?? 0, valid: v.valid ?? 0, validatedPct: v.validatedPct ?? 0,
    basePf: v.base && v.base.n ? v.base.pf : null, baseOrders: v.base?.n ?? 0,
    unfilteredPf: v.unfiltered && v.unfiltered.n ? v.unfiltered.pf : null, open: open[pack]?.open ?? 0,
  }));
  return rows.sort((a, b) => b.valid - a.valid || (b.basePf ?? 0) - (a.basePf ?? 0) || a.pack.localeCompare(b.pack));
}

export type HistoryPoint = { t: number; pf: number | null; validatedPct: number; base: number | null; main: number | null; real: number | null; realNeg: number | null };

/** The scoring passes as a chart series: PF after all gates, validated share and the PF of each stage's population. */
export function historySeries(pfBase: PfBase | undefined): HistoryPoint[] {
  return (pfBase?.history ?? []).map((h) => ({
    t: h.t, pf: h.n ? h.pf : null, validatedPct: h.validatedPct,
    base: h.stages?.base?.pf ?? null, main: h.stages?.main?.pf ?? null, real: h.stages?.real?.pf ?? null, realNeg: h.stages?.realNeg?.pf ?? null,
  }));
}

export type GroupCell = { pack: string; kind: string; range: string; lane: string; side: string; orders: number; pf: number | null; allow: boolean };

/** The group gate's scopes ("group:<pack>:<kind>:<range>:<lane>[:<side>]") as rows of the strategies matrix. A pack name may itself hold ':'. */
export function groupCells(exec: Execution | undefined): GroupCell[] {
  const out: GroupCell[] = [];
  for (const [key, v] of Object.entries(exec?.groups?.scopes ?? {})) {
    const parts = key.replace(/^group:/, "").split(":");
    if (parts.length < 4) continue;
    const hasSide = ["L", "S", "B"].includes(parts[parts.length - 1]);
    const side = hasSide ? parts.pop()! : "";
    const lane = parts.pop()!;
    const range = parts.pop()!;
    const kind = parts.pop()!;
    out.push({ pack: parts.join(":"), kind, range, lane, side, orders: v.orders, pf: v.pf, allow: v.allow });
  }
  return out.sort((a, b) => b.orders - a.orders || a.pack.localeCompare(b.pack));
}

export type ClosedSplit = { key: string; orders: number; wins: number; pf: number | null; netPct: number };

/** Executed closes grouped by a key (pack family, exit reason ...): orders, wins, PF of the net returns, summed net. Unscored closes (-downtime,
 * unverified) are left out, as the engine scores them nowhere. */
export function splitClosed(rows: LiveClosed[] | undefined, key: (r: LiveClosed) => string): ClosedSplit[] {
  const acc = new Map<string, { orders: number; wins: number; g: number; l: number }>();
  for (const r of rows ?? []) {
    const reason = String(r.reason ?? "");
    if (reason.includes("-downtime") || reason.includes("unverified")) continue;
    const net = Number(r.net_pnl_pct ?? r.pnl_pct ?? 0);
    const k = key(r);
    const a = acc.get(k) ?? { orders: 0, wins: 0, g: 0, l: 0 };
    a.orders += 1;
    if (net > 0) { a.wins += 1; a.g += net; } else a.l -= net;
    acc.set(k, a);
  }
  return [...acc.entries()].map(([k, a]) => ({ key: k, orders: a.orders, wins: a.wins, pf: a.l > 0 ? a.g / a.l : a.g > 0 ? Infinity : null, netPct: a.g - a.l }))
    .sort((x, y) => y.orders - x.orders);
}

export type FunnelStep = { stage: string; sets: number; pct: number; pf: number | null };

/** Base -> Main -> Real -> valid, with the PF of each stage's own population (selection PF, in-sample) next to the executed PF. */
export function stageFunnel(pfBase: PfBase | undefined): FunnelStep[] {
  const st = pfBase?.stages;
  if (!st) return [];
  return (["base", "main", "real", "realNeg", "valid"] as const).map((k) => ({ stage: k, sets: st[k]?.sets ?? 0, pct: st[k]?.pct ?? 0, pf: st[k]?.orders ? st[k].pf : null }));
}

/** Signals packs as a source x (range, timeframe) grid of Sets, validated Sets and PF after Base. */
export function signalGrid(rows: PackRow[]): { sources: string[]; cols: string[]; cell: Record<string, PackRow> } {
  const cell: Record<string, PackRow> = {};
  const sources = new Set<string>();
  const cols = new Set<string>();
  for (const r of rows) {
    const p = parseSignalPack(r.pack);
    if (!p) continue;
    sources.add(p.source);
    const col = `${p.range} ${p.tf}m`;
    cols.add(col);
    cell[`${p.source}|${col}`] = r;
  }
  const order = (c: string) => (c.startsWith("short") ? 0 : 100) + Number(c.split(" ")[1].replace("m", ""));
  return { sources: [...sources].sort(), cols: [...cols].sort((a, b) => order(a) - order(b)), cell };
}

/** A PF for display: "∞" without a loss, "–" without orders. */
export function fmtPf(pf: number | null | undefined): string {
  if (pf == null || Number.isNaN(pf)) return "–";
  if (!Number.isFinite(pf)) return "∞";
  return pf.toFixed(2);
}

/** A colour class for a PF cell: under 1 danger, 1 .. 1.2 neutral, above success. */
export function pfTone(pf: number | null | undefined): "bad" | "flat" | "good" | "none" {
  if (pf == null || Number.isNaN(pf)) return "none";
  return pf < 1 ? "bad" : pf < 1.2 ? "flat" : "good";
}

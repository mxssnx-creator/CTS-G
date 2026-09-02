import type { ReactNode } from "react";
import { lastNCostPf, formatDuration } from "@/lib/analytics";
import type { KindStat, LiveClosed, LiveStats, SideStat, StrategyStat } from "@/lib/live-stats";

const INDICATION_KINDS = ["state", "signals", "active", "direction", "move", "common"] as const;
const STRATEGY_KEYS = ["indications", "general", "block", "trailing", "dca", "exits"] as const;

const KIND_SET = new Set<string>(INDICATION_KINDS);

function kindOf(c: LiveClosed): string {
  const k = String(c.indKind || c.ind_kind || "").toLowerCase();
  if (KIND_SET.has(k)) return k;
  const reason = String(c.reason || "");
  if (reason.startsWith("ind:")) {
    const cand = reason.split(":")[1] || "signals";
    return KIND_SET.has(cand) ? cand : "signals";
  }
  return "";
}

function stratsOf(c: LiveClosed): string[] {
  const keys: string[] = [];
  const pack = String(c.pack || "").toLowerCase();
  if (["indications", "general", "block", "dca"].includes(pack)) keys.push(pack);
  const reason = String(c.reason || "").toLowerCase();
  const head = reason.split(":")[0].split(" ")[0] || "";
  if (head.startsWith("block") || pack === "block") keys.push("block");
  if (head.startsWith("dca") || pack === "dca") keys.push("dca");
  const trail = String(c.trail_key || "");
  if (trail && trail !== "0" && trail !== "off") keys.push("trailing");
  if (["lock", "peak", "rev", "hard", "sl", "tp", "trail"].includes(head) || reason.includes("exit:")) keys.push("exits");
  const kind = kindOf(c);
  if (kind) keys.push("indications");
  else if (pack === "indications") keys.push("indications");
  return [...new Set(keys)];
}

function tapeStats(rows: LiveClosed[]): { n: number; pf: number; wr: number; bySide: Record<string, SideStat> } {
  const pf = lastNCostPf(
    rows.map((r) => ({ pnl: r.pnl, t: r.t, pnl_pct: r.pnl_pct })),
    Math.max(1, rows.length),
  );
  const wins = rows.filter((r) => r.pnl > 0).length;
  const decided = rows.filter((r) => r.pnl !== 0).length;
  const side = (dir: string): SideStat => {
    const sub = rows.filter((r) => (r.side || "").toUpperCase().startsWith(dir[0]));
    const s = lastNCostPf(
      sub.map((r) => ({ pnl: r.pnl, t: r.t, pnl_pct: r.pnl_pct })),
      Math.max(1, sub.length),
    );
    return { n: sub.length, pf: s.ratio, wr: sub.filter((r) => r.pnl > 0).length };
  };
  return {
    n: rows.length,
    pf: pf.ratio,
    wr: decided ? (100 * wins) / decided : 0,
    bySide: { LONG: side("LONG"), SHORT: side("SHORT") },
  };
}

const KIND_HINT: Record<string, string> = {
  state: "RSI / MACD / EMA pack + consensus",
  signals: "1m / 5m / 15m source lanes",
  active: "Outbreak ranges 3 / 5 / 10",
  direction: "Two-window reversal",
  move: "Same-window displacement",
  common: "RSI + MACD + EMA + Bollinger",
};

const STRAT_HINT: Record<string, string> = {
  indications: "Indication pack entries",
  general: "General pack entries",
  block: "Count stack on a live parent",
  trailing: "Arm / give trail family",
  dca: "Adverse-step book",
  exits: "Lock / peak / rev / time / hard",
};

function resolveKindStats(stats: LiveStats | null): Record<string, KindStat> {
  const fromBlob = stats?.byIndication || {};
  const fromGate = stats?.sets?.indGate || stats?.coverage?.indicationGate || {};
  const live = stats?.indications?.kindStats || {};
  const hits = stats?.coverage?.indicationHits || stats?.indications?.typeHits || {};
  const types = stats?.coverage?.indicationTypes || stats?.indications?.types || {};
  const closed = stats?.closed ?? [];
  const byKind: Record<string, LiveClosed[]> = {};
  for (const c of closed) {
    const k = kindOf(c);
    if (k) (byKind[k] ||= []).push(c);
  }
  const out: Record<string, KindStat> = {};
  for (const k of INDICATION_KINDS) {
    const a = fromBlob[k] || {};
    const g = fromGate[k] || {};
    const l = live[k] || {};
    const tape = byKind[k] ? tapeStats(byKind[k]) : null;
    const n = Number(a.n || g.n || tape?.n || 0);
    const pf = Number(a.pf ?? g.pf ?? tape?.pf ?? 0);
    out[k] = {
      kind: k,
      n,
      pf,
      wr: Number(a.wr ?? tape?.wr ?? 0),
      maxDdS: Number(a.maxDdS ?? g.maxDdS ?? 0),
      avgDdS: Number(a.avgDdS ?? g.avgDdS ?? 0),
      netAvg: Number(a.netAvg ?? g.netAvg ?? 0),
      validated: Boolean(a.validated ?? g.validated),
      profitable: Boolean(a.profitable ?? g.profitable),
      ok: a.ok ?? g.ok,
      enabled: types[k] !== false && l.enabled !== false,
      processed: true,
      hits: Number(a.hits ?? l.hits ?? hits[k] ?? 0),
      scanSymbols: Number(a.scanSymbols ?? l.symbols ?? 0),
      scanLong: Number(a.scanLong ?? l.long ?? 0),
      scanShort: Number(a.scanShort ?? l.short ?? 0),
      avgConf: Number(a.avgConf ?? l.avgConf ?? 0),
      avgStrength: Number(a.avgStrength ?? l.avgStrength ?? 0),
      bySide: a.bySide || g.bySide || tape?.bySide,
    };
  }
  return out;
}

function resolveStrategyStats(stats: LiveStats | null): Record<string, StrategyStat> {
  const fromBlob = stats?.byStrategy || {};
  const cov = stats?.coverage?.strategies || {};
  const closed = stats?.closed ?? [];
  const buckets: Record<string, LiveClosed[]> = {};
  for (const c of closed) {
    for (const k of stratsOf(c)) (buckets[k] ||= []).push(c);
  }
  const out: Record<string, StrategyStat> = {};
  for (const k of STRATEGY_KEYS) {
    const a = fromBlob[k] || {};
    const tape = buckets[k] ? tapeStats(buckets[k]) : null;
    out[k] = {
      strategy: k,
      n: Number(a.n || tape?.n || 0),
      pf: Number(a.pf || tape?.pf || 0),
      wr: Number(a.wr || tape?.wr || 0),
      maxDdS: Number(a.maxDdS || 0),
      avgDdS: Number(a.avgDdS || 0),
      netAvg: Number(a.netAvg || 0),
      validated: Boolean(a.validated),
      enabled: a.enabled ?? cov[k] ?? k !== "dca",
      processed: true,
      bySide: a.bySide || tape?.bySide,
    };
  }
  if (!out.block?.n && stats?.block?.lanes?.length) {
    out.block = { ...out.block, n: stats.block.lanes.length, enabled: stats.block.enabled !== false };
  }
  if (!out.dca?.pf && stats?.dca?.last15Ratio != null) {
    out.dca = { ...out.dca, pf: Number(stats.dca.last15Ratio), enabled: Boolean(stats.dca.enabled) };
  }
  return out;
}

function pfTone(pf: number, n: number) {
  if (n < 1) return "text-muted";
  if (pf >= 1.1) return "text-primary";
  if (pf < 1) return "text-danger";
  return "text-fg";
}

function Card({ title, hint, testId, children }: { title: string; hint?: string; testId?: string; children: ReactNode }) {
  return (
    <section className="rounded-radius border border-border bg-surface p-4" data-testid={testId}>
      <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
        <h2 className="text-sm font-medium tracking-wide text-muted uppercase">{title}</h2>
        {hint ? <p className="font-mono text-[11px] text-muted">{hint}</p> : null}
      </div>
      {children}
    </section>
  );
}

export function IndicationKindsPanel({ stats }: { stats: LiveStats | null }) {
  const kinds = resolveKindStats(stats);
  return (
    <Card title="Indication types · PF / DDT" hint="Every type is scored independently · 1.00=neutral after cost" testId="indication-types">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-left text-sm">
          <thead className="font-mono text-[11px] text-muted">
            <tr>
              <th className="pb-2 font-medium">Type</th>
              <th className="pb-2 font-medium">On</th>
              <th className="pb-2 text-right font-medium">Hits</th>
              <th className="pb-2 text-right font-medium">n</th>
              <th className="pb-2 text-right font-medium">PF</th>
              <th className="pb-2 text-right font-medium">L / S</th>
              <th className="pb-2 text-right font-medium">WR</th>
              <th className="pb-2 text-right font-medium">Max DDt</th>
              <th className="pb-2 font-medium">Gate</th>
            </tr>
          </thead>
          <tbody>
            {INDICATION_KINDS.map((k) => {
              const r = kinds[k];
              const lpf = Number(r.bySide?.LONG?.pf ?? 0);
              const spf = Number(r.bySide?.SHORT?.pf ?? 0);
              const ln = Number(r.bySide?.LONG?.n ?? r.scanLong ?? 0);
              const sn = Number(r.bySide?.SHORT?.n ?? r.scanShort ?? 0);
              return (
                <tr key={k} className="border-t border-border font-mono text-xs">
                  <td className="py-1.5">
                    <div className="text-fg">{k}</div>
                    <div className="text-[10px] text-muted">{KIND_HINT[k]}</div>
                  </td>
                  <td className={r.enabled ? "py-1.5 text-primary" : "py-1.5 text-faint"}>{r.enabled ? "on" : "off"}</td>
                  <td className="py-1.5 text-right">
                    {r.hits ?? 0}
                    {r.scanSymbols ? <span className="text-muted"> · {r.scanSymbols}s</span> : null}
                  </td>
                  <td className="py-1.5 text-right">{r.n ?? 0}</td>
                  <td className={`py-1.5 text-right ${pfTone(r.pf ?? 0, r.n ?? 0)}`}>{(r.n ?? 0) ? (r.pf ?? 0).toFixed(2) : "—"}</td>
                  <td className="py-1.5 text-right text-muted">
                    {ln || sn ? `${lpf.toFixed(2)} / ${spf.toFixed(2)}` : "—"}
                  </td>
                  <td className="py-1.5 text-right">{r.n ? `${Number(r.wr ?? 0).toFixed(0)}%` : "—"}</td>
                  <td className="py-1.5 text-right">{r.maxDdS ? formatDuration(Number(r.maxDdS) * 1000) : "—"}</td>
                  <td className={`py-1.5 ${r.ok === false ? "text-danger" : r.validated && r.profitable ? "text-primary" : "text-muted"}`}>
                    {r.ok === false ? "block" : r.validated ? (r.profitable ? "pass" : "fail") : "cold"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

export function StrategyStatsPanel({ stats }: { stats: LiveStats | null }) {
  const rows = resolveStrategyStats(stats);
  return (
    <Card title="Strategies · PF / DDT" hint="Indications · general · block · trail · DCA · exits" testId="strategy-stats">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[640px] text-left text-sm">
          <thead className="font-mono text-[11px] text-muted">
            <tr>
              <th className="pb-2 font-medium">Strategy</th>
              <th className="pb-2 font-medium">On</th>
              <th className="pb-2 text-right font-medium">n</th>
              <th className="pb-2 text-right font-medium">PF</th>
              <th className="pb-2 text-right font-medium">L / S</th>
              <th className="pb-2 text-right font-medium">WR</th>
              <th className="pb-2 text-right font-medium">Max DDt</th>
              <th className="pb-2 font-medium">Status</th>
            </tr>
          </thead>
          <tbody>
            {STRATEGY_KEYS.map((k) => {
              const r = rows[k];
              const lpf = Number(r.bySide?.LONG?.pf ?? 0);
              const spf = Number(r.bySide?.SHORT?.pf ?? 0);
              return (
                <tr key={k} className="border-t border-border font-mono text-xs">
                  <td className="py-1.5">
                    <div className="text-fg">{k}</div>
                    <div className="text-[10px] text-muted">{STRAT_HINT[k]}</div>
                  </td>
                  <td className={r.enabled ? "py-1.5 text-primary" : "py-1.5 text-faint"}>{r.enabled ? "on" : "off"}</td>
                  <td className="py-1.5 text-right">{r.n ?? 0}</td>
                  <td className={`py-1.5 text-right ${pfTone(r.pf ?? 0, r.n ?? 0)}`}>{(r.n ?? 0) ? (r.pf ?? 0).toFixed(2) : "—"}</td>
                  <td className="py-1.5 text-right text-muted">
                    {r.bySide?.LONG || r.bySide?.SHORT ? `${lpf.toFixed(2)} / ${spf.toFixed(2)}` : "—"}
                  </td>
                  <td className="py-1.5 text-right">{r.n ? `${Number(r.wr ?? 0).toFixed(0)}%` : "—"}</td>
                  <td className="py-1.5 text-right">{r.maxDdS ? formatDuration(Number(r.maxDdS) * 1000) : "—"}</td>
                  <td className={`py-1.5 ${r.validated ? "text-primary" : "text-muted"}`}>{r.validated ? "validated" : "building"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

export function KindStrategyStrip({ stats }: { stats: LiveStats | null }) {
  const kinds = resolveKindStats(stats);
  const strats = resolveStrategyStats(stats);
  return (
    <div className="mt-3 space-y-2" data-testid="kind-strategy-strip">
      <div className="rounded-xl border border-border bg-bg2 px-3 py-2 font-mono text-xs">
        <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
          <span className="text-primary">indication types</span>
          <span className="text-muted">hits · PF · DDt · L/S</span>
        </div>
        <div className="flex flex-wrap gap-2">
          {INDICATION_KINDS.map((k) => {
            const r = kinds[k];
            const on = r.enabled !== false;
            return (
              <span
                key={k}
                className={`rounded-md border px-2 py-1 ${on ? "border-border text-fg" : "border-border text-faint"}`}
                title={KIND_HINT[k]}
              >
                {k} {r.hits ?? 0}
                {r.n ? ` · ${(r.pf ?? 0).toFixed(2)}` : ""}
              </span>
            );
          })}
        </div>
      </div>
      <div className="rounded-xl border border-border bg-bg2 px-3 py-2 font-mono text-xs">
        <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
          <span className="text-primary">strategies</span>
          <span className="text-muted">n · PF · DDt</span>
        </div>
        <div className="flex flex-wrap gap-2">
          {STRATEGY_KEYS.map((k) => {
            const r = strats[k];
            return (
              <span key={k} className={`rounded-md border px-2 py-1 ${r.enabled ? "border-border text-fg" : "border-border text-faint"}`}>
                {k} {r.enabled ? "on" : "off"}
                {r.n ? ` · n${r.n} ${(r.pf ?? 0).toFixed(2)}` : ""}
              </span>
            );
          })}
        </div>
      </div>
    </div>
  );
}

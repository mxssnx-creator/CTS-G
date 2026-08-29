import { useEffect, useState, type ReactElement, type ReactNode } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { LiveOpen, LiveStats } from "@/lib/live-stats";
import type { Derived } from "@/lib/derive-stats";

export function ClientChart({
  children,
  height = 220,
}: {
  children: ReactNode;
  height?: number;
}) {
  const [ready, setReady] = useState(false);
  useEffect(() => setReady(true), []);
  if (!ready) return <div className="w-full bg-bg2" style={{ height }} />;
  return (
    <div className="w-full" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        {children as ReactElement}
      </ResponsiveContainer>
    </div>
  );
}

function Tip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: Array<{ value: number; name: string; payload: Record<string, unknown> }>;
  label?: string | number;
}) {
  if (!active || !payload?.length) return null;
  const p = payload[0];
  return (
    <div className="rounded-lg border border-border bg-surface px-2.5 py-1.5 font-mono text-xs">
      <div className="text-muted">{String(label ?? p.payload.symbol ?? "")}</div>
      <div>{Number(p.value).toFixed(4)}</div>
    </div>
  );
}

export function EquityArea({ data }: { data: Derived["equityCurve"] }) {
  if (data.length < 2) {
    return <EmptyChart label="Equity path fills after two closed trades" />;
  }
  const last = data[data.length - 1]?.pnl ?? 0;
  const pos = last >= 0;
  return (
    <ClientChart height={240}>
      <AreaChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
        <defs>
          <linearGradient id="eqFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={pos ? "#3dcf8e" : "#ef6f63"} stopOpacity={0.28} />
            <stop offset="100%" stopColor={pos ? "#3dcf8e" : "#ef6f63"} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke="#1d3a32" vertical={false} />
        <XAxis dataKey="i" hide />
        <YAxis
          tick={{ fill: "#7f9d90", fontSize: 11, fontFamily: "IBM Plex Mono" }}
          width={48}
          tickFormatter={(v) => Number(v).toFixed(3)}
        />
        <Tooltip content={<Tip />} />
        <Area
          type="monotone"
          dataKey="pnl"
          stroke={pos ? "#3dcf8e" : "#ef6f63"}
          strokeWidth={2}
          fill="url(#eqFill)"
        />
      </AreaChart>
    </ClientChart>
  );
}

export function TradeBars({ data }: { data: Derived["tradeBars"] }) {
  if (!data.length) return <EmptyChart label="No closed results yet" />;
  return (
    <ClientChart height={220}>
      <BarChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
        <CartesianGrid stroke="#1d3a32" vertical={false} />
        <XAxis dataKey="symbol" tick={{ fill: "#7f9d90", fontSize: 10 }} interval={0} />
        <YAxis
          tick={{ fill: "#7f9d90", fontSize: 11, fontFamily: "IBM Plex Mono" }}
          width={48}
          tickFormatter={(v) => Number(v).toFixed(3)}
        />
        <Tooltip content={<Tip />} />
        <Bar dataKey="pnl" radius={[4, 4, 0, 0]}>
          {data.map((d, i) => (
            <Cell key={i} fill={d.pnl >= 0 ? "#3dcf8e" : "#ef6f63"} />
          ))}
        </Bar>
      </BarChart>
    </ClientChart>
  );
}

export function SymbolBars({ data }: { data: Derived["bySymbol"] }) {
  if (!data.length) return <EmptyChart label="Symbol results appear after exits" />;
  return (
    <ClientChart height={220}>
      <BarChart data={data} layout="vertical" margin={{ top: 8, right: 12, left: 8, bottom: 0 }}>
        <CartesianGrid stroke="#1d3a32" horizontal={false} />
        <XAxis type="number" tick={{ fill: "#7f9d90", fontSize: 11 }} />
        <YAxis type="category" dataKey="symbol" width={56} tick={{ fill: "#d9f0e6", fontSize: 11 }} />
        <Tooltip content={<Tip />} />
        <Bar dataKey="pnl" radius={[0, 4, 4, 0]}>
          {data.map((d, i) => (
            <Cell key={i} fill={d.pnl >= 0 ? "#3dcf8e" : "#ef6f63"} />
          ))}
        </Bar>
      </BarChart>
    </ClientChart>
  );
}

function EmptyChart({ label }: { label: string }) {
  return (
    <div className="flex h-52 items-center justify-center rounded-lg bg-bg2 px-4 text-center text-sm text-muted">
      {label}
    </div>
  );
}

export function Meter({
  label,
  value,
  max = 100,
  suffix = "%",
  danger,
}: {
  label: string;
  value: number;
  max?: number;
  suffix?: string;
  danger?: boolean;
}) {
  const pct = Math.max(0, Math.min(100, (value / max) * 100));
  return (
    <div>
      <div className="mb-1.5 flex justify-between font-mono text-xs text-muted">
        <span>{label}</span>
        <span className={danger ? "text-danger" : "text-fg"}>
          {value.toFixed(1)}
          {suffix}
        </span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-bg2">
        <div
          className={`h-full rounded-full ${danger ? "bg-danger" : "bg-primary"}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export function WinRing({ wins, losses }: { wins: number; losses: number }) {
  const total = wins + losses;
  const wr = total ? (wins / total) * 100 : 0;
  const r = 42;
  const c = 2 * Math.PI * r;
  const dash = (wr / 100) * c;
  return (
    <div className="flex items-center gap-4">
      <svg viewBox="0 0 100 100" className="size-28 -rotate-90">
        <circle cx="50" cy="50" r={r} fill="none" stroke="#16302a" strokeWidth="8" />
        <circle
          cx="50"
          cy="50"
          r={r}
          fill="none"
          stroke="#3dcf8e"
          strokeWidth="8"
          strokeDasharray={`${dash} ${c - dash}`}
          strokeLinecap="round"
        />
      </svg>
      <div>
        <div className="font-mono text-3xl font-medium tabular-nums">{wr.toFixed(0)}%</div>
        <div className="text-sm text-muted">
          {wins} won · {losses} lost
        </div>
      </div>
    </div>
  );
}

export function SlTpTape({ p }: { p: LiveOpen }) {
  const sl = p.sl;
  const tp = p.tp;
  const mark = p.px ?? p.entry;
  const lo = Math.min(sl, tp, mark, p.entry);
  const hi = Math.max(sl, tp, mark, p.entry);
  const span = hi - lo || 1;
  const x = (v: number) => `${((v - lo) / span) * 100}%`;
  return (
    <div className="relative mt-3 h-8">
      <div className="absolute top-3 right-0 left-0 h-px bg-border" />
      <Tick left={x(sl)} label="SL" tone="danger" />
      <Tick left={x(p.entry)} label="IN" tone="muted" />
      <Tick left={x(mark)} label="PX" tone="fg" />
      <Tick left={x(tp)} label="TP" tone="primary" />
    </div>
  );
}

function Tick({
  left,
  label,
  tone,
}: {
  left: string;
  label: string;
  tone: "danger" | "primary" | "muted" | "fg";
}) {
  const cls =
    tone === "danger"
      ? "text-danger"
      : tone === "primary"
        ? "text-primary"
        : tone === "fg"
          ? "text-fg"
          : "text-muted";
  return (
    <div className={`absolute top-0 -translate-x-1/2 ${cls}`} style={{ left }}>
      <div className="h-2 w-px bg-current" />
      <div className="mt-1 font-mono text-xs leading-none">{label}</div>
    </div>
  );
}

export function BlockHeat({ stats }: { stats: LiveStats }) {
  const lanes = (stats.block?.lanes ?? []).slice(0, 8);
  if (!lanes.length) {
    return <p className="py-6 text-center text-sm text-muted">No Block parent lanes</p>;
  }
  const extra = (stats.block?.lanes?.length ?? 0) - lanes.length;
  return (
    <div className="flex flex-col gap-4">
      {lanes.map((lane) => (
        <div key={lane.symbol + lane.side}>
          <div className="mb-2 flex items-baseline justify-between gap-2">
            <span className="text-sm font-medium">
              {lane.symbol.replace("-USDT", "")} {lane.side}
            </span>
            <span className="font-mono text-xs text-muted">
              base {lane.baseQty} · +{lane.confirmedAdd} → {lane.aggregate}
            </span>
          </div>
          <div className="grid grid-cols-6 gap-1.5 sm:grid-cols-12">
            {lane.counts.slice(0, 12).map((c) => (
              <div
                key={c.n}
                className={`flex aspect-square flex-col items-center justify-center rounded-lg border font-mono text-xs ${
                  c.satisfied
                    ? "border-primary bg-primary-dim/50 text-primary"
                    : c.paused
                      ? "border-border text-faint"
                      : c.pass
                        ? "border-border bg-bg2 text-fg"
                        : "border-danger/40 text-danger"
                }`}
                title={`count ${c.n} minPF ${c.minPF} req ${c.requested}`}
              >
                <span>{c.n}</span>
                <span className="text-xs opacity-70">{c.inc.toFixed(0)}×</span>
              </div>
            ))}
          </div>
        </div>
      ))}
      {extra > 0 ? <p className="font-mono text-xs text-muted">+{extra} block lanes</p> : null}
    </div>
  );
}

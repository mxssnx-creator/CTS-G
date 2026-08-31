import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { Blocks, Plug, Radio, Shield } from "lucide-react";
import { DeskShell } from "@/components/desk-shell";
import { useConnection } from "@/components/connection-provider";
import {
  DEFAULT_OVERLAY,
  fetchCtsBundle,
  loadLocalOverlay,
  overlayFromCts,
  saveOverlay,
  type PulseOverlay,
} from "@/lib/config-model";
import { fetchLiveStats, pickView, type LiveStats } from "@/lib/live-stats";
import {
  LAYERS,
  MODULES,
  applyModule,
  modulesFromOverlay,
  type ModuleStatus,
} from "@/lib/system-catalog";

export const Route = createFileRoute("/system")({ component: SystemPage });

function SystemPage() {
  const { conn } = useConnection();
  const [stats, setStats] = useState<LiveStats | null>(null);
  const [overlay, setOverlay] = useState<PulseOverlay>(DEFAULT_OVERLAY);
  const [dirty, setDirty] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const dirtyRef = useRef(false);
  dirtyRef.current = dirty;

  useEffect(() => {
    let alive = true;
    setStats(null);
    const pull = async () => {
      const [c, s] = await Promise.all([fetchCtsBundle(conn), fetchLiveStats(conn)]);
      if (!alive) return;
      setStats(pickView(s, conn));
      if (!dirtyRef.current) {
        const local = loadLocalOverlay(conn);
        setOverlay(overlayFromCts(c.cts ?? {}, { ...(local || {}), ...(c.overlay || {}) }));
      }
    };
    let timer: ReturnType<typeof setTimeout> | null = null;
    const chain = async () => {
      await pull();
      if (alive) timer = setTimeout(chain, 4000);
    };
    void chain();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [conn]);

  const flags = useMemo(() => modulesFromOverlay(overlay), [overlay]);
  const liveN = MODULES.filter((m) => m.status === "live").length;
  const slotN = MODULES.filter((m) => m.status !== "live").length;

  const toggle = async (id: string, on: boolean) => {
    dirtyRef.current = true;
    const next = applyModule(overlay, id, on);
    setOverlay(next);
    setDirty(true);
    const r = await saveOverlay(next, conn);
    setMsg(r.detail);
    if (r.ok) {
      dirtyRef.current = false;
      setDirty(false);
    }
  };

  return (
    <DeskShell
      live={Boolean(stats?.running && !stats?.halted && !stats?.paused)}
      mode={stats?.paused ? "PAUSED" : stats?.mode}
      paused={Boolean(stats?.paused || stats?.haltReason === "paused")}
    >
      <section className="grid gap-3 sm:grid-cols-3">
        <Stat k="Live packs" v={String(liveN)} s="wired into the core" />
        <Stat k="Extension slots" v={String(slotN)} s="same contract, not attached" />
        <Stat
          k="Load"
          v={String(stats?.engine?.load?.level ?? stats?.coverage?.load?.level ?? "calm")}
          s={`rss ${Math.round(Number(stats?.engine?.load?.rssMb ?? stats?.rssMb ?? 0))}MB · chunk ${stats?.engine?.load?.scanChunk ?? "—"}`}
        />
      </section>

      <section className="rounded-radius border border-border bg-surface p-4 sm:p-5">
        <div className="mb-4 flex items-center gap-2 text-muted">
          <Blocks className="size-4" />
          <h2 className="text-sm font-medium tracking-wide uppercase">Pipeline</h2>
        </div>
        <div className="grid gap-2 sm:grid-cols-7">
          {LAYERS.map((l, i) => (
            <div key={l.id} className="rounded-xl border border-border bg-bg2 px-3 py-3">
              <p className="font-mono text-[11px] text-faint">
                {String(i + 1).padStart(2, "0")}
              </p>
              <p className="mt-1 text-sm font-medium">{l.title}</p>
              <p className="mt-1 text-xs text-muted">{l.blurb}</p>
            </div>
          ))}
        </div>
        <p className="mt-4 text-sm text-muted">
          Core never imports a venue or a pack by name. Feeds, exchange, strategies,
          risk and execution register. Turn a pack off here — the loop keeps running.
        </p>
      </section>

      {LAYERS.map((layer) => {
        const rows = MODULES.filter((m) => m.layer === layer.id);
        const Icon = layer.id === "exchange" ? Plug : layer.id === "feed" ? Radio : layer.id === "risk" ? Shield : Blocks;
        return (
          <section key={layer.id} className="rounded-radius border border-border bg-surface p-4 sm:p-5">
            <div className="mb-4 flex items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <Icon className="size-4 text-muted" />
                <h2 className="text-sm font-medium tracking-wide uppercase">{layer.title}</h2>
              </div>
              <span className="font-mono text-xs text-muted">{rows.length} modules</span>
            </div>
            <ul className="grid gap-2 md:grid-cols-2">
              {rows.map((m) => {
                const on = flags[m.id] !== false;
                const canToggle = Boolean(m.toggle) || m.id === "strategy.coord";
                return (
                  <li
                    key={m.id}
                    className="flex items-start justify-between gap-3 rounded-xl border border-border bg-bg2 px-3 py-3"
                  >
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-medium">{m.name}</span>
                        <StatusChip status={m.status} />
                      </div>
                      <p className="mt-1 text-xs text-muted">{m.summary}</p>
                      <p className="mt-1 font-mono text-[11px] text-faint">{m.id}</p>
                    </div>
                    {canToggle && m.status !== "slot" ? (
                      <button
                        type="button"
                        className={`min-h-11 shrink-0 rounded-full px-3 text-xs font-medium ${
                          on ? "bg-primary-dim text-primary" : "bg-surface2 text-muted"
                        }`}
                        onClick={() => toggle(m.id, !on)}
                      >
                        {on ? "On" : "Off"}
                      </button>
                    ) : (
                      <span className="shrink-0 font-mono text-[11px] text-faint">
                        {m.status === "slot" ? "slot" : "core"}
                      </span>
                    )}
                  </li>
                );
              })}
            </ul>
          </section>
        );
      })}

      {msg ? <p className="text-sm text-muted">{msg}</p> : null}
    </DeskShell>
  );
}

function Stat({ k, v, s }: { k: string; v: string; s: string }) {
  return (
    <div className="rounded-radius border border-border bg-surface p-4">
      <p className="font-mono text-xs tracking-wide text-muted uppercase">{k}</p>
      <p className="mt-1 text-2xl font-medium">{v}</p>
      <p className="mt-1 text-xs text-muted">{s}</p>
    </div>
  );
}

function StatusChip({ status }: { status: ModuleStatus }) {
  const label = status === "live" ? "live" : status === "ready" ? "ready" : "slot";
  const cls =
    status === "live"
      ? "bg-primary-dim/40 text-primary"
      : status === "ready"
        ? "bg-surface2 text-fg"
        : "text-faint border border-border";
  return (
    <span className={`rounded-full px-2 py-0.5 font-mono text-[10px] uppercase ${cls}`}>
      {label}
    </span>
  );
}

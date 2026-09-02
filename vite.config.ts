import { existsSync, readdirSync, readFileSync, writeFileSync, openSync } from "node:fs";
import { spawn } from "node:child_process";
import type { IncomingMessage, ServerResponse } from "node:http";
import { join } from "node:path";
import type { Plugin, ProxyOptions } from "vite";
import { defineConfig } from "vite";
import { tanstackStart } from "@tanstack/react-start/plugin/vite";
import viteReact from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { nitro } from "nitro/vite";
// @ts-expect-error JS plugin alongside the TS vite config
import { grokPwaPlugin } from "./scripts/grok-pwa-plugin.mjs";
// @ts-expect-error JS plugin alongside the TS vite config
import { appEnvPlugin } from "./scripts/app-env-plugin.mjs";
import { isMigrationFile } from "./scripts/migration-plan.mjs";

const PULSE = (process.env.PULSE_URL || "http://127.0.0.1:3015").replace(/\/$/, "");
const CTS = (process.env.CTS_URL || "http://152.53.114.112").replace(/\/$/, "");
const LIVE_ID = "bingx-90fb3a5490fb";
const VST_ID = "bingx-x02";

/** The files `src/lib/db.ts` globs — same directory, same non-recursive scope. */
function hasGlobbedMigrations(root: string): boolean {
  try {
    return readdirSync(join(root, "migrations")).some(isMigrationFile);
  } catch {
    return false;
  }
}

/**
 * Finish PGLite bootstrap during dev-server setup (before traffic). Vite awaits
 * async `configureServer` hooks. Production: `src/lib/db` kicks `ensureDbReady`
 * on import.
 *
 * Vite awaiting the hook puts this on time-to-first-render, so an app with no
 * migrations — no schema to apply — skips it entirely rather than paying for a
 * PGLite instance it never queries.
 */
function pgliteBootstrapPlugin(): Plugin {
  return {
    name: "app-builder:pglite-bootstrap",
    apply: "serve",
    async configureServer(server) {
      if (!hasGlobbedMigrations(server.config.root)) return;
      try {
        const mod = (await server.ssrLoadModule("/src/lib/db.ts")) as {
          ensureDbReady?: () => Promise<void>;
        };
        if (typeof mod.ensureDbReady === "function") {
          await mod.ensureDbReady();
        }
      } catch (err) {
        console.error("[app-builder] DB bootstrap failed:", err);
        throw err;
      }
    },
  };
}

/**
 * Live-preview OAuth popup — handled HERE so the agent never has to create a
 * `/auth/popup` route (and cannot break it by scaffolding a React page that
 * paints the full app shell in the popup).
 *
 * `signIn` (client.ts) opens `/auth/popup?providerId=…` in a top-level window.
 * This middleware runs before TanStack Start, calls `handleAuthPopupRequest`,
 * and returns the 302 / completion HTML. Deployed apps do not use the popup
 * (full-page OAuth redirect), so `apply: "serve"` is enough.
 */
function authPopupPlugin(): Plugin {
  return {
    name: "app-builder:auth-popup",
    apply: "serve",
    configureServer(server) {
      // Register immediately (not in a returned post-hook) so we run BEFORE
      // TanStack Start / the SPA HTML fallback. A model-authored
      // `src/routes/auth/popup.tsx` React page must never win this path.
      server.middlewares.use(async (req, res, next) => {
        try {
          const rawUrl = req.url ?? "";
          const pathOnly = rawUrl.split("?", 1)[0] ?? "";
          if (pathOnly !== "/auth/popup") {
            next();
            return;
          }
          if ((req.method ?? "GET").toUpperCase() !== "GET") {
            res.statusCode = 405;
            res.setHeader("content-type", "text/plain; charset=utf-8");
            res.end("Method Not Allowed");
            return;
          }

          const host = String(
            req.headers["x-forwarded-host"] ?? req.headers.host ?? "localhost:8080",
          );
          const proto = String(
            req.headers["x-forwarded-proto"] ??
              ((req.socket as { encrypted?: boolean } | undefined)?.encrypted ? "https" : "http"),
          );
          const requestHeaders = new Headers();
          for (const [key, value] of Object.entries(req.headers)) {
            if (value === undefined) continue;
            if (Array.isArray(value)) {
              for (const v of value) requestHeaders.append(key, v);
            } else {
              requestHeaders.set(key, value);
            }
          }
          // Ensure Host is the public preview host so Better Auth's dynamic
          // baseURL / redirect_uri match the popup origin.
          if (!requestHeaders.has("host")) requestHeaders.set("host", host);

          const request = new Request(`${proto}://${host}${rawUrl}`, {
            method: "GET",
            headers: requestHeaders,
          });

          const mod = (await server.ssrLoadModule("/src/lib/auth/popup.server.ts")) as {
            handleAuthPopupRequest: (req: Request) => Promise<Response>;
          };
          const response = await mod.handleAuthPopupRequest(request);

          res.statusCode = response.status;
          // Preserve multiple Set-Cookie headers (OAuth state + session).
          const setCookies =
            typeof response.headers.getSetCookie === "function"
              ? response.headers.getSetCookie()
              : [];
          response.headers.forEach((value, key) => {
            if (key.toLowerCase() === "set-cookie") return;
            res.setHeader(key, value);
          });
          for (const cookie of setCookies) {
            res.appendHeader("set-cookie", cookie);
          }
          const body = Buffer.from(await response.arrayBuffer());
          res.end(body);
        } catch (err) {
          console.error("[app-builder] /auth/popup handler failed:", err);
          if (!res.headersSent) {
            res.statusCode = 500;
            res.setHeader("content-type", "text/plain; charset=utf-8");
            res.end("auth popup failed");
          }
        }
      });
    },
  };
}

function jsonRes(res: ServerResponse, status: number, body: unknown) {
  if (res.headersSent) return;
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

function readReqBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on("data", (c) => chunks.push(Buffer.isBuffer(c) ? c : Buffer.from(c)));
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

function laneIds(conn: string): string[] {
  const c = (conn || "").toLowerCase();
  if (c === "vst" || c === "bingx-x02") return [VST_ID];
  if (c === "live" || c === "bingx-x01" || c.includes("90fb")) return [LIVE_ID];
  return [LIVE_ID, VST_ID];
}

function laneLabel(id: string) {
  return id === VST_ID ? "VST" : "Live";
}

async function ctsJson(method: string, path: string, body?: unknown, ms = 12000) {
  const r = await fetch(CTS + path, {
    method,
    headers: { Accept: "application/json", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(ms),
  });
  const json = (await r.json().catch(() => ({}))) as Record<string, unknown>;
  return { ok: r.ok && json.success !== false, status: r.status, json };
}

async function engineStates(id: string) {
  const r = await ctsJson("GET", `/api/connections/${id}/engine-states`, undefined, 8000);
  return r.json;
}

async function connBusy(id: string): Promise<boolean> {
  const st = await engineStates(id);
  if (st.engineRunning) return true;
  const prog = await ctsJson("GET", `/api/connections/progression/${id}`, undefined, 8000);
  const pr = (prog.json.progression || {}) as { phase?: string; progress?: number };
  const phase = String(pr.phase || "");
  if (phase === "live_trading" || phase === "prehistoric_data") return true;
  const status = await ctsJson("GET", "/api/trade-engine/status", undefined, 8000);
  const row = ((status.json.connections as Array<{ id?: string; status?: string; actualRuntimeStatus?: string }>) || []).find(
    (c) => c.id === id,
  );
  const s = String(row?.actualRuntimeStatus || row?.status || "");
  return s === "running" || s === "starting" || s === "queued";
}

async function applyCtsControl(conn: string, action: string): Promise<{ ok: boolean; detail: string }> {
  const ids = laneIds(conn);
  const notes: string[] = [];
  if (action === "start" || action === "resume") {
    const flags = await Promise.all(ids.map(async (id) => ({ id, busy: await connBusy(id) })));
    const cold = flags.filter((f) => !f.busy).map((f) => f.id);
    if (cold.length) {
      try {
        await ctsJson("POST", "/api/trade-engine/start", {}, 8000);
      } catch {
        notes.push("coordinator start timed out");
      }
    }
    if (action === "resume" && ids.length === 2) {
      await ctsJson("POST", "/api/trade-engine/resume", {}, 8000);
    }
    for (const id of ids) {
      if (flags.find((f) => f.id === id)?.busy) {
        notes.push(`${laneLabel(id)} already running`);
        continue;
      }
      await ctsJson("POST", `/api/settings/connections/${id}/live-trade`, { is_live_trade: true }, 8000);
      await ctsJson(
        "POST",
        "/api/trade-engine/quick-start",
        { action: "enable", connectionId: id, liveTrade: true, is_live_trade: true },
        20000,
      );
      await ctsJson("POST", "/api/trade-engine/resume", { connectionId: id }, 8000);
      const after = await engineStates(id);
      notes.push(`${laneLabel(id)} ${after.engineRunning ? "started" : "queued"}`);
    }
    return { ok: true, detail: notes.join(" · ") || "started" };
  }
  if (action === "pause") {
    // Global pause stops VST too — only use it for Overall.
    if (ids.length === 2) {
      const r = await ctsJson("POST", "/api/trade-engine/pause", {}, 8000);
      return { ok: r.ok, detail: String(r.json.message || r.json.error || "paused") };
    }
    for (const id of ids) {
      const r = await ctsJson(
        "POST",
        `/api/settings/connections/${id}/live-trade`,
        { is_live_trade: false },
        8000,
      );
      notes.push(`${laneLabel(id)} entries paused`);
      if (!r.ok) notes.push(String(r.json.error || r.status));
    }
    return { ok: true, detail: notes.join(" · ") };
  }
  if (action === "stop") {
    if (ids.length === 2) {
      await ctsJson("POST", "/api/trade-engine/stop", {}, 12000);
    }
    for (const id of ids) {
      await ctsJson("POST", `/api/settings/connections/${id}/live-trade`, { is_live_trade: false }, 8000);
      await ctsJson(
        "POST",
        "/api/trade-engine/quick-start",
        { action: "disable", connectionId: id },
        15000,
      );
      notes.push(`${laneLabel(id)} stopped`);
    }
    return { ok: true, detail: notes.join(" · ") + " · positions stay on BingX" };
  }
  return { ok: false, detail: "unknown action" };
}

function overlayFile(conn: string): string {
  const id = laneIds(conn)[0] === VST_ID ? "bingx-x02" : "bingx-x01";
  return join(process.cwd(), "server/pulse", `overlay-${id}.json`);
}

function readLiveStats(): Record<string, unknown> {
  return JSON.parse(readFileSync(join(process.cwd(), "public/live-stats.json"), "utf8")) as Record<string, unknown>;
}

function connectionsFallback(): unknown {
  const s = readLiveStats();
  const lanes = (Array.isArray(s.lanes) ? s.lanes : []) as Array<Record<string, unknown>>;
  return {
    selectedDefault: "overall",
    types: [
      {
        type: "overall",
        label: "Overall",
        blurb: "All desks in parallel",
        running: Boolean(s.running),
        openCount: s.openCount ?? lanes.reduce((n, l) => n + Number(l.openCount || 0), 0),
        halted: Boolean(s.halted),
        equity: s.equity,
      },
      ...lanes.map((l) => ({
        type: l.type,
        label: l.label,
        id: l.id,
        unit: l.unit,
        blurb: l.exchange,
        running: Boolean(l.running) && !l.halted,
        halted: l.halted,
        paused: l.paused,
        equity: l.equity,
        openCount: l.openCount,
        alive: l.alive,
        progressPct: l.progressPct,
        progressPhase: l.progressPhase,
        progressReady: l.progressReady,
        haltReason: l.haltReason,
        symbolCount: l.symbolCount,
      })),
    ],
    slots: s.slots || [],
    lanes,
  };
}

function universeFallback(): unknown {
  const p = join(process.cwd(), "server/pulse/universe.json");
  if (existsSync(p)) return JSON.parse(readFileSync(p, "utf8"));
  return { rows: [], count: 0, updated: Math.floor(Date.now() / 1000) };
}

function configFallback(conn: string): unknown {
  if (conn === "overall") {
    return {
      cts: null,
      overlay: null,
      conn: "overall",
      lanes: [
        { type: "live", id: "bingx-x01", overlay: JSON.parse(readFileSync(overlayFile("live"), "utf8")) },
        { type: "vst", id: "bingx-x02", overlay: JSON.parse(readFileSync(overlayFile("vst"), "utf8")) },
      ],
    };
  }
  const file = overlayFile(conn);
  const overlay = existsSync(file) ? JSON.parse(readFileSync(file, "utf8")) : {};
  return { cts: null, overlay, conn };
}

async function tryPulse(method: string, path: string, raw?: string, ms = 1600): Promise<{ status: number; json: unknown } | null> {
  try {
    const r = await fetch(`${PULSE}${path}`, {
      method,
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: method === "GET" ? undefined : raw,
      signal: AbortSignal.timeout(ms),
    });
    const text = await r.text();
    return { status: r.status, json: JSON.parse(text) };
  } catch {
    return null;
  }
}

/** Pulse sidecar first; local overlay + CTS worker if :3015 is down. */
function pulseControlPlugin(): Plugin {
  return {
    name: "pulse-control-fallback",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        const rawUrl = req.url ?? "";
        const pathOnly = rawUrl.split("?", 1)[0] ?? "";
        const method = (req.method ?? "GET").toUpperCase();
        const handled = ["/control.json", "/connections.json", "/config.json", "/connection.json", "/universe.json", "/live-stats.json", "/hist-calc.json"];
        if (!handled.includes(pathOnly)) {
          next();
          return;
        }
        if (pathOnly === "/live-stats.json") {
          // Synced snapshot wins when present; otherwise serve the halted-desk
          // fallback so a fresh clone/dev box never 404s the desk snapshot.
          const snap = join(process.cwd(), "public/live-stats.json");
          if (existsSync(snap)) {
            next();
            return;
          }
          jsonRes(res as ServerResponse, 200, statsFallback("overall"));
          return;
        }
        try {
          const url = new URL(rawUrl, "http://127.0.0.1");
          const conn = url.searchParams.get("conn") || "overall";
          if (pathOnly === "/control.json") {
            if (method !== "POST") {
              jsonRes(res as ServerResponse, 405, { ok: false, detail: "POST only" });
              return;
            }
            const raw = await readReqBody(req);
            let action = "";
            try {
              action = String((JSON.parse(raw || "{}") as { action?: string }).action || "").toLowerCase();
            } catch {
              action = "";
            }
            // systemctl start/stop inside the sidecar can take ~25s — the 1.6s
            // default would fall through to the legacy-CTS fallback on every
            // start/stop and report bogus state. Control calls get 30s.
            const pulse = await tryPulse("POST", `/control.json?conn=${encodeURIComponent(conn)}`, raw || JSON.stringify({ action }), 30000);
            if (pulse) {
              jsonRes(res as ServerResponse, pulse.status, pulse.json);
              return;
            }
            const out = await applyCtsControl(conn, action);
            jsonRes(res as ServerResponse, out.ok ? 200 : 400, {
              ok: out.ok,
              detail: out.detail,
              conn,
              action,
              via: "cts",
            });
            return;
          }
          if (pathOnly === "/connections.json") {
            const pulse = await tryPulse("GET", "/connections.json");
            jsonRes(res as ServerResponse, 200, pulse?.json ?? connectionsFallback());
            return;
          }
          if (pathOnly === "/universe.json") {
            const pulse = await tryPulse("GET", "/universe.json");
            jsonRes(res as ServerResponse, 200, pulse?.json ?? universeFallback());
            return;
          }
          if (pathOnly === "/connection.json") {
            if (method === "GET") {
              const pulse = await tryPulse("GET", `/connection.json?conn=${encodeURIComponent(conn)}`);
              jsonRes(res as ServerResponse, 200, pulse?.json ?? {
                ok: true,
                conn,
                connType: conn,
                connectionType: conn === "vst" ? "vst" : "mainnet",
                connectionMethod: "library",
                exchange: "BingX",
                apiKeyMasked: "",
                apiKeySet: false,
                apiSecretSet: false,
                lastTestStatus: "",
                defaultMainnet: conn !== "vst",
                detail: "sidecar offline",
              });
              return;
            }
            if (method !== "POST") {
              jsonRes(res as ServerResponse, 405, { ok: false, detail: "POST only" });
              return;
            }
            const raw = await readReqBody(req);
            const pulse = await tryPulse("POST", `/connection.json?conn=${encodeURIComponent(conn)}`, raw, 8000);
            if (pulse) {
              jsonRes(res as ServerResponse, pulse.status, pulse.json);
              return;
            }
            jsonRes(res as ServerResponse, 503, { ok: false, detail: "pulse sidecar offline — credentials live in Redis on the desk host" });
            return;
          }
          if (pathOnly === "/hist-calc.json") {
            if (method === "GET") {
              const pulse = await tryPulse("GET", "/hist-calc.json");
              const pj = (pulse?.json ?? null) as { phase?: string; ok?: boolean } | null;
              if (pulse && pulse.status < 400 && pj && (pj.phase || pj.ok)) {
                jsonRes(res as ServerResponse, pulse.status, pulse.json);
                return;
              }
              const local = join(process.cwd(), "server/pulse/hist-calc.json");
              if (existsSync(local)) {
                try {
                  jsonRes(res as ServerResponse, 200, JSON.parse(readFileSync(local, "utf8")));
                  return;
                } catch {
                  /* fall through */
                }
              }
              jsonRes(res as ServerResponse, 200, {
                ok: true,
                phase: "idle",
                pct: 0,
                detail: "no calc yet",
                independent: true,
                rows: [],
                kinds: {},
                bySymbol: [],
              });
              return;
            }
            if (method !== "POST") {
              jsonRes(res as ServerResponse, 405, { ok: false, detail: "POST only" });
              return;
            }
            const raw = await readReqBody(req);
            const pulse = await tryPulse("POST", "/hist-calc.json", raw, 8000);
            const pj = (pulse?.json ?? null) as { phase?: string; ok?: boolean } | null;
            if (pulse && pulse.status < 400 && pj && (pj.phase || pj.ok)) {
              jsonRes(res as ServerResponse, pulse.status, pulse.json);
              return;
            }
            const dir = join(process.cwd(), "server/pulse");
            const reqFile = join(dir, "hist-calc-req.json");
            try {
              writeFileSync(reqFile, raw || "{}");
            } catch {
              /* ignore */
            }
            const seed = {
              ok: true,
              phase: "queued",
              pct: 1,
              detail: "starting independent 20h calc",
              independent: true,
            };
            try {
              writeFileSync(join(dir, "hist-calc.json"), JSON.stringify(seed));
            } catch {
              /* ignore */
            }
            try {
              const logFd = openSync(join(dir, "hist-calc.log"), "a");
              const child = spawn("python3", [join(dir, "hist_calc.py"), "--run", "--req", reqFile], {
                cwd: dir,
                detached: true,
                stdio: ["ignore", logFd, logFd],
                env: { ...process.env, CTS_HIST_CALC_PATH: join(dir, "hist-calc.json") },
              });
              child.unref();
            } catch (err) {
              jsonRes(res as ServerResponse, 500, { ok: false, phase: "error", detail: String(err) });
              return;
            }
            jsonRes(res as ServerResponse, 200, seed);
            return;
          }
          if (pathOnly === "/config.json") {
            if (method === "GET") {
              const pulse = await tryPulse("GET", `/config.json?conn=${encodeURIComponent(conn)}`);
              jsonRes(res as ServerResponse, 200, pulse?.json ?? configFallback(conn));
              return;
            }
            if (method !== "POST") {
              jsonRes(res as ServerResponse, 405, { ok: false, detail: "POST only" });
              return;
            }
            const raw = await readReqBody(req);
            const pulse = await tryPulse("POST", `/config.json?conn=${encodeURIComponent(conn)}`, raw);
            if (pulse) {
              jsonRes(res as ServerResponse, pulse.status, pulse.json);
              return;
            }
            if (conn === "overall") {
              jsonRes(res as ServerResponse, 400, { ok: false, detail: "Pick Live or VST to save" });
              return;
            }
            let body: Record<string, unknown> = {};
            try {
              body = JSON.parse(raw || "{}") as Record<string, unknown>;
            } catch {
              jsonRes(res as ServerResponse, 400, { ok: false, detail: "invalid json" });
              return;
            }
            const overlay =
              body.overlay && typeof body.overlay === "object"
                ? (body.overlay as Record<string, unknown>)
                : body;
            const dest = overlayFile(conn);
            const cur = existsSync(dest) ? (JSON.parse(readFileSync(dest, "utf8")) as Record<string, unknown>) : {};
            const next = { ...cur, ...overlay };
            writeFileSync(dest, JSON.stringify(next, null, 2));
            jsonRes(res as ServerResponse, 200, { ok: true, overlay: next, conn, via: "local" });
            return;
          }
          next();
        } catch (err) {
          jsonRes(res as ServerResponse, 500, {
            ok: false,
            detail: err instanceof Error ? err.message : String(err),
          });
        }
      });
    },
  };
}

function statsFallback(conn: string): Record<string, unknown> {
  /** Full halted-desk payload for when the sidecar is unreachable: the desk
   * renders "halted / sidecar-down" lanes instead of loading forever. */
  let snap: Record<string, unknown> = {};
  try {
    snap = readLiveStats();
  } catch {
    /* no synced snapshot yet */
  }
  const snapLanes = (Array.isArray(snap.lanes) ? snap.lanes : []) as Array<Record<string, unknown>>;
  const mkLane = (type: string, id: string, label: string, unit: string, exchange: string) => {
    const cur = snapLanes.find((l) => l.type === type) ?? {};
    return {
      type,
      id,
      label,
      unit,
      exchange,
      running: false,
      halted: true,
      paused: false,
      alive: false,
      haltReason: "sidecar-down",
      equity: cur.equity ?? 0,
      available: cur.available ?? 0,
      unrealized: cur.unrealized ?? 0,
      openCount: cur.openCount ?? 0,
      wins: cur.wins ?? 0,
      losses: cur.losses ?? 0,
      sessionPnl: cur.sessionPnl ?? 0,
      pf: cur.pf ?? 0,
      scanMs: cur.scanMs ?? 0,
      symbolCount: cur.symbolCount ?? 0,
      errors: 0,
      progressPct: cur.progressPct ?? 0,
      progressPhase: cur.progressPhase ?? "idle",
      progressReady: false,
    };
  };
  const liveLane = mkLane("live", "bingx-x01", "Live", "USDT", "BingX");
  const vstLane = mkLane("vst", "bingx-x02", "VST", "VST", "BingX VST");
  const base: Record<string, unknown> = {
    running: false,
    halted: true,
    paused: false,
    haltReason: "sidecar-down",
    mode: "OFF",
    exchange: "BingX",
    startedAt: 0,
    now: Math.floor(Date.now() / 1000),
    uptimeS: 0,
    equity: 0,
    startEquity: 0,
    available: 0,
    usedMargin: 0,
    unrealized: 0,
    realizedPnl: 0,
    sessionPnl: 0,
    pnlPct: 0,
    drawdownPct: 0,
    wins: 0,
    losses: 0,
    winRate: 0,
    openCount: 0,
    maxOpen: 0,
    symbols: [],
    leverage: 0,
    slPct: 0,
    tpPct: 0,
    targetNotional: 0,
    activityPerMin: 0,
    consecLoss: 0,
    errors: 0,
    lastError: "",
    cycle: 0,
    open: [],
    closed: [],
    signals: [],
    prices: {},
    detail:
      "Live pulse sidecar unreachable. Restart grok-pulse@bingx-x01 on the VPS (SSH). Overlay is ready: all USDT-M, 0=unlimited, Block+DCA multi-add.",
  };
  if (conn === "live") {
    return { ...base, connType: "live", connection: "bingx-x01", unit: "USDT", mode: "LIVE_MAINNET" };
  }
  if (conn === "vst") {
    return { ...base, connType: "vst", connection: "bingx-x02", unit: "VST", mode: "VST_DEMO", exchange: "BingX VST" };
  }
  return {
    ...base,
    connType: "overall",
    connection: "overall",
    unit: "MIXED",
    lanes: [liveLane, vstLane],
    equityLive: liveLane.equity,
    equityVst: vstLane.equity,
    sessionPnlLive: liveLane.sessionPnl,
    sessionPnlVst: vstLane.sessionPnl,
  };
}

function pulseProxy(path: string): Record<string, ProxyOptions> {
  return {
    [path]: {
      target: PULSE,
      changeOrigin: true,
      // A hung sidecar must fail fast into the local fallback below, never
      // stall the desk's polling loop on an open socket.
      timeout: 8000,
      proxyTimeout: 8000,
      configure(proxy) {
        proxy.on("error", (_err, req, res) => {
          const r = res as import("node:http").ServerResponse;
          if (!r || r.headersSent) return;
          if (String(req.url || "").startsWith("/stats.json")) {
            try {
              const body = readFileSync(join(process.cwd(), "public/live-stats.json"), "utf8");
              r.writeHead(200, { "Content-Type": "application/json" });
              r.end(body);
              return;
            } catch {
              /* fall through */
            }
            // No synced snapshot either: answer 200 with a full halted-desk
            // payload so the desk renders halted lanes + identity instead of
            // polling forever behind a "Loading…" placeholder.
            let conn = "overall";
            try {
              conn = new URL(String(req.url || ""), "http://127.0.0.1").searchParams.get("conn") || "overall";
            } catch {
              /* keep overall */
            }
            r.writeHead(200, { "Content-Type": "application/json" });
            r.end(JSON.stringify(statsFallback(conn)));
            return;
          }
          r.writeHead(503, { "Content-Type": "application/json" });
          r.end(
            JSON.stringify({
              ok: false,
              running: false,
              halted: true,
              haltReason: "sidecar-down",
              detail:
                "Live pulse sidecar unreachable. Restart grok-pulse@bingx-x01 on the VPS (SSH). Overlay is ready: all USDT-M, 0=unlimited, Block+DCA multi-add.",
            }),
          );
        });
      },
    },
  };
}

// `0.0.0.0:8080` is the live-preview contract — don't change host/port.
// The dev server starts once `src/router.tsx` and `src/routes/` exist — see
// AGENTS.md § "First scaffold".
export default defineConfig(({ command, isPreview }) => ({
  server: {
    host: "0.0.0.0",
    port: 8080,
    strictPort: true,
    proxy: {
      ...pulseProxy("/stats.json"),
      ...pulseProxy("/stats"),
      ...pulseProxy("/results-export.json"),
      ...pulseProxy("/results-export.md"),
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 8081,
    strictPort: true,
  },
  resolve: { tsconfigPaths: true },
  plugins: [
    pgliteBootstrapPlugin(),
    pulseControlPlugin(),
    // Before tanstackStart so /auth/popup never falls through to the SPA.
    authPopupPlugin(),
    // Dev-only /__app-env, read by scripts/check-auth-invariant.mjs.
    appEnvPlugin(),
    // PWA head + ?install=1 tutorial page; runs before Start/Nitro.
    grokPwaPlugin(),
    tailwindcss(),
    tanstackStart(),
    ...(command === "build" || isPreview
      ? [
          nitro({
            preset: "vercel",
            // Auto-registers server/middleware/* (the PWA install page +
            // manifest + head-tag middleware). Nitro v3 defaults serverDir to
            // false, so removing this silently unwires /?install=1 on deploys.
            serverDir: "./server",
          }),
        ]
      : []),
    viteReact(),
  ],
}));

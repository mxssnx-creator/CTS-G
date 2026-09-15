import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, unlinkSync, writeFileSync } from "node:fs";
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

const PULSE = (process.env.PULSE_URL || "http://152.53.114.112:3102").replace(/\/$/, "");
const CTS = (process.env.CTS_URL || "").replace(/\/$/, "");
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
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  res.end(JSON.stringify(body));
}

function jsonRaw(res: ServerResponse, status: number, raw: string) {
  if (res.headersSent) return;
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
    "Content-Length": Buffer.byteLength(raw),
  });
  res.end(raw);
}

function readReqBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    let oversized = false;
    req.on("data", (c) => {
      if (oversized) return;
      const chunk = Buffer.isBuffer(c) ? c : Buffer.from(c);
      size += chunk.length;
      if (size > 256 * 1024) {
        oversized = true;
        chunks.length = 0;
        reject(new Error("Request exceeds 256 KiB limit"));
      } else chunks.push(chunk);
    });
    req.on("end", () => { if (!oversized) resolve(Buffer.concat(chunks).toString("utf8")); });
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
  if (!CTS) throw new Error("Legacy cross-project fallback is disabled");
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
  let allOk = true;
  if (action === "start" || action === "resume") {
    const flags = await Promise.all(ids.map(async (id) => ({ id, busy: await connBusy(id) })));
    const cold = flags.filter((f) => !f.busy).map((f) => f.id);
    if (cold.length) {
      try {
        const started = await ctsJson("POST", "/api/trade-engine/start", {}, 8000);
        allOk = allOk && started.ok;
      } catch {
        allOk = false;
        notes.push("coordinator start timed out");
      }
    }
    if (action === "resume" && ids.length === 2) {
      const resumed = await ctsJson("POST", "/api/trade-engine/resume", {}, 8000);
      allOk = allOk && resumed.ok;
    }
    for (const id of ids) {
      if (flags.find((f) => f.id === id)?.busy) {
        notes.push(`${laneLabel(id)} already running`);
        continue;
      }
      const liveFlag = await ctsJson("POST", `/api/settings/connections/${id}/live-trade`, { is_live_trade: true }, 8000);
      const quickStart = await ctsJson(
        "POST",
        "/api/trade-engine/quick-start",
        { action: "enable", connectionId: id, liveTrade: true, is_live_trade: true },
        20000,
      );
      const resumed = await ctsJson("POST", "/api/trade-engine/resume", { connectionId: id }, 8000);
      allOk = allOk && liveFlag.ok && quickStart.ok && resumed.ok;
      const after = await engineStates(id);
      const started = Boolean(after.engineRunning);
      allOk = allOk && started;
      notes.push(`${laneLabel(id)} ${started ? "started" : "queued"}`);
    }
    return { ok: allOk, detail: notes.join(" · ") || (allOk ? "started" : "start failed") };
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
      allOk = allOk && r.ok;
      notes.push(`${laneLabel(id)} entries paused`);
      if (!r.ok) notes.push(String(r.json.error || r.status));
    }
    return { ok: allOk, detail: notes.join(" · ") };
  }
  if (action === "stop") {
    if (ids.length === 2) {
      const stopped = await ctsJson("POST", "/api/trade-engine/stop", {}, 12000);
      allOk = allOk && stopped.ok;
    }
    for (const id of ids) {
      const liveFlag = await ctsJson("POST", `/api/settings/connections/${id}/live-trade`, { is_live_trade: false }, 8000);
      const disabled = await ctsJson(
        "POST",
        "/api/trade-engine/quick-start",
        { action: "disable", connectionId: id },
        15000,
      );
      allOk = allOk && liveFlag.ok && disabled.ok;
      notes.push(`${laneLabel(id)} stopped`);
    }
    return { ok: allOk, detail: notes.join(" · ") + " · positions stay on BingX" };
  }
  return { ok: false, detail: "unknown action" };
}

function overlayFile(conn: string): string {
  const id = laneIds(conn)[0] === VST_ID ? "bingx-x02" : "bingx-x01";
  return join(process.cwd(), "server/pulse", `overlay-${id}.json`);
}

function forcedConfigFile(conn: string): string {
  const id = laneIds(conn)[0] === VST_ID || conn === "overall" ? "bingx-x02" : "bingx-x01";
  return join(process.cwd(), "server/pulse", `forced-configs-${id}.json`);
}

function readLocalForced(conn: string): Record<string, unknown> | null {
  const paths = [
    forcedConfigFile(conn),
    join(process.cwd(), "reports/hist-test/forced-configs.json"),
  ];
  for (const file of paths) {
    if (!existsSync(file)) continue;
    try {
      const parsed = JSON.parse(readFileSync(file, "utf8")) as Record<string, unknown>;
      const rows = parsed?.rows;
      if (parsed && Array.isArray(rows) && rows.length) return parsed;
    } catch {
      /* try next */
    }
  }
  return null;
}

function winnerFields(local: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const key of ["succeeded", "missing", "variant", "variantSettings", "bestBySymbol", "forcedBest", "engineMinPf", "hours"]) {
    if (local[key] != null) out[key] = local[key];
  }
  return out;
}

function mergeForcedConfigs(payload: Record<string, unknown>, conn: string): Record<string, unknown> {
  const lane = (conn || "").toLowerCase();
  if (lane === "live" || lane === "bingx-x01" || lane.includes("90fb")) return payload;
  const local = readLocalForced(conn);
  if (!local) return payload;
  const current = payload.forcedConfigs;
  const rows = current && typeof current === "object" ? (current as { rows?: unknown }).rows : null;
  if (Array.isArray(rows) && rows.length) {
    return { ...payload, forcedConfigs: { ...(current as Record<string, unknown>), ...winnerFields(local), rows } };
  }
  return { ...payload, forcedConfigs: local, forcedOnly: payload.forcedOnly ?? true };
}

function readLiveStats(): Record<string, unknown> {
  return loadLiveStatsCache()?.obj ?? {};
}

type LiveStatsFileCache = { mtime: number; obj: Record<string, unknown>; raw: string };
let liveStatsFileCache: LiveStatsFileCache | null = null;
const fallbackJsonCache = new Map<string, string>();
let pulseGetDownUntil = 0;

function liveStatsPath() {
  return join(process.cwd(), "public/live-stats.json");
}

function loadLiveStatsCache(): LiveStatsFileCache | null {
  try {
    const path = liveStatsPath();
    const mtime = statSync(path).mtimeMs;
    if (liveStatsFileCache && liveStatsFileCache.mtime === mtime) return liveStatsFileCache;
    const raw = readFileSync(path, "utf8");
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return liveStatsFileCache;
    liveStatsFileCache = { mtime, obj: parsed as Record<string, unknown>, raw };
    fallbackJsonCache.clear();
    return liveStatsFileCache;
  } catch {
    return liveStatsFileCache;
  }
}

function statsFallbackJson(conn: string): string {
  const cache = loadLiveStatsCache();
  const key = `${conn}:${cache?.mtime ?? 0}`;
  const hit = fallbackJsonCache.get(key);
  if (hit) return hit;
  const json = JSON.stringify({ ...statsFallback(conn), stale: true, sidecar: false });
  fallbackJsonCache.set(key, json);
  if (fallbackJsonCache.size > 8) {
    const first = fallbackJsonCache.keys().next().value;
    if (first) fallbackJsonCache.delete(first);
  }
  return json;
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
  const readOverlay = (lane: string) => {
    const file = overlayFile(lane);
    if (!existsSync(file)) return {};
    try {
      return JSON.parse(readFileSync(file, "utf8")) as Record<string, unknown>;
    } catch {
      return {};
    }
  };
  if (conn === "overall") {
    return {
      cts: null,
      overlay: null,
      conn: "overall",
      lanes: [
        { type: "live", id: "bingx-x01", overlay: readOverlay("live") },
        { type: "vst", id: "bingx-x02", overlay: readOverlay("vst") },
      ],
    };
  }
  return { cts: null, overlay: readOverlay(conn), conn };
}

function mergeOverlayForced(payload: Record<string, unknown>, conn: string): Record<string, unknown> {
  const file = overlayFile(conn === "overall" ? "vst" : conn);
  if (!existsSync(file)) return payload;
  try {
    const local = JSON.parse(readFileSync(file, "utf8")) as Record<string, unknown>;
    if (!local.forcedBest) return payload;
    if (conn === "overall") {
      return payload;
    }
    const overlay = payload.overlay && typeof payload.overlay === "object" ? (payload.overlay as Record<string, unknown>) : {};
    return {
      ...payload,
      overlay: {
        ...overlay,
        forcedSymbols: local.forcedSymbols ?? overlay.forcedSymbols,
        forcedVariant: local.forcedVariant ?? overlay.forcedVariant,
        forcedEligible: local.forcedEligible ?? overlay.forcedEligible,
        forcedBest: local.forcedBest,
        tpPct: local.tpPct ?? overlay.tpPct,
        slPct: local.slPct ?? overlay.slPct,
        slToTpRatio: local.slToTpRatio ?? overlay.slToTpRatio,
      },
    };
  } catch {
    return payload;
  }
}

async function tryPulse(method: string, path: string, raw?: string, ms = 4000): Promise<{ status: number; json: unknown } | null> {
  if (method === "GET" && Date.now() < pulseGetDownUntil) return null;
  try {
    const r = await fetch(`${PULSE}${path}`, {
      method,
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: method === "GET" ? undefined : raw,
      signal: AbortSignal.timeout(ms),
    });
    if (method === "GET" && r.status >= 400) {
      pulseGetDownUntil = Date.now() + 4000;
      return { status: r.status, json: null };
    }
    const text = await r.text();
    try {
      const json = JSON.parse(text);
      if (r.ok) pulseGetDownUntil = 0;
      return { status: r.status, json };
    } catch {
      if (method === "GET") pulseGetDownUntil = Date.now() + 4000;
      return null;
    }
  } catch {
    pulseGetDownUntil = Date.now() + 4000;
    return null;
  }
}

/** Pulse sidecar first; local overlay + CTS worker if :3015 is down. */
function pulseControlPlugin(): Plugin {
  const plugin: Plugin = {
    name: "pulse-control-fallback",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        const rawUrl = req.url ?? "";
        const pathOnly = rawUrl.split("?", 1)[0] ?? "";
        const method = (req.method ?? "GET").toUpperCase();
        const handled = ["/stats.json", "/stats", "/progress.json", "/progress", "/system.json", "/control.json", "/connections.json", "/config.json", "/connection.json", "/universe.json", "/live-stats.json", "/hist-calc.json", "/hist-test.json", "/user-presets.json"];
        if (!handled.includes(pathOnly)) {
          next();
          return;
        }
        if (pathOnly === "/stats.json" || pathOnly === "/stats") {
          // Nitro's preview handler can consume these paths before Vite's
          // generic proxy. Keep the canonical stats route ahead of that handler,
          // just like settings and system telemetry in dev and built preview.
          if (method !== "GET") {
            jsonRes(res as ServerResponse, 405, { ok: false, detail: "GET only" });
            return;
          }
          const result = await tryPulse("GET", rawUrl, undefined, 1500);
          const conn = new URL(rawUrl, "http://127.0.0.1").searchParams.get("conn") || "overall";
          if (result?.status === 200 && result.json && typeof result.json === "object") {
            jsonRes(res as ServerResponse, 200, result.json);
            return;
          }
          jsonRaw(res as ServerResponse, 200, statsFallbackJson(conn));
          return;
        }
        if (pathOnly === "/progress.json" || pathOnly === "/progress") {
          if (method !== "GET") {
            jsonRes(res as ServerResponse, 405, { ok: false, detail: "GET only" });
            return;
          }
          const pulse = await tryPulse("GET", rawUrl, undefined, 8000);
          if (pulse && pulse.status < 400) {
            jsonRes(res as ServerResponse, pulse.status, pulse.json);
            return;
          }
          const conn = new URL(rawUrl, "http://127.0.0.1").searchParams.get("conn") || "overall";
          const fallback = statsFallback(conn);
          const lanes = Array.isArray(fallback.lanes) ? fallback.lanes : [];
          jsonRes(res as ServerResponse, 200, {
            ok: false,
            connection: conn,
            phase: conn === "overall" ? "offline" : String(fallback.progressPhase || "offline"),
            ready: false,
            detail: "pulse sidecar unavailable",
            stale: true,
            lanes,
          });
          return;
        }
        if (pathOnly === "/system.json") {
          if (method !== "GET" && method !== "POST") {
            jsonRes(res as ServerResponse, 405, { ok: false, detail: "Use GET or POST" });
            return;
          }
          const origin = req.headers.origin;
          if (method === "POST" && origin) {
            let sameOrigin = false;
            try { sameOrigin = new URL(origin).host === req.headers.host; } catch { /* Invalid origin is rejected. */ }
            if (!sameOrigin) {
              jsonRes(res as ServerResponse, 403, { ok: false, detail: "Same-origin maintenance required" });
              return;
            }
          }
          let raw: string | undefined;
          try { raw = method === "GET" ? undefined : await readReqBody(req); }
          catch { jsonRes(res as ServerResponse, 413, { ok: false, detail: "Request exceeds bounded payload limit" }); return; }
          const result = await tryPulse(method, rawUrl, raw, 10000);
          jsonRes(res as ServerResponse, result?.status ?? 503, result?.json ?? { ok: false, detail: "System statistics service unavailable" });
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
            // Never forward Start/Stop to a public desk URL. PULSE_URL in this
            // sandbox is often the remote UI (:3102), not the sidecar (:3015).
            const pulseIsSidecar = /:3015\b/.test(PULSE);
            const pulse = pulseIsSidecar
              ? await tryPulse("POST", `/control.json?conn=${encodeURIComponent(conn)}`, raw || JSON.stringify({ action }), 30000)
              : null;
            if (pulse && pulse.status < 400) {
              jsonRes(res as ServerResponse, pulse.status, pulse.json);
              return;
            }
            try {
              const out = await applyCtsControl(conn, action);
              jsonRes(res as ServerResponse, out.ok ? 200 : 400, {
                ok: out.ok,
                detail: out.detail,
                conn,
                action,
                via: "cts",
              });
            } catch {
              jsonRes(res as ServerResponse, 200, {
                ok: false,
                detail: "pulse sidecar offline — control stays local",
                conn,
                action,
                via: "offline",
                halted: true,
              });
            }
            return;
          }
          if (pathOnly === "/connections.json") {
            const pulse = await tryPulse("GET", "/connections.json");
            const body = pulse?.json as { types?: unknown; lanes?: unknown } | null;
            const ok = Boolean(pulse && pulse.status < 400 && body && (Array.isArray(body.types) || Array.isArray(body.lanes)));
            jsonRes(res as ServerResponse, 200, ok ? pulse!.json : connectionsFallback());
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
              const pulse = await tryPulse("GET", `/hist-calc.json?conn=${encodeURIComponent(conn)}`);
              const pj = (pulse?.json ?? null) as { phase?: string; ok?: boolean } | null;
              if (pulse && pulse.status < 400 && pj && (pj.phase || pj.ok)) {
                jsonRes(res as ServerResponse, pulse.status, mergeForcedConfigs(pulse.json as Record<string, unknown>, conn));
                return;
              }
              const laneId = conn === "vst" || conn === "bingx-x02" ? "bingx-x02" : "bingx-x01";
              const local = join(process.cwd(), `server/pulse/hist-calc-${laneId}.json`);
              if (existsSync(local)) {
                try {
                  jsonRes(res as ServerResponse, 200, mergeForcedConfigs(JSON.parse(readFileSync(local, "utf8")) as Record<string, unknown>, conn));
                  return;
                } catch {
                  /* fall through */
                }
              }
              const forced = readLocalForced(conn);
              jsonRes(res as ServerResponse, 200, mergeForcedConfigs({
                ok: true,
                phase: "idle",
                pct: 0,
                detail: "no calc yet",
                connection: conn,
                shared: true,
                independent: false,
                rows: [],
                kinds: {},
                bySymbol: [],
                ...(forced ? { forcedConfigs: forced, forcedOnly: true } : {}),
              }, conn));
              return;
            }
            if (method !== "POST") {
              jsonRes(res as ServerResponse, 405, { ok: false, detail: "POST only" });
              return;
            }
            const raw = await readReqBody(req);
            const pulse = await tryPulse("POST", `/hist-calc.json?conn=${encodeURIComponent(conn)}`, raw, 8000);
            const pj = (pulse?.json ?? null) as { phase?: string; ok?: boolean } | null;
            if (pulse && pulse.status < 400 && pj && (pj.phase || pj.ok)) {
              jsonRes(res as ServerResponse, pulse.status, pulse.json);
              return;
            }
              jsonRes(res as ServerResponse, 503, {
                ok: false,
                phase: "deferred",
                detail: "shared historic lane unavailable",
                connection: conn,
                shared: true,
                independent: false,
              });

            return;
          }
          if (pathOnly === "/hist-test.json") {
            const histLatchDir = () => join(process.cwd(), "reports", "hist-test");
            const writeHistLatch = (kind: "stop" | "pause" | "clear") => {
              const dir = histLatchDir();
              try { mkdirSync(dir, { recursive: true }); } catch { /* ignore */ }
              const stopF = join(dir, "STOP");
              const pauseF = join(dir, "PAUSE");
              if (kind === "stop") {
                try { writeFileSync(stopF, "1"); } catch { /* ignore */ }
                try { unlinkSync(pauseF); } catch { /* ignore */ }
              } else if (kind === "pause") {
                try { writeFileSync(pauseF, "1"); } catch { /* ignore */ }
                try { unlinkSync(stopF); } catch { /* ignore */ }
              } else {
                try { unlinkSync(stopF); } catch { /* ignore */ }
                try { unlinkSync(pauseF); } catch { /* ignore */ }
              }
            };
            const localJob = () => {
              const dest = join(process.cwd(), "public/hist-test.json");
              let job: Record<string, unknown> = { ok: true, phase: "idle", pct: 0, detail: "Ready · 20h historic test · fill until positive count", hours: 20, minPf: 1.1, ready: false, running: false, paused: false, independent: true, symbols: [] };
              if (existsSync(dest)) {
                try { job = { ...job, ...(JSON.parse(readFileSync(dest, "utf8")) as Record<string, unknown>) }; } catch { /* fall through */ }
              }
              const phase = String(job.phase || "");
              if ((job.ready === true || phase === "ready") && phase !== "paused" && phase !== "stopped") {
                const pct = Number(job.pct);
                if (!Number.isFinite(pct) || pct < 99) job.pct = 100;
                const positives = (Array.isArray(job.positive) ? job.positive : Array.isArray(job.symbols) ? job.symbols : []) as unknown[];
                if (job.filled == null && positives.length) job.filled = positives.length;
              }
              const dir = join(process.cwd(), "reports", "hist-test");
              if (existsSync(join(dir, "STOP"))) {
                return { ...job, phase: "stopped", running: false, paused: false, detail: "historic test stopped" };
              }
              if (existsSync(join(dir, "PAUSE"))) {
                const phase = String(job.phase || "");
                const runningPhases = ["queued", "rank", "evaluate", "fetch", "replay", "score"];
                const resumePhase = runningPhases.includes(phase) ? phase : (job.resumePhase || "evaluate");
                return { ...job, phase: "paused", paused: true, running: runningPhases.includes(String(resumePhase)), resumePhase, detail: "historic test paused" };
              }
              return job;
            };
            if (method === "GET") {
              const pulse = await tryPulse("GET", "/hist-test.json");
              const pj = (pulse?.json ?? null) as { phase?: string; ok?: boolean } | null;
              if (pulse && pulse.status < 400 && pj && (pj.phase || pj.ok)) {
                jsonRes(res as ServerResponse, pulse.status, pulse.json);
                return;
              }
              jsonRes(res as ServerResponse, 200, localJob());
              return;
            }
            if (method !== "POST") {
              jsonRes(res as ServerResponse, 405, { ok: false, detail: "POST only" });
              return;
            }
            const raw = await readReqBody(req);
            const pulse = await tryPulse("POST", "/hist-test.json", raw, 8000);
            const pj = (pulse?.json ?? null) as { phase?: string; ok?: boolean } | null;
            if (pulse && pulse.status < 400 && pj && (pj.phase || pj.ok)) {
              jsonRes(res as ServerResponse, pulse.status, pulse.json);
              return;
            }
            let body: Record<string, unknown> = {};
            try { body = JSON.parse(raw || "{}") as Record<string, unknown>; } catch { body = {}; }
            const action = String(body.action || "start").toLowerCase();
            if (action === "stop") {
              writeHistLatch("stop");
              spawn("python3", ["scripts/run_hist_test.py", "--stop"], { cwd: process.cwd(), detached: true, stdio: "ignore" }).unref();
              const job = { ...localJob(), phase: "stopped", running: false, paused: false, detail: "historic test stopped" };
              try { writeFileSync(join(process.cwd(), "public/hist-test.json"), JSON.stringify(job)); } catch { /* ignore */ }
              jsonRes(res as ServerResponse, 200, job);
              return;
            }
            if (action === "pause") {
              writeHistLatch("pause");
              spawn("python3", ["scripts/run_hist_test.py", "--pause"], { cwd: process.cwd(), detached: true, stdio: "ignore" }).unref();
              const job = { ...localJob(), phase: "paused", running: Boolean(localJob().running), paused: true, detail: "historic test paused" };
              try { writeFileSync(join(process.cwd(), "public/hist-test.json"), JSON.stringify(job)); } catch { /* ignore */ }
              jsonRes(res as ServerResponse, 200, job);
              return;
            }
            if (action === "resume") {
              writeHistLatch("clear");
              spawn("python3", ["scripts/run_hist_test.py", "--resume"], { cwd: process.cwd(), detached: true, stdio: "ignore" }).unref();
              const prev = localJob();
              const job = { ...prev, phase: String(prev.resumePhase || prev.phase || "evaluate"), running: true, paused: false, detail: "historic test resumed" };
              try { writeFileSync(join(process.cwd(), "public/hist-test.json"), JSON.stringify(job)); } catch { /* ignore */ }
              jsonRes(res as ServerResponse, 200, job);
              return;
            }
            const hours = Math.max(4, Math.min(64, Math.round(Number(body.hours) || 20)));
            const minPf = Number(body.minPf || body.histTestMinPf || 1.1);
            const count = Math.max(1, Math.min(200, Math.round(Number(body.symbolCap || body.targetCount || body.count) || 20)));
            writeHistLatch("clear");
            const queued = {
              ok: true, phase: "queued", pct: 1, ready: false, running: true, paused: false, independent: true,
              hours, minPf, positivePf: minPf, targetCount: count,
              detail: `queued · ${hours}h · min PF ${minPf} · fill ${count}`,
              symbols: [],
            };
            try { writeFileSync(join(process.cwd(), "public/hist-test.json"), JSON.stringify(queued)); } catch { /* ignore */ }
            spawn("python3", ["scripts/run_hist_test.py", "--hours", String(hours), "--min-pf", String(minPf), "--count", String(count)], {
              cwd: process.cwd(), detached: true, stdio: "ignore",
            }).unref();
            jsonRes(res as ServerResponse, 200, queued);
            return;
          }
          if (pathOnly === "/user-presets.json") {
            const pulsePath = method === "GET" ? "/user-presets.json" : "/user-presets.json";
            if (method === "GET") {
              const pulse = await tryPulse("GET", pulsePath);
              if (pulse && pulse.status < 400 && pulse.json && typeof pulse.json === "object") {
                jsonRes(res as ServerResponse, pulse.status, pulse.json);
                return;
              }
              const local = join(process.cwd(), "server/pulse/user-presets.json");
              if (existsSync(local)) {
                try {
                  jsonRes(res as ServerResponse, 200, JSON.parse(readFileSync(local, "utf8")));
                  return;
                } catch {
                  /* fall through */
                }
              }
              jsonRes(res as ServerResponse, 200, { ok: true, presets: [], system: true, max: 24 });
              return;
            }
            if (method !== "POST") {
              jsonRes(res as ServerResponse, 405, { ok: false, detail: "POST only" });
              return;
            }
            const raw = await readReqBody(req);
            const pulse = await tryPulse("POST", "/user-presets.json", raw, 8000);
            if (pulse && pulse.status < 400 && pulse.json && typeof pulse.json === "object") {
              jsonRes(res as ServerResponse, pulse.status, pulse.json);
              return;
            }
            jsonRes(res as ServerResponse, 503, { ok: false, detail: "pulse sidecar offline — presets save on the desk host" });
            return;
          }
          if (pathOnly === "/config.json") {
            if (method === "GET") {
              const pulse = await tryPulse("GET", `/config.json?conn=${encodeURIComponent(conn)}`);
              const body = pulse?.json as Record<string, unknown> | null;
              const ok = Boolean(
                pulse &&
                  pulse.status < 400 &&
                  body &&
                  typeof body === "object" &&
                  (body.overlay != null || body.cts != null || Array.isArray(body.lanes)),
              );
              jsonRes(res as ServerResponse, 200, mergeOverlayForced((ok ? body : configFallback(conn)) as Record<string, unknown>, conn));
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
  plugin.configurePreviewServer = plugin.configureServer as Plugin["configurePreviewServer"];
  return plugin;
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
    return {
      ...base,
      ...snap,
      ...liveLane,
      connType: "live",
      connection: "bingx-x01",
      unit: "USDT",
      mode: "LIVE_MAINNET",
      halted: true,
      running: false,
      paused: false,
      haltReason: "sidecar-down",
      stale: true,
    };
  }
  if (conn === "vst") {
    return {
      ...base,
      ...snap,
      ...vstLane,
      connType: "vst",
      connection: "bingx-x02",
      unit: "VST",
      mode: "VST_DEMO",
      exchange: "BingX VST",
      halted: true,
      running: false,
      paused: false,
      haltReason: "sidecar-down",
      stale: true,
    };
  }
  return {
    ...snap,
    ...base,
    ...snap,
    connType: "overall",
    connection: "overall",
    unit: String(snap.unit || "MIXED"),
    lanes: [liveLane, vstLane],
    equityLive: liveLane.equity,
    equityVst: vstLane.equity,
    sessionPnlLive: liveLane.sessionPnl,
    sessionPnlVst: vstLane.sessionPnl,
    halted: true,
    haltReason: "sidecar-down",
    running: false,
    stale: true,
  };
}

function reportFallbackHtml(conn: string): string {
  const stats = statsFallback(conn);
  const escapeHtml = (value: unknown) => {
    const entities: Record<string, string> = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    };
    return String(value ?? "").replace(/[&<>"']/g, (character) => entities[character] || character);
  };
  const connection = escapeHtml(stats.connection || conn || "overall");
  const detail = escapeHtml(stats.detail || "The pulse sidecar is not responding.");
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CTS-G · stats waiting</title><style>:root{--bg:#07110e;--panel:#0f221c;--text:#d9f0e6;--muted:#7f9d90;--accent:#3dcf8e;color-scheme:dark;font:15px/1.5 system-ui,sans-serif}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text)}main{max-width:720px;margin:auto;padding:32px 20px}.label{color:var(--muted);font:11px ui-monospace,monospace;letter-spacing:.14em;text-transform:uppercase}section{margin-top:20px;border:1px solid color-mix(in srgb,var(--muted) 30%,var(--bg));background:var(--panel);border-radius:12px;padding:20px}h1{font-size:clamp(26px,6vw,44px);line-height:1.1;letter-spacing:-.04em;margin:10px 0}p{color:var(--muted)}strong{color:var(--accent);font:600 22px ui-monospace,monospace}</style></head><body><main><div class="label">CTS-G · canonical live stats export</div><h1>Stats report waiting</h1><section><p>The preview can display the report as soon as the pulse sidecar responds. No orders or state changes are performed by this page.</p><p>Connection: <strong>${connection}</strong></p><p>${detail}</p></section></main></body></html>`;
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
          const requestUrl = String(req.url || "");
          if (requestUrl.startsWith("/results-export.html")) {
            let conn = "overall";
            try {
              conn = new URL(requestUrl, "http://127.0.0.1").searchParams.get("conn") || "overall";
            } catch {
              /* keep overall */
            }
            const body = reportFallbackHtml(conn);
            r.writeHead(503, {
              "Content-Type": "text/html; charset=utf-8",
              "Content-Length": Buffer.byteLength(body),
            });
            r.end(body);
            return;
          }
          if (requestUrl.startsWith("/stats.json")) {
            try {
              const conn = new URL(requestUrl, "http://127.0.0.1").searchParams.get("conn") || "overall";
              jsonRaw(r, 200, statsFallbackJson(conn));
              return;
            } catch {
              jsonRaw(r, 200, statsFallbackJson("overall"));
              return;
            }
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
      ...pulseProxy("/results-export.html"),
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 8081,
    strictPort: true,
    proxy: {
      ...pulseProxy("/stats.json"),
      ...pulseProxy("/stats"),
      ...pulseProxy("/results-export.json"),
      ...pulseProxy("/results-export.md"),
      ...pulseProxy("/results-export.html"),
    },
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

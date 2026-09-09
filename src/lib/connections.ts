import { requestJson, requestPreferredJson } from "./request-json.ts";

export type ConnType = "overall" | "live" | "vst";

export type ConnLane = {
  type: ConnType | string;
  id?: string;
  label: string;
  unit?: string;
  blurb?: string;
  running?: boolean;
  halted?: boolean;
  equity?: number;
  openCount?: number;
  exchangeOpenCount?: number;
  simOpenCount?: number;
  simUPnl?: number;
  alive?: boolean;
  exchange?: string;
  wins?: number;
  losses?: number;
  pf?: number;
  sessionPnl?: number;
  systemPnl?: number;
  systemGrow?: number;
  systemLoss?: number;
  haltReason?: string;
  paused?: boolean;
  progressPct?: number;
  progressPhase?: string;
  progressReady?: boolean;
  hotMs?: number;
  pfCost?: number;
  controlsOk?: number;
  controlsMissing?: number;
  entryPolicy?: string;
  executionEvidence?: Record<string, unknown>;
  symbolCount?: number;
};

export type ConnCatalog = {
  selectedDefault: string;
  types: ConnLane[];
  slots: Array<{ type: string; label: string; ready?: boolean }>;
  lanes: ConnLane[];
};

export type ControlLaneState = {
  connection: string;
  unit?: string;
  state?: string;
  serviceActive?: boolean;
  running?: boolean;
  paused?: boolean;
  stopped?: boolean;
  haltReason?: string | null;
  statsAgeS?: number;
};

export type ControlResult = {
  ok: boolean;
  detail: string;
  conn?: string;
  action?: "start" | "stop" | "pause" | "resume" | string;
  state?: ControlLaneState;
  lanes?: ControlLaneState[];
};

const KEY = "pulse.connType";
let mem: ConnType | null = null;

export function sessionConn(): ConnType {
  return mem ?? "overall";
}

export function readStoredConn(): ConnType {
  if (mem === "live" || mem === "vst" || mem === "overall") return mem;
  try {
    const v = localStorage.getItem(KEY);
    if (v === "live" || v === "vst" || v === "overall") {
      mem = v;
      return v;
    }
  } catch {
    /* ssr */
  }
  return "overall";
}

export function storeConn(v: ConnType) {
  mem = v;
  try {
    localStorage.setItem(KEY, v);
  } catch {
    /* ignore */
  }
}

export async function fetchConnections(signal?: AbortSignal): Promise<ConnCatalog | null> {
  return requestPreferredJson("/connections.json", "/live-stats.json", (value) => {
    const live = value as ConnCatalog;
    if (Array.isArray(live.types) && live.types.length) return live;
    const s = value as { lanes?: ConnLane[]; slots?: ConnCatalog["slots"]; running?: boolean; openCount?: number; equity?: number };
    if (typeof s.running !== "boolean" || !Array.isArray(s.lanes)) return null;
    const lanes = Array.isArray(s.lanes) ? s.lanes : [];
    const types: ConnLane[] = [
      {
        type: "overall",
        label: "Overall",
        unit: "MIXED",
        running: Boolean(s.running),
        equity: s.equity,
        openCount: s.openCount,
        alive: true,
      },
      ...lanes.map((l) => ({
        ...l,
        type: l.type,
        label: l.label,
        running: l.running,
        equity: l.equity,
        openCount: l.openCount,
        alive: l.alive,
      })),
    ];
    return { selectedDefault: "overall", types, slots: s.slots || [], lanes };
  }, signal);
}

export function statsUrl(conn: ConnType | string) {
  return `/stats.json?conn=${encodeURIComponent(conn)}`;
}

export function configUrl(conn: ConnType | string) {
  return `/config.json?conn=${encodeURIComponent(conn)}`;
}

export type ConnectionCreds = {
  ok?: boolean;
  conn?: string;
  connType?: string;
  connectionType?: string;
  connectionMethod?: string;
  exchange?: string;
  baseUrl?: string;
  isTestnet?: boolean;
  liveTradeEnabled?: boolean;
  apiKeyMasked?: string;
  apiKeySet?: boolean;
  apiSecretSet?: boolean;
  lastTestStatus?: string;
  defaultMainnet?: boolean;
  detail?: string;
};

export function connectionUrl(conn: ConnType | string) {
  return `/connection.json?conn=${encodeURIComponent(conn)}`;
}

export async function fetchConnection(conn: ConnType | string, signal?: AbortSignal): Promise<ConnectionCreds | null> {
  const j = (await requestJson(connectionUrl(conn), signal, 2500)) as ConnectionCreds | null;
  return j && typeof j === "object" ? j : null;
}

export async function saveConnection(
  conn: ConnType | string,
  body: {
    api_key?: string;
    api_secret?: string;
    connection_type?: string;
    connection_method?: string;
    as_default_mainnet?: boolean;
  },
): Promise<{ ok: boolean; detail: string; creds?: ConnectionCreds }> {
  try {
    const r = await fetch(connectionUrl(conn), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = (await r.json().catch(() => ({}))) as ConnectionCreds & { ok?: boolean; detail?: string };
    return {
      ok: r.ok && Boolean(j.ok),
      detail: String(j.detail || (r.ok ? "saved" : r.status)),
      creds: j,
    };
  } catch (e) {
    return { ok: false, detail: String(e) };
  }
}

export async function postControl(
  conn: ConnType | string,
  action: "start" | "stop" | "pause" | "resume",
): Promise<ControlResult> {
  try {
    const r = await fetch(`/control.json?conn=${encodeURIComponent(conn)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    const j = (await r.json().catch(() => ({}))) as Partial<ControlResult>;
    const lanes = Array.isArray(j.lanes) ? j.lanes : undefined;
    return {
      ok: r.ok && Boolean(j.ok),
      detail: String(j.detail || (r.ok ? `${action} accepted` : r.status)),
      conn: j.conn,
      action: j.action || action,
      state: j.state,
      lanes,
    };
  } catch (error) {
    return { ok: false, detail: String(error), conn, action };
  }
}

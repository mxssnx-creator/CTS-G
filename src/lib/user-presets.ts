import { syncOverlayFlags, type PulseOverlay } from "@/lib/config-model";
import type { HistCalcOptions } from "@/lib/hist-calc";

export type UserPreset = {
  id: string;
  name: string;
  hint: string;
  overview: string;
  overlay: Partial<PulseOverlay>;
  calcOpt?: Partial<HistCalcOptions>;
  updated?: number;
  created?: number;
  system?: boolean;
};

const LOCAL_KEY = "x01-user-presets";
export const DEFAULT_PRESET_ID = "up-default";

function isDefaultPreset(p: Pick<UserPreset, "id" | "name">): boolean {
  return p.id === DEFAULT_PRESET_ID || p.name.trim().toLowerCase() === "default";
}

export function overlayOverview(ov: Partial<PulseOverlay> | PulseOverlay): string {
  const sl = Number(ov.slToTpRatio);
  const slS = Number.isFinite(sl) ? sl.toFixed(1) : "—";
  const lo = ov.setMinStep;
  const hi = ov.setStepMax;
  const step = lo != null && hi != null ? `${lo}–${hi}` : "—";
  const trail = ov.stratTrailing === false ? "off" : `${ov.trailArmPct ?? "—"}:${ov.trailGivePct ?? "—"}`;
  const block = ov.blockEnabled !== false && ov.stratBlock !== false ? "Block ON" : "Block OFF";
  const dca = ov.dcaEnabled && ov.stratDca !== false ? "DCA ON" : "DCA OFF";
  const pf = Number(ov.setMinPf ?? ov.minPf);
  const pfS = Number.isFinite(pf) && pf > 0 ? pf.toFixed(2) : "—";
  const dd = Number(ov.setMaxDdTimeS ?? ov.maxDdTimeS);
  const ddS = Number.isFinite(dd) ? `${Math.round(dd)}s` : "—";
  return `SL ${slS} · step ${step} · trail ${trail} · ${block} · ${dca} · PF ${pfS} · DDt ${ddS}`;
}

export function suggestPresetName(existing: UserPreset[]): string {
  const used = new Set(existing.map((p) => p.name));
  let n = 1;
  while (used.has(`Preset-${n}`)) n += 1;
  return `Preset-${n}`;
}

export function applyUserPreset(base: PulseOverlay, preset: UserPreset): PulseOverlay {
  return syncOverlayFlags({ ...base, ...(preset.overlay || {}) });
}

function readLocal(): UserPreset[] {
  try {
    const raw = localStorage.getItem(LOCAL_KEY);
    if (!raw) return [];
    const j = JSON.parse(raw) as { presets?: UserPreset[] } | UserPreset[];
    const rows = Array.isArray(j) ? j : j.presets;
    return Array.isArray(rows) ? rows.filter((r) => r && r.id && r.name) : [];
  } catch {
    return [];
  }
}

function writeLocal(rows: UserPreset[]) {
  try {
    localStorage.setItem(LOCAL_KEY, JSON.stringify({ presets: rows, system: true }));
  } catch {
    /* ignore */
  }
}

async function post(body: Record<string, unknown>): Promise<{
  ok: boolean;
  detail: string;
  preset?: UserPreset;
  presets?: UserPreset[];
  overlay?: Partial<PulseOverlay>;
  calcOpt?: Partial<HistCalcOptions>;
  applied?: string[];
}> {
  try {
    const r = await fetch("/user-presets.json", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = (await r.json().catch(() => ({}))) as {
      ok?: boolean;
      detail?: string;
      preset?: UserPreset;
      presets?: UserPreset[];
      overlay?: Partial<PulseOverlay>;
      calcOpt?: Partial<HistCalcOptions>;
      applied?: string[];
    };
    return {
      ok: Boolean(r.ok && j.ok !== false),
      detail: String(j.detail || (r.ok ? "ok" : `HTTP ${r.status}`)),
      preset: j.preset,
      presets: j.presets,
      overlay: j.overlay,
      calcOpt: j.calcOpt,
      applied: j.applied,
    };
  } catch (e) {
    return { ok: false, detail: e instanceof Error ? e.message : "preset request failed" };
  }
}

export async function fetchUserPresets(): Promise<UserPreset[]> {
  try {
    const r = await fetch("/user-presets.json", { cache: "no-store" });
    if (r.ok) {
      const j = (await r.json()) as { presets?: UserPreset[] };
      if (Array.isArray(j.presets)) {
        writeLocal(j.presets);
        return j.presets;
      }
    }
  } catch {
    /* sidecar down */
  }
  return readLocal();
}

export async function saveUserPreset(args: {
  name?: string;
  overlay: PulseOverlay;
  calcOpt?: Partial<HistCalcOptions>;
  id?: string;
}): Promise<{ ok: boolean; detail: string; preset?: UserPreset; presets: UserPreset[] }> {
  const local = readLocal();
  const r = await post({
    action: "save",
    name: args.name || "",
    overlay: args.overlay,
    calcOpt: args.calcOpt || {},
    id: args.id || "",
  });
  if (r.ok && r.presets) {
    writeLocal(r.presets);
    return { ok: true, detail: r.detail, preset: r.preset, presets: r.presets };
  }
  const now = Date.now() / 1000;
  const overview = overlayOverview(args.overlay);
  const name = (args.name || "").trim().startsWith("Preset-")
    ? (args.name || "").trim()
    : args.name?.trim()
      ? `Preset-${args.name.trim()}`
      : suggestPresetName(local);
  const row: UserPreset = {
    id: args.id || `up-local-${Math.random().toString(36).slice(2, 10)}`,
    name,
    hint: overview,
    overview,
    overlay: args.overlay,
    calcOpt: args.calcOpt,
    created: now,
    updated: now,
    system: true,
  };
  const presets = args.id ? local.map((p) => (p.id === args.id ? row : p)) : [...local, row];
  writeLocal(presets);
  return { ok: true, detail: `saved ${row.name} (local)`, preset: row, presets };
}

export async function renameUserPreset(id: string, name: string) {
  const r = await post({ action: "rename", id, name });
  if (r.ok && r.presets) {
    writeLocal(r.presets);
    return { ok: true, detail: r.detail, preset: r.preset, presets: r.presets };
  }
  const local = readLocal();
  const presets = local.map((p) => {
    if (p.id !== id) return p;
    const n = name.trim().toLowerCase().startsWith("preset-") ? name.trim() : `Preset-${name.trim() || p.name}`;
    return { ...p, name: n, updated: Date.now() / 1000 };
  });
  writeLocal(presets);
  return { ok: true, detail: "renamed (local)", presets };
}

export async function deleteUserPreset(id: string) {
  const local = readLocal();
  if (local.some((p) => p.id === id && isDefaultPreset(p))) {
    return { ok: false, detail: "Default is protected", presets: local };
  }
  const r = await post({ action: "delete", id });
  if (r.ok && r.presets) {
    writeLocal(r.presets);
    return { ok: true, detail: r.detail, presets: r.presets };
  }
  const presets = local.filter((p) => p.id !== id);
  writeLocal(presets);
  return { ok: true, detail: "deleted (local)", presets };
}

export async function saveDefaultUserPreset(args: {
  overlay: PulseOverlay;
  calcOpt?: Partial<HistCalcOptions>;
}): Promise<{ ok: boolean; detail: string; preset?: UserPreset; presets: UserPreset[] }> {
  const r = await post({
    action: "save_default",
    name: "Default",
    id: DEFAULT_PRESET_ID,
    overlay: args.overlay,
    calcOpt: args.calcOpt || {},
  });
  if (r.ok && r.presets) {
    writeLocal(r.presets);
    return { ok: true, detail: r.detail, preset: r.preset, presets: r.presets };
  }
  const local = readLocal();
  const now = Date.now() / 1000;
  const old = local.find(isDefaultPreset);
  const row: UserPreset = {
    id: DEFAULT_PRESET_ID,
    name: "Default",
    hint: overlayOverview(args.overlay),
    overview: overlayOverview(args.overlay),
    overlay: args.overlay,
    calcOpt: args.calcOpt,
    created: old?.created ?? now,
    updated: now,
    system: true,
  };
  const presets = old ? local.map((p) => (isDefaultPreset(p) ? row : p)) : [row, ...local];
  writeLocal(presets);
  return { ok: true, detail: "saved Default (local)", preset: row, presets };
}

export async function deleteAllExceptDefaultUserPresets(): Promise<{ ok: boolean; detail: string; presets: UserPreset[] }> {
  const r = await post({ action: "delete_except_default" });
  if (r.ok && r.presets) {
    writeLocal(r.presets);
    return { ok: true, detail: r.detail, presets: r.presets };
  }
  const local = readLocal();
  const keep = local.find(isDefaultPreset);
  if (!keep) return { ok: false, detail: "Save the Default preset before cleanup", presets: local };
  const row = { ...keep, id: DEFAULT_PRESET_ID, name: "Default" };
  const presets = [row];
  writeLocal(presets);
  return { ok: true, detail: "deleted all presets except Default (local)", presets };
}

export async function loadUserPreset(id: string) {
  const r = await post({ action: "load", id, applyAll: true });
  if (r.ok && r.preset) {
    if (r.presets) writeLocal(r.presets);
    return {
      ok: true,
      detail: r.detail,
      preset: r.preset,
      overlay: r.overlay || r.preset.overlay,
      calcOpt: r.calcOpt || r.preset.calcOpt,
      applied: r.applied || [],
    };
  }
  const local = readLocal().find((p) => p.id === id);
  if (!local) return { ok: false, detail: "preset not found", preset: undefined };
  return {
    ok: true,
    detail: `loaded ${local.name}`,
    preset: local,
    overlay: local.overlay,
    calcOpt: local.calcOpt,
    applied: [] as string[],
  };
}

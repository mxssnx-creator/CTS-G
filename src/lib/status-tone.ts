/** Red is reserved for real faults (crash, missing protection, recon fail, lastError, test fail). */

export function pnlClass(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n) || n === 0) return "text-muted";
  return n > 0 ? "text-primary" : "text-muted";
}

export function pfClass(ratio: number, n = 1, minPf = 1.1): string {
  if (n < 1) return "text-muted";
  if (ratio + 1e-9 >= minPf) return "text-primary";
  if (ratio >= 1) return "text-fg";
  return "text-muted";
}

export function activeClass(on: boolean): string {
  return on ? "text-primary" : "text-muted";
}

export function sideChipClass(side: string): string {
  const long = String(side || "").toUpperCase() === "LONG";
  return long ? "bg-primary-dim/40 text-primary" : "bg-bg2 text-fg";
}

export function isEquityHalt(reason?: string | null): boolean {
  const r = String(reason || "").toLowerCase();
  return r.includes("below min") || r.includes("equity") || r.includes("drawdown");
}

export function haltClass(reason?: string | null, halted?: boolean): string {
  if (!halted && !reason) return "";
  const r = String(reason || "").toLowerCase();
  if (isEquityHalt(r) || r.includes("paused") || r.includes("stopped")) return "text-warn";
  return "text-danger";
}

export function engineDotClass(opts: {
  running?: boolean;
  halted?: boolean;
  paused?: boolean;
  alive?: boolean;
}): string {
  if (opts.paused) return "bg-warn";
  if (opts.running && !opts.halted) return "bg-primary";
  if (opts.halted && opts.alive !== false) return "bg-warn";
  if (opts.running == null && opts.halted == null) return "bg-faint";
  return "bg-danger";
}

export function engineStatusLabel(opts: {
  running?: boolean;
  halted?: boolean;
  paused?: boolean;
  mode?: string;
}): { text: string; sub: string } {
  if (opts.paused) return { text: "PAUSE", sub: "PAUSED" };
  if (opts.running && !opts.halted) return { text: "LIVE", sub: opts.mode ?? "LIVE" };
  if (opts.halted) return { text: "HALT", sub: opts.mode ?? "HALTED" };
  if (opts.running == null && opts.halted == null) return { text: "…", sub: "CONNECTING" };
  return { text: "OFFLINE", sub: "OFFLINE" };
}

export function isBenignError(msg?: string | null): boolean {
  const s = String(msg || "").toLowerCase();
  if (!s) return true;
  return s.includes("no position to close") || s.includes("already-flat") || s.includes("position not exist");
}

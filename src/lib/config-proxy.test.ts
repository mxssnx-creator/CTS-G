import assert from "node:assert/strict";
import { test } from "node:test";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Readable } from "node:stream";

type Handler = (req: IncomingMessage, res: ServerResponse, next: () => void) => Promise<void> | void;

// The engine's own overlay (its data directory), served by the fake sidecar.
const engineOverlay = { tpPct: 0.9, slPct: 0.5, slToTpRatio: 0.8, symbols: ["BTC-USDT"] };
// A stale checkout copy with forced winners, as shipped in server/pulse/.
const checkout = { tpPct: 0.7, slPct: 0.42, slToTpRatio: 0.3, forcedSymbols: ["XRP-USDT"], forcedBest: { "XRP-USDT": { tpPct: 0.7 } } };

async function desk() {
  const sidecar = createServer((req, res) => {
    if (req.method === "POST") {
      req.socket.destroy(); // sidecar unavailable for saves
      return;
    }
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ cts: null, overlay: engineOverlay, conn: "vst" }));
  });
  await new Promise<void>((resolve) => sidecar.listen(0, "127.0.0.1", resolve));
  process.env.PULSE_URL = `http://127.0.0.1:${(sidecar.address() as AddressInfo).port}`;
  // A non-literal specifier keeps the desk server config out of the app's type program.
  const viteConfig = "../../vite.config.ts";
  const mod = (await import(viteConfig)) as { default: (env: Record<string, unknown>) => { plugins: unknown[] } };
  const config = mod.default({ command: "serve", mode: "development", isPreview: false });
  const plugin = config.plugins.flat().find((p) => (p as { name?: string })?.name === "pulse-control-fallback") as {
    configureServer: (server: unknown) => void;
  };
  let handler: Handler | null = null;
  plugin.configureServer({ middlewares: { use: (fn: Handler) => { handler = fn; } }, config: { root: process.cwd() } });
  const root = mkdtempSync(join(tmpdir(), "cts-desk-"));
  mkdirSync(join(root, "server/pulse"), { recursive: true });
  const file = join(root, "server/pulse/overlay-bingx-x02.json");
  writeFileSync(file, JSON.stringify(checkout));
  const call = async (method: string, url: string, body = "") => {
    const req = Object.assign(Readable.from(body ? [Buffer.from(body)] : []), { method, url, headers: {} });
    let status = 0;
    let text = "";
    const done = new Promise<void>((resolve) => {
      const res = {
        headersSent: false,
        writeHead(code: number) { status = code; return res; },
        setHeader() { return res; },
        end(chunk?: string) { text = String(chunk ?? ""); resolve(); },
      };
      const cwd = process.cwd();
      process.chdir(root);
      Promise.resolve(handler?.(req as unknown as IncomingMessage, res as unknown as ServerResponse, () => resolve()))
        .finally(() => process.chdir(cwd));
    });
    await done;
    return { status, json: text ? JSON.parse(text) as Record<string, unknown> : {} };
  };
  return { call, file, close: () => sidecar.close() };
}

test("desk config reads keep the engine's risk values and saves never fake an apply", async () => {
  const d = await desk();
  try {
    const got = await d.call("GET", "/config.json?conn=vst");
    const overlay = got.json.overlay as Record<string, unknown>;
    assert.equal(got.status, 200);
    assert.equal(overlay.tpPct, 0.9);
    assert.equal(overlay.slPct, 0.5);
    assert.equal(overlay.slToTpRatio, 0.8);
    assert.deepEqual(overlay.forcedSymbols, ["XRP-USDT"]);

    const before = readFileSync(d.file, "utf8");
    const saved = await d.call("POST", "/config.json?conn=vst", JSON.stringify({ overlay: { tpPct: 1.1 } }));
    assert.equal(saved.status, 503);
    assert.equal(saved.json.ok, false);
    assert.ok(existsSync(d.file));
    assert.equal(readFileSync(d.file, "utf8"), before);
  } finally {
    d.close();
  }
});

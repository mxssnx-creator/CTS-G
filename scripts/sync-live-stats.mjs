const SRC = "http://152.53.114.112:3015/stats.json?conn=overall";
const DEST = new URL("../public/live-stats.json", import.meta.url);

async function markOffline(reason) {
  const fs = await import("node:fs/promises");
  try {
    const s = JSON.parse(await fs.readFile(DEST, "utf8"));
    const err = String(reason || "pulse sidecar unreachable");
    s.running = false;
    s.halted = true;
    s.paused = false;
    s.alive = false;
    s.haltReason = "sidecar-down";
    s.lastError = err;
    s.progressPhase = "offline";
    s.progressReady = false;
    for (const l of s.lanes || []) {
      l.running = false;
      l.halted = true;
      l.alive = false;
      l.paused = false;
      l.haltReason = "sidecar-down";
      l.lastError = err;
      l.progressPhase = "offline";
      l.progressReady = false;
    }
    await fs.writeFile(DEST, JSON.stringify(s));
  } catch {
    /* keep last snapshot if we cannot rewrite it */
  }
}

async function once() {
  const r = await fetch(SRC, { cache: "no-store" });
  if (!r.ok) throw new Error("status " + r.status);
  const text = await r.text();
  JSON.parse(text);
  const fs = await import("node:fs/promises");
  await fs.writeFile(DEST, text);
}

async function loop() {
  for (;;) {
    try {
      await once();
    } catch (e) {
      const msg = e && e.message ? e.message : String(e);
      console.error("sync-live-stats", msg);
      await markOffline("pulse sidecar down: " + msg);
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
}

loop();

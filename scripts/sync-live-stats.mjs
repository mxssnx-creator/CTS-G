const PULSE = "http://152.53.114.112:3015/stats.json?conn=overall";
const CTS = "http://152.53.114.112";
const LIVE_ID = "bingx-90fb3a5490fb";
const VST_ID = "bingx-x02";
const DEST = new URL("../public/live-stats.json", import.meta.url);

async function readJson(url, timeoutMs = 8000) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const r = await fetch(url, { cache: "no-store", signal: ac.signal });
    if (!r.ok) throw new Error("status " + r.status);
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

function applyCtsLane(lane, progression, engineStates, engRow) {
  const pr = (progression && progression.progression) || {};
  const st = (progression && progression.state) || {};
  const pp = pr.prehistoricProgress || {};
  const running = !!(engineStates && engineStates.engineRunning) || String((engRow && engRow.status) || "") === "running";
  const phase = String(pr.phase || "");
  const pre = !!st.prehistoricPhaseActive || phase.includes("prehistoric");
  const pct = Number(pr.progress ?? pp.percentComplete ?? (pre ? 0 : 100));
  lane.running = running;
  lane.halted = !running;
  lane.alive = running;
  lane.paused = false;
  lane.haltReason = running ? "" : "engine-stopped";
  lane.lastError = running ? "" : "CTS worker not attached";
  lane.progressPct = Math.max(0, Math.min(100, Number.isFinite(pct) ? pct : 0));
  lane.progressPhase = pre ? "warmup" : phase === "live_trading" ? "live" : phase || (running ? "run" : "idle");
  lane.progressDetail = String(pr.message || "");
  lane.progressReady = running && !pre && phase === "live_trading";
  if (st.cyclesCompleted != null) lane.cycle = st.cyclesCompleted;
  if (pp.symbolsTotal) lane.symbolCount = pp.symbolsTotal;
  else if (pr.details && pr.details.symbolCount) lane.symbolCount = pr.details.symbolCount;
  return lane;
}

async function fromCts() {
  const fs = await import("node:fs/promises");
  const s = JSON.parse(await fs.readFile(DEST, "utf8"));
  const [liveP, vstP, liveE, vstE, eng] = await Promise.all([
    readJson(`${CTS}/api/connections/progression/${LIVE_ID}`),
    readJson(`${CTS}/api/connections/progression/${VST_ID}`),
    readJson(`${CTS}/api/connections/${LIVE_ID}/engine-states`),
    readJson(`${CTS}/api/connections/${VST_ID}/engine-states`),
    readJson(`${CTS}/api/trade-engine/status`),
  ]);
  const engMap = Object.fromEntries((eng.connections || []).map((c) => [c.id, c]));
  for (const l of s.lanes || []) {
    if (l.type === "live") applyCtsLane(l, liveP, liveE, engMap[LIVE_ID]);
    if (l.type === "vst") applyCtsLane(l, vstP, vstE, engMap[VST_ID]);
  }
  const liveLane = (s.lanes || []).find((l) => l.type === "live");
  const vstLane = (s.lanes || []).find((l) => l.type === "vst");
  s.running = !!(liveLane && liveLane.running) || !!(vstLane && vstLane.running);
  s.halted = !s.running;
  s.alive = s.running;
  s.paused = false;
  s.haltReason = s.running ? "" : "sidecar-down";
  if (liveLane) {
    s.progressPhase = liveLane.progressPhase;
    s.progressPct = liveLane.progressPct;
    s.progressDetail = liveLane.progressDetail;
    s.progressReady = liveLane.progressReady;
    s.cycle = liveLane.cycle;
  }
  s.now = Date.now();
  await fs.writeFile(DEST, JSON.stringify(s));
}

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
  const r = await fetch(PULSE, { cache: "no-store" });
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
      console.error("sync-live-stats pulse", msg);
      try {
        await fromCts();
        console.error("sync-live-stats cts fallback ok");
      } catch (e2) {
        const msg2 = e2 && e2.message ? e2.message : String(e2);
        console.error("sync-live-stats cts", msg2);
        await markOffline("pulse sidecar down: " + msg);
      }
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
}

loop();

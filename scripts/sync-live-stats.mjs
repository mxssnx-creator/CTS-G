const SRC = "http://152.53.114.112:3015/stats.json?conn=overall";
const DEST = new URL("../public/live-stats.json", import.meta.url);

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
      console.error("sync-live-stats", e.message || e);
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
}

loop();

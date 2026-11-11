// Compare the Python port to the actual pinned CTS-A-O registry, not another
// implementation imported from the port. No exchange code is loaded.
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { execFileSync, spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";

const root = resolve(process.argv[2] ?? "");
const sha = execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).trim();
if (sha !== "2599b652b9429c34d290a07e8df1d70a62d80462") throw new Error("unexpected reference checkpoint");
const { INDICATION_BY_ID } = await import(pathToFileURL(resolve(root, "src/core/indications/registry.ts")));
const { SeriesCache } = await import(pathToFileURL(resolve(root, "src/core/indications/cache.ts")));
const ids = { "ema-cross": ["ema-9-21", "ema-21-55"], "macd-cross": ["macd-cross-8-21-5", "macd-cross"],
  "rsi-reversal": ["rsi-7-15-85", "rsi-14-25-75"], "ema-trend": ["trend-ema-12-26", "trend-ema-20-50"],
  "rsi-momentum": ["rsi-mom-14-20", "rsi-mom-21-20"], donchian: ["break-don10", "break-don20"],
  "bb-bounce": ["bb-bounce", "bb-bounce-50-2"], "cci-reversal": ["cci-14-100", "cci-20-100"],
  "obv-trend": ["obv-20", "obv-50"] };
const cases = [];
for (const mode of ["waves", "flat", "tiny", "steps"]) {
  const closes = Array.from({ length: 1200 }, (_, i) => mode === "flat" ? 100 : mode === "tiny" ? 100 + 1e-10 * Math.sin(i / 11) : mode === "steps" ? 100 + Math.floor(i / 20) % 6 : 100 + 5 * Math.sin(i / 17) + 2 * Math.cos(i / 7));
  const highs = closes.map((x,i) => x + (mode === "tiny" || mode === "flat" ? 0 : .1 + i % 5 / 20));
  const lows = closes.map((x,i) => x - (mode === "tiny" || mode === "flat" ? 0 : .1 + i % 7 / 20));
  const volumes = closes.map((_,i) => 1+i%7);
  const b = { sym: "FIXTURE", tfMin: 1, n: closes.length, t: Float64Array.from(closes,(_,i)=>i*60000),
    o: Float64Array.from(closes), h: Float64Array.from(highs), l: Float64Array.from(lows), c: Float64Array.from(closes), v: Float64Array.from(volumes) };
  const cache = new SeriesCache(b);
  for (const [source, variants] of Object.entries(ids)) for (const [index, id] of variants.entries()) {
    const expected = Array.from(INDICATION_BY_ID.get(id).fn(cache));
    cases.push({ mode, source, range: index ? "medium" : "short", closes, highs, lows, volumes, expected });
  }
}
const pulse = resolve(fileURLToPath(new URL("../server/pulse", import.meta.url)));
const python = "import json,sys;sys.path.insert(0,sys.argv[1]);from signal_sources import states;xs=json.load(sys.stdin);json.dump([states(x['closes'],x['source'],x['range'],x['highs'],x['lows'],x['volumes']) for x in xs],sys.stdout)";
const run = spawnSync("python3", ["-B", "-c", python, pulse], { input: JSON.stringify(cases), encoding: "utf8", maxBuffer: 8*1024*1024, timeout: 60000 });
if (run.status !== 0) throw new Error(run.stderr || "port failed");
const actual = JSON.parse(run.stdout), differences = [];
for (const [i,c] of cases.entries()) for (let j=0;j<c.expected.length;j++) if (c.expected[j]!==actual[i][j]) differences.push({source:c.source,range:c.range,mode:c.mode,index:j,reference:c.expected[j],actual:actual[i][j]});
const sourceHashes = Object.fromEntries(["src/core/math/indicators.ts", "src/core/indications/registry.ts", "src/core/indications/cache.ts", "src/core/signal-config.ts"].map(p=>[p,createHash("sha256").update(readFileSync(resolve(root,p))).digest("hex")]));
const differenceGroups = {};
for (const d of differences) { const key = `${d.source}/${d.range}/${d.mode}`; differenceGroups[key]=(differenceGroups[key]??0)+1; }
console.log(JSON.stringify({ reference: sha, cases: cases.length, comparedBars: cases.length*1200, differences: differences.slice(0,50), differenceCount: differences.length, differenceGroups, sourceHashes,
  portSha256:createHash("sha256").update(readFileSync(resolve(pulse,"signal_sources.py"))).digest("hex"),
  interpretation:"Reference parity is diagnostic, not a correctness oracle: BB variance cancellation / boundary errors and CCI near-flat rolling-mean errors are retained as differences. Independent Decimal source checks test corrected formulas." }, null, 2));
if (differences.length) process.exitCode=1;

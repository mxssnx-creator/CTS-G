/** Each profile owns a native processing lane; roles are labels, never borrowed qualification. */
export type LastNLane = {
  id: string; role: "major" | "minor" | "independent" | "additional";
  scope?: "overall" | "symbol" | "indication" | "type" | "config";
  lastN: { baseLong?: number; baseShort?: number; mainLong?: number; mainShort?: number[]; negWindows?: number[]; realN?: number; realNegN?: number };
  minPfByStep?: Record<string, number>; minPfBySlRatio?: Record<string, number>;
  negativeFilter?: { windows: number[]; mode: "any" | "all"; maxPf: number } | null;
};
export const DEFAULT_LAST_N_LANES: LastNLane[] = [
  {id:"major",role:"major",lastN:{baseLong:30,baseShort:15,mainLong:20,mainShort:[5,10],realN:12,realNegN:12}},
  {id:"minor",role:"minor",lastN:{baseLong:20,baseShort:10,mainLong:10,mainShort:[5],realN:8,realNegN:8}},
  {id:"independent",role:"independent",lastN:{baseLong:10,baseShort:5,mainLong:10,mainShort:[1,3,5],realN:5,realNegN:5}},
];
const number = (x: unknown, lo: number, hi: number, integer = false): x is number =>
  typeof x === "number" && Number.isFinite(x) && x >= lo && x <= hi && (!integer || Number.isInteger(x));
export function lastNLanes(raw: unknown): LastNLane[] {
  if (raw == null) return [];
  if (!Array.isArray(raw) || raw.length > 8) throw new Error("At most 8 independent Last-N lanes");
  const ids = new Set<string>();
  return raw.map(item => {
    if (!item || typeof item !== "object" || Array.isArray(item)) throw new Error("Invalid Last-N lane");
    const id = item.id, role = item.role ?? "independent", scope = item.scope ?? "config";
    if (typeof id !== "string" || !id.trim() || id.length > 64 || ids.has(id)) throw new Error("Unique Last-N lane ID required");
    ids.add(id);
    if (!["major","minor","independent","additional"].includes(role) || !["overall","symbol","indication","type","config"].includes(scope)) throw new Error("Invalid lane role or scope");
    const windows = item.lastN ?? {};
    if (!windows || typeof windows !== "object" || Array.isArray(windows)) throw new Error("Invalid Last-N windows");
    const lastN: Record<string, number | number[]> = {};
    for (const [k,v] of Object.entries(windows)) {
      if (["mainShort","negWindows"].includes(k)) {
        if (!Array.isArray(v) || !v.length || v.length > 16 || v.some(n => !number(n,1,200,true))) throw new Error(`Invalid ${k}`);
        lastN[k] = [...new Set(v)].sort((a,b)=>a-b);
      } else {
        const lo = k === "baseShort" ? 0 : 1;
        if (!["baseLong","mainLong","baseShort","realN","realNegN"].includes(k) || !number(v,lo,200,true)) throw new Error(`Invalid ${k}`);
        lastN[k] = v;
      }
    }
    const p: LastNLane = { id, role, scope, lastN };
    if (item.negativeFilter != null) {
      const f = item.negativeFilter, mode = f.mode ?? "any", maxPf = f.maxPf ?? 1;
      if (typeof f !== "object" || Array.isArray(f) || Object.keys(f).some(k=>!["windows","mode","maxPf"].includes(k)) ||
        !Array.isArray(f.windows) || !f.windows.length || f.windows.length > 16 || f.windows.some((n: unknown)=>!number(n,1,200,true)) ||
        !["any","all"].includes(mode) || !number(maxPf,0,1)) throw new Error("Invalid negative filter");
      p.negativeFilter = {windows:[...new Set<number>(f.windows)].sort((a,b)=>a-b),mode,maxPf};
    } else if ("negativeFilter" in item) p.negativeFilter = null;
    for (const k of ["minPfByStep", "minPfBySlRatio"] as const) {
      const m = item[k] ?? {};
      if (!m || typeof m !== "object" || Array.isArray(m)) throw new Error(`Invalid ${k}`);
      const checked: Record<string,number> = {};
      for (const [key,v] of Object.entries(m)) {
        const x = Number(key);
        if (!key.trim() || !(k === "minPfByStep" ? number(x,2,50,true) : number(x,.25,2.5)) || !number(v,1.000001,100)) throw new Error(`Invalid ${k} floor`);
        checked[String(x)] = v;
      }
      p[k] = checked;
    }
    return p;
  });
}

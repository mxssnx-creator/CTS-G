export const STAGE_ORDER = ["base", "main", "real", "exec"] as const;
export type StageName = (typeof STAGE_ORDER)[number];

export const STAGE_LABEL = { base: "Base", main: "Main", real: "Real", exec: "Exec" } as const;
export const LONG_COUNTS = [10, 20, 30, 40, 50] as const;
export const SHORT_COUNTS = [4, 8, 12, 16] as const;
export const LAST_COUNTS = [1, 2, 3, 4, 5] as const;
export const SIGN_MODES = ["positive", "negative"] as const;
export const LONG_MODES = ["none", "inherit"] as const;
type SignMode = (typeof SIGN_MODES)[number];
type LongMode = (typeof LONG_MODES)[number];

export type StageWindows = {
  short: number[];
  last: number[];
  shortModes: SignMode[];
  lastModes: SignMode[];
};
export type StageGridConfig = {
  enabled: boolean;
  base: StageWindows & { long: number[] };
} & Record<Exclude<StageName, "base">, StageWindows & { longModes: LongMode[] }>;

function selected<T>(choices: readonly T[], raw: unknown): T[] {
  const valid = Array.isArray(raw) ? choices.filter((choice) => raw.includes(choice)) : [];
  return valid.length ? valid : [...choices];
}

export function stageGridOf(raw: unknown): StageGridConfig {
  const input = raw && typeof raw === "object" ? raw as Record<string, unknown> : {};
  function inputFor(name: StageName) {
    const value = input[name];
    return value && typeof value === "object" ? value as Record<string, unknown> : {};
  }
  function stage(name: StageName): StageWindows {
    const value = inputFor(name);
    const short = selected(SHORT_COUNTS, value.short);
    return {
      short,
      last: selected(LAST_COUNTS, value.last),
      shortModes: short.every((n) => n > 12) ? ["positive"] : selected(SIGN_MODES, value.shortModes),
      lastModes: selected(SIGN_MODES, value.lastModes),
    };
  }
  function downstream(name: Exclude<StageName, "base">) {
    return { ...stage(name), longModes: selected(LONG_MODES, inputFor(name).longModes) };
  }
  return {
    enabled: raw !== false && input.enabled !== false,
    base: { ...stage("base"), long: selected(LONG_COUNTS, inputFor("base").long) },
    main: downstream("main"),
    real: downstream("real"),
    exec: downstream("exec"),
  };
}

export type StageGridFigures = {
  configured: number;
  evaluated: number;
  sampleFull: number;
  qualified: number;
  negativeShort?: number;
  negativeLast?: number;
  samplePct: number;
  orders: number;
  gain: number;
  loss: number;
  pf: number | null;
};
export type StageGridReport = { baseSets: number; stages: Record<StageName, StageGridFigures> };
export type StageGridSelection = {
  windows: Partial<Record<StageName, { long: number; short: number; last: number; shortMode: SignMode; lastMode: SignMode }>>;
  id?: string;
  parentIds: Partial<Record<StageName, string>>;
};

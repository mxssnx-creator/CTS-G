/** The settings page's sections and its top menus (owner 2026-11-15: "add top menus (submenus) for sections for better navigation").
 * Every section sits under exactly one menu. */
export const SECTIONS = [
  "overview",
  "connection",
  "profit",
  "risk",
  "trailing",
  "timeframes",
  "packs",
  "sets",
  "exits",
  "stages",
  "block",
  "dca",
  "axes",
  "volume",
  "controls",
  "indication",
  "pulse",
  "symbols",
] as const;
export type SettingsSection = (typeof SECTIONS)[number];

export const SECTION_LABEL: Record<SettingsSection, string> = {
  overview: "Overview",
  connection: "Connection",
  profit: "Profit factor",
  risk: "SL / TP ratios",
  trailing: "Trailing recals",
  timeframes: "Timeframes",
  packs: "Strategy packs",
  sets: "Sets · historic",
  exits: "Exits · SL",
  stages: "Stages",
  block: "Block",
  dca: "DCA",
  axes: "Axes",
  volume: "Volume",
  controls: "Control orders",
  indication: "Indication",
  pulse: "Pulse overlay",
  symbols: "Symbols",
};

export const SECTION_GROUPS: ReadonlyArray<{ id: string; label: string; sections: readonly SettingsSection[] }> = [
  { id: "run", label: "Run", sections: ["overview", "connection", "pulse", "symbols"] },
  { id: "strategies", label: "Strategies", sections: ["packs", "sets", "indication", "timeframes"] },
  { id: "validation", label: "Validation · PF", sections: ["profit", "stages"] },
  { id: "exits", label: "Exits · risk", sections: ["risk", "exits", "trailing", "controls"] },
  { id: "stacking", label: "Block · DCA · Axis", sections: ["block", "dca", "axes"] },
  { id: "sizing", label: "Volume", sections: ["volume"] },
];

/** The menu that holds a section. */
export function groupOf(section: SettingsSection): string {
  return SECTION_GROUPS.find((g) => g.sections.includes(section))?.id ?? SECTION_GROUPS[0].id;
}

import type { PulseOverlay } from "@/lib/config-model";

export type ModuleStatus = "live" | "ready" | "slot";
export type LayerId = "core" | "feed" | "exchange" | "strategy" | "risk" | "exec" | "desk";

export type SysModule = {
  id: string;
  layer: LayerId;
  name: string;
  summary: string;
  status: ModuleStatus;
  toggle?: keyof PulseOverlay;
  defaultOn: boolean;
};

export const LAYERS: { id: LayerId; title: string; blurb: string }[] = [
  { id: "core", title: "Core", blurb: "Scan loop, watchdog, persistence" },
  { id: "feed", title: "Feeds", blurb: "Prices and bars — swap without touching strategies" },
  { id: "exchange", title: "Exchange", blurb: "Signed venue adapter. One live; others are slots" },
  { id: "strategy", title: "Strategies", blurb: "CTS packs — Block, axes, trailing, DCA" },
  { id: "risk", title: "Risk", blurb: "Floors, caps, halt — always evaluated" },
  { id: "exec", title: "Execution", blurb: "Orders, batch, control SL/TP" },
  { id: "desk", title: "Desk", blurb: "Stats, results, settings surfaces" },
];

export const MODULES: SysModule[] = [
  { id: "core.engine", layer: "core", name: "Pulse engine", summary: "Independent live loop, adopt, stats dump", status: "live", defaultOn: true },
  { id: "core.watchdog", layer: "core", name: "Watchdog", summary: "Hang restart if a scan stalls", status: "live", defaultOn: true },
  { id: "core.load", layer: "core", name: "Load governor", summary: "Partial scans, RSS caps, cache trim, thread backpressure", status: "live", defaultOn: true },
  { id: "core.historic", layer: "core", name: "1m historic", summary: "Prehistoric replay on 1-minute bars for every Set", status: "live", toggle: "histEnabled", defaultOn: true },
  { id: "feed.ws", layer: "feed", name: "WebSocket marks", summary: "Venue stream for last price", status: "live", defaultOn: true },
  { id: "feed.rest", layer: "feed", name: "REST snapshot", summary: "Ticker + kline batch with rate buckets", status: "live", defaultOn: true },
  { id: "feed.universeRank", layer: "feed", name: "Universe rank", summary: "Dynamic book: exchange max leverage then 1H volatility", status: "live", toggle: "symbolsDynamic", defaultOn: true },
  { id: "feed.signals", layer: "feed", name: "Signal sources", summary: "CTS public 1m lanes — BingX + Binance + Bybit", status: "live", defaultOn: true },
  { id: "feed.tf1m", layer: "feed", name: "TF 1m", summary: "Independent 1-minute Signal lane", status: "live", toggle: "tf1m", defaultOn: true },
  { id: "feed.tf5m", layer: "feed", name: "TF 5m", summary: "Independent 5-minute Signal lane", status: "live", toggle: "tf5m", defaultOn: true },
  { id: "feed.tf15m", layer: "feed", name: "TF 15m", summary: "Independent 15-minute Signal lane", status: "live", toggle: "tf15m", defaultOn: true },
  { id: "feed.tfCombined", layer: "feed", name: "TF combined", summary: "Comprehensive consensus across 1/5/15", status: "live", toggle: "tfCombined", defaultOn: true },
  { id: "exchange.bingx", layer: "exchange", name: "BingX USDT-M", summary: "X01 live adapter · hedge · crossed", status: "live", defaultOn: true },
  { id: "exchange.binance", layer: "exchange", name: "Binance USD-M", summary: "Adapter slot — same order/risk contract", status: "slot", defaultOn: false },
  { id: "exchange.bybit", layer: "exchange", name: "Bybit linear", summary: "Adapter slot — same order/risk contract", status: "slot", defaultOn: false },
  { id: "exchange.okx", layer: "exchange", name: "OKX swap", summary: "Adapter slot — same order/risk contract", status: "slot", defaultOn: false },
  { id: "strategy.indications", layer: "strategy", name: "Indications", summary: "Signals coordinated: min 3 sources, 0.6 agreement, low-stop consensus", status: "live", toggle: "indEnabled", defaultOn: true },
  { id: "strategy.block", layer: "strategy", name: "Block strategy", summary: "Counts 1–12 on a live parent · independent PF per stack", status: "live", toggle: "blockEnabled", defaultOn: true },
  { id: "strategy.coord", layer: "strategy", name: "Coordination axes", summary: "Prev / last / cont / pause windows gate new risk", status: "live", defaultOn: true },
  { id: "strategy.sets", layer: "strategy", name: "Independent Sets", summary: "Pack × SL:TP × trail × step books with 1m historic", status: "live", toggle: "histEnabled", defaultOn: true },
  { id: "strategy.trailing", layer: "strategy", name: "Trailing recals", summary: "Independent arm/give range · auto pick on last-N", status: "live", toggle: "stratTrailing", defaultOn: true },
  { id: "strategy.trailRecalc", layer: "strategy", name: "Trail auto-recalc", summary: "Pick arm:give from last-N independent of SL:TP", status: "live", toggle: "trailAuto", defaultOn: true },
  { id: "strategy.dca", layer: "strategy", name: "DCA", summary: "Independent CTS steps on adverse move · own PF book", status: "live", toggle: "dcaEnabled", defaultOn: true },
  { id: "strategy.exits", layer: "strategy", name: "Exit SL coord", summary: "Best close via optimal SL · independent of TP", status: "live", toggle: "exitEnabled", defaultOn: true },
  { id: "strategy.rearrange", layer: "strategy", name: "Rearrange", summary: "Free a weak slot for a stronger signal", status: "live", toggle: "rearrange", defaultOn: true },
  { id: "risk.slTpRatios", layer: "risk", name: "SL:TP ratios", summary: "0.3–1.5 step 0.3 · SL bound to TP · independent recals", status: "live", defaultOn: true },
  { id: "risk.equityFloor", layer: "risk", name: "Equity floor", summary: "Halt when equity is below min", status: "live", defaultOn: true },
  { id: "risk.notionalCap", layer: "risk", name: "Notional cap", summary: "Size cannot exceed available × lev", status: "live", defaultOn: true },
  { id: "risk.drawdown", layer: "risk", name: "Drawdown halt", summary: "Stop new risk after session DD", status: "live", defaultOn: true },
  { id: "exec.controls", layer: "exec", name: "Control orders", summary: "Exchange STOP / TAKE_PROFIT", status: "live", toggle: "controlOrders", defaultOn: true },
  { id: "exec.batch", layer: "exec", name: "Batch SL+TP", summary: "Up to 5 legs, venue batch endpoint", status: "live", defaultOn: true },
  { id: "exec.rate", layer: "exec", name: "Rate buckets", summary: "Public / private / order lanes + 100410 cool", status: "live", defaultOn: true },
  { id: "desk.stats", layer: "desk", name: "Live desk", summary: "Equity, book, Block heat, coord strip", status: "live", defaultOn: true },
  { id: "desk.results", layer: "desk", name: "Results", summary: "Closed tape, PF, symbol breakdown", status: "live", defaultOn: true },
  { id: "desk.settings", layer: "desk", name: "Settings", summary: "CTS values + pulse overlay", status: "live", defaultOn: true },
  { id: "desk.alerts", layer: "desk", name: "Alerts", summary: "Push / voice slot", status: "slot", defaultOn: false },
  { id: "desk.copy", layer: "desk", name: "Copy overlay", summary: "Mirror fills to a follower slot", status: "slot", defaultOn: false },
];

export function modulesFromOverlay(o: PulseOverlay): Record<string, boolean> {
  const out: Record<string, boolean> = { ...(o.modules ?? {}) };
  for (const m of MODULES) {
    if (out[m.id] === undefined) {
      if (m.toggle) out[m.id] = Boolean(o[m.toggle]);
      else out[m.id] = m.defaultOn;
    }
  }
  return out;
}

export function applyModule(
  overlay: PulseOverlay,
  id: string,
  on: boolean,
): PulseOverlay {
  const mod = MODULES.find((m) => m.id === id);
  const next: PulseOverlay = {
    ...overlay,
    modules: { ...modulesFromOverlay(overlay), [id]: on },
  };
  if (mod?.toggle) {
    (next as unknown as Record<string, unknown>)[mod.toggle] = on;
  }
  if (id === "strategy.indications") {
    next.indEnabled = on;
  }
  if (id === "feed.tf1m") next.tf1m = on;
  if (id === "feed.tf5m") next.tf5m = on;
  if (id === "feed.tf15m") next.tf15m = on;
  if (id === "feed.tfCombined") next.tfCombined = on;
  if (id === "strategy.coord") {
    next.axisPrevEnabled = on;
    next.axisLastEnabled = on;
    next.axisContEnabled = on;
    next.axisPauseEnabled = on;
  }
  if (id === "strategy.block") {
    next.blockEnabled = on;
    next.stratBlock = on;
  }
  if (id === "strategy.sets") {
    next.histEnabled = on;
  }
  if (id === "strategy.exits") {
    next.exitEnabled = on;
  }
  if (id === "strategy.dca") {
    next.dcaEnabled = on;
    next.stratDca = on;
  }
  return next;
}

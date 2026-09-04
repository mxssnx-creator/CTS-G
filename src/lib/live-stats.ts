export type LiveOpen = {
  symbol: string;
  side: string;
  qty: number;
  entry: number;
  px: number | null;
  uPnlPct: number;
  ageS: number;
  reason: string;
  sl: number;
  tp: number;
  slOid?: string;
  tpOid?: string;
  secSl?: number;
  secTp?: number;
  secSlOid?: string;
  secTpOid?: string;
  controls?: boolean;
  overall?: boolean;
  security?: boolean;
  controlMode?: "per-config" | "aggregate" | string;
  controlGroupKey?: string;
  controlGroupToken?: string;
  controlRangeKey?: string;
  controlRangeBp?: { sl?: number; tp?: number };
  controlStatus?: "protected" | "missing" | string;
  memberCount?: number;
  lineageSetIds?: string[];
  lineageParentSetIds?: string[];
  lineageAxisKeys?: string[];
  lineagePacks?: string[];
  connection?: string;
  connType?: string;
  unit?: string;
  slRatio?: number;
  trailKey?: string;
  slPct?: number;
  tpPct?: number;
  setId?: string;
  parentSetId?: string;
  axisKey?: string;
  relativeCount?: number;
  volumeRatio?: number;
  pack?: string;
  clientId?: string;
  ours?: boolean;
  indKind?: string;
};

export type LiveClosed = {
  t: number;
  symbol: string;
  side: string;
  qty: number;
  entry: number;
  exit: number;
  pnl: number;
  pnl_pct: number;
  reason: string;
  hold_s: number;
  set_id?: string;
  parent_set_id?: string;
  axis_key?: string;
  relative_count?: number;
  volume_ratio?: number;
  pack?: string;
  sl_ratio?: number;
  trail_key?: string;
  ind_kind?: string;
  indKind?: string;
};

export type SideStat = {
  n?: number;
  pf?: number;
  wr?: number;
  maxDdS?: number;
  netAvg?: number;
  validated?: boolean;
  ok?: boolean;
  direction?: string;
};

export type KindStat = {
  kind?: string;
  n?: number;
  pf?: number;
  wr?: number;
  maxDdS?: number;
  avgDdS?: number;
  ddEpisodes?: number;
  netAvg?: number;
  validated?: boolean;
  profitable?: boolean;
  ok?: boolean;
  enabled?: boolean;
  processed?: boolean;
  evaluated?: number;
  qualified?: number;
  entered?: number;
  exited?: number;
  blocked?: number;
  rejected?: number;
  noSignal?: number;
  hits?: number;
  symbols?: number;
  long?: number;
  short?: number;
  scanSymbols?: number;
  scanLong?: number;
  scanShort?: number;
  avgConf?: number;
  avgStrength?: number;
  costSubtracted?: boolean;
  bySide?: Record<string, SideStat>;
};

export type StrategyStat = {
  strategy?: string;
  n?: number;
  pf?: number;
  wr?: number;
  maxDdS?: number;
  avgDdS?: number;
  netAvg?: number;
  validated?: boolean;
  enabled?: boolean;
  processed?: boolean;
  evaluated?: number;
  qualified?: number;
  entered?: number;
  exited?: number;
  blocked?: number;
  rejected?: number;
  costSubtracted?: boolean;
  bySide?: Record<string, SideStat>;
};

export type ActivityEvent = {
  event_id?: string;
  event_type?: string;
  ts?: number;
  connection?: string;
  status?: string;
  symbol?: string;
  side?: string;
  set_id?: string;
  parent_set_id?: string;
  axis_key?: string;
  indication_kind?: string;
  strategy?: string;
  order_id?: string;
  client_id?: string;
  code?: string;
  qty?: number;
  price?: number;
  pnl?: number;
  fee?: number;
  detail?: string;
  metadata?: Record<string, unknown>;
};

export type ActivityCounts = {
  evaluated?: number;
  qualified?: number;
  selected?: number;
  entered?: number;
  exited?: number;
  blocked?: number;
  rejected?: number;
  paused?: number;
  long?: number;
  short?: number;
};

export type ActivitySummary = {
  eventCount?: number;
  grossPnl?: number;
  fees?: number;
  duplicateCount?: number;
  byType?: Record<string, number>;
  byStatus?: Record<string, number>;
  responseCodes?: Record<string, number>;
  requestCount?: number;
  responseCount?: number;
  fillCount?: number;
  openEventCount?: number;
  closeEventCount?: number;
  protectionEventCount?: number;
  cancellationCount?: number;
  errorCount?: number;
  internalOpen?: number;
  exchangeOpen?: number;
  internalClosed?: number;
  parity?: "match" | "pending" | "discrepant" | string;
  pendingCount?: number;
  recoveredCount?: number;
  discrepantCount?: number;
  byIndication?: Record<string, ActivityCounts>;
  byStrategy?: Record<string, ActivityCounts>;
  byAxis?: Record<string, ActivityCounts>;
  tail?: ActivityEvent[];
  source?: string;
};

export type LiveStats = {
  running: boolean;
  mode: string;
  connection: string;
  exchange: string;
  startedAt: number;
  now: number;
  uptimeS: number;
  equity: number;
  startEquity: number;
  available: number;
  usedMargin: number;
  unrealized: number;
  realizedPnl: number;
  sessionPnl: number;
  systemPnl?: number;
  systemGrow?: number;
  systemLoss?: number;
  systemRealized?: number;
  systemUnrealized?: number;
  walletEquity?: number;
  walletUnrealized?: number;
  pnlPct: number;
  drawdownPct: number;
  wins: number;
  losses: number;
  winRate: number;
  openCount: number;
  exchangeOpenCount?: number;
  simOpenCount?: number;
  simUPnl?: number;
  maxOpen: number;
  symbols: string[];
  symbolCount?: number;
  symbolMax?: number;
  scanMs?: number;
  rssMb?: number;
  klinesReady?: number;
  regime?: string;
  coord?: {
    axes?: Record<string, { enabled: boolean; max_window: number }>;
    minPf?: number;
    pfWindow?: number;
    positionCostPct?: number;
    rearrange?: boolean;
    gate?: { allow?: boolean; reasons?: string[]; metrics?: Record<string, number> };
    stages?: Record<string, { pf?: number; n?: number; open?: boolean }>;
    mainEval?: number;
    realEval?: number;
  };
  indications?: {
    enabled?: boolean;
    types?: Record<string, boolean>;
    typeHits?: Record<string, number>;
    kindStats?: Record<string, KindStat>;
    samples?: Array<{
      symbol: string;
      kinds?: Record<string, { dir?: string; conf?: number; sl?: number; mode?: string }>;
    }>;
    minSources?: number;
    minAgreement?: number;
    extraSources?: boolean;
    evalN?: number;
    lanes?: Record<string, string[]>;
    primary?: Array<{
      symbol: string;
      direction: string;
      mode: string;
      confidence: number;
      agreement: number;
      strength: number;
      stop_loss_pct: number;
      take_profit_pct: number;
      sources: string[];
      votes_long: number;
      votes_short: number;
      timeframe?: string;
    }>;
    tf?: Record<string, { independent?: string[]; combined?: string | null }>;
    tf1m?: boolean;
    tf5m?: boolean;
    tf15m?: boolean;
    tfCombined?: boolean;
    tfMinAgree?: number;
  };
  halted: boolean;
  haltReason: string | null;
  leverage: number;
  useMaxLeverage?: boolean;
  leverageMap?: Record<string, number>;
  slPct: number;
  tpPct: number;
  targetNotional: number;
  activityPerMin: number;
  consecLoss: number;
  errors: number;
  lastError: string;
  cycle: number;
  lastEvent?: string;
  eventN?: number;
  activity?: ActivitySummary;
  events?: ActivityEvent[];
  progressPct?: number;
  progressPhase?: string;
  progressDetail?: string;
  progressReady?: boolean;
  progressSymbol?: string;
  progressSetId?: string;
  progressSymbolsDone?: number;
  progressSymbolsTotal?: number;
  progressSetsDone?: number;
  progressSetsTotal?: number;
  progressBarsDone?: number;
  progressBarsTotal?: number;
  progressElapsedMs?: number;
  progressLastRunMs?: number;
  progressCycle?: number;
  progressError?: string;
  alive?: boolean;
  tests?: Array<{name:string;pass:boolean;detail:string}>;
  open: LiveOpen[];
  closed: LiveClosed[];
  signals: Array<Record<string, unknown>>;
  prices: Record<string, number | undefined>;
  block?: {
    enabled: boolean;
    maxStack: number;
    volumeRatio: number;
    profitFactorRatio: number;
    pauseCountRatio: number;
    activeLive: boolean;
    activeReal: boolean;
    defaultMinPF: number;
    allCounts?: Array<{ n: number; inc: number; targetAdd: number; targetBlock?: number; minPF: number }>;
    countN?: number;
    lanes: Array<{
      symbol: string;
      side: string;
      baseQty: number;
      confirmedAdd: number;
      aggregate: number;
      counts: Array<{
        n: number;
        kind: string;
        inc: number;
        targetAdd: number;
        requested: number;
        minPF: number;
        obsPF: number;
        pass: boolean;
        paused: boolean;
        satisfied: boolean;
        cold: boolean;
      }>;
    }>;
  };
  dca?: {
    enabled?: boolean;
    active?: boolean;
    deactReason?: string;
    maxSteps?: number;
    distancesPct?: number[];
    mults?: number[];
    tpMode?: string;
    lastPick?: string;
    emits?: number;
    last15Ratio?: number;
    last25AvgR?: number;
    last15N?: number;
    lanes?: Array<{
      symbol: string;
      side: string;
      parentQty: number;
      avgEntry: number;
      filledN: number;
      steps: Array<{ n: number; distancePct: number; mult: number; filled: boolean; qty: number; paused: boolean }>;
    }>;
  };
  pulse?: Record<string, unknown>;
  coverage?: {
    strategies?: Record<string, boolean>;
    indicationTypes?: Record<string, boolean>;
    indicationHits?: Record<string, number>;
    indicationGate?: Record<string, KindStat>;
    stageFlow?: {
      stages?: Record<string, { evaluated?: number; qualified?: number; blocked?: number; rejected?: number; selected?: number; entered?: number; exited?: number; parents?: number; sampleCount?: number; confidence?: number; insufficientSample?: number; volumeRatio?: number }>;
      stageOrder?: string[];
      parentRule?: string;
      requiredSamples?: number;
      costSubtracted?: boolean;
    };
    evaluations?: { requiredSamples?: number; positionCostPct?: number; pairedNormalAdjusted?: boolean; costSubtracted?: boolean };
    evals?: { n?: number; symbols?: number; typeHits?: Record<string, number> };
    coord?: {
      allow?: boolean;
      stages?: Record<string, { pf?: number; n?: number; open?: boolean }>;
      variants?: Record<string, unknown>;
      coordination?: Record<string, ActivityCounts>;
      volumeRatioUnit?: number;
      closedOnlyPrev?: boolean;
      oneOpenOrderPerSet?: boolean;
      mainEval?: number;
      realEval?: number;
      axes?: Record<string, { enabled?: boolean; maxWindow?: number }>;
    };
    tracking?: {
      tag?: string;
      ours?: number;
      foreign?: number;
      withSet?: number;
      withCid?: number;
      closedOurs?: number;
    };
    block?: {
      enabled?: boolean;
      maxStack?: number;
      countN?: number;
      allCounts?: Array<{ n: number; inc: number; targetAdd: number; targetBlock?: number; minPF: number }>;
      liveLanes?: number;
      activeReal?: boolean;
    };
    sets?: {
      families?: { base?: number; trail?: number };
      setCount?: number;
      activeCount?: number;
      validatedCount?: number;
      histFills?: number;
      liveFills?: number;
      liveProcessed?: number;
      liveActive?: number;
      livePf?: number;
      liveNetAvg?: number;
      costSubtracted?: boolean;
      trailCover?: boolean;
      independentTrail?: boolean;
      slCover?: boolean;
      product?: number;
      packs?: string[];
      slRatios?: number[];
      trails?: string[];
      steps?: number[];
      dims?: { pack?: number; sl?: number; trail?: number; step?: number };
    };
    controls?: {
    open?: number;
    ok?: number;
    missing?: number;
    security?: number;
    mode?: string;
    groupCount?: number;
    protectedGroups?: number;
    mergedMembers?: number;
    groups?: Array<{
      key?: string;
      symbol?: string;
      side?: string;
      range?: string;
      qty?: number;
      memberCount?: number;
      protected?: boolean;
    }>;
  };
    recon?: { ok?: boolean; pending?: boolean; detail?: string };
    activity?: ActivitySummary;
    events?: ActivityEvent[];
    px?: number;
    symbols?: number;
    scan?: {
      universe?: number;
      px?: number;
      kl1m?: number;
      kl5m?: number;
      kl15m?: number;
      indications?: number;
      missingInd?: string[];
    };
    load?: {
      level?: string;
      rssMb?: number;
      peakMb?: number;
      scanChunk?: number;
      histChunk?: number;
      trimmed?: number;
      gcN?: number;
      shed?: string[];
      partial?: boolean;
    };
  };
  unit?: string;
  connType?: string;
  paused?: boolean;
  volumeFactor?: number;
  lanes?: Array<{
    type: string;
    id: string;
    label: string;
    unit: string;
    exchange: string;
    mode?: string;
    running: boolean;
    halted: boolean;
    haltReason?: string;
    equity: number;
    available: number;
    unrealized: number;
    openCount: number;
    exchangeOpenCount?: number;
    simOpenCount?: number;
    simUPnl?: number;
    wins: number;
    losses: number;
    sessionPnl: number;
    systemPnl?: number;
    systemGrow?: number;
    systemLoss?: number;
    pf: number;
    scanMs?: number;
    rssMb?: number;
    errors: number;
    alive: boolean;
    paused?: boolean;
    progressPct?: number;
    progressPhase?: string;
    progressDetail?: string;
    progressReady?: boolean;
    progressSymbol?: string;
    progressSetId?: string;
    progressSymbolsDone?: number;
    progressSymbolsTotal?: number;
    progressSetsDone?: number;
    progressSetsTotal?: number;
    progressBarsDone?: number;
    progressBarsTotal?: number;
    progressElapsedMs?: number;
    progressLastRunMs?: number;
    progressCycle?: number;
    progressError?: string;
    klinesReady?: number;
    hotMs?: number;
    pfCost?: number;
    controlsOk?: number;
    controlsMissing?: number;
    controlsSecurity?: number;
    validatedSetCount?: number;
    symbolCount?: number;
    lastError?: string;
    trackPrefix?: string;
    cycle?: number;
  }>;
  equityLive?: number;
  equityVst?: number;
  sessionPnlLive?: number;
  sessionPnlVst?: number;
  systemGrowLive?: number;
  systemLossLive?: number;
  systemGrowVst?: number;
  systemLossVst?: number;
  detailConn?: string;
  detailType?: string;
  engine?: {
    hotMs?: number;
    warmMs?: number;
    asyncP50?: number;
    asyncN?: number;
    qaPass?: number;
    qaFail?: number;
    scanS?: number;
    cycleMs?: number;
    cycleWaitMs?: number;
    cycleOverrun?: boolean;
    trackPrefix?: string;
    ignoredForeign?: number;
    klineLimit?: number;
    tfReady?: Record<string, number>;
    scanChunk?: number;
    scanKeep?: string[];
    load?: {
      level?: string;
      rssMb?: number;
      peakMb?: number;
      softMb?: number;
      hardMb?: number;
      scanChunk?: number;
      histChunk?: number;
      klineBatch?: number;
      lookback?: number;
      extraN?: number;
      tf5m?: boolean;
      tf15m?: boolean;
      extraSources?: boolean;
      histRun?: boolean;
      doGc?: boolean;
      statsFull?: boolean;
      partial?: boolean;
      trimmed?: number;
      gcN?: number;
      overrunN?: number;
      shed?: string[];
      hotMs?: number;
      warmMs?: number;
      histBusy?: boolean;
    };
  };
  api?: {
    wsOk?: boolean;
    asyncP50?: number;
    wsAgeMs?: number;
    rest?: number;
    ws?: number;
  };
  pfCost?: {
    n?: number;
    count?: number;
    avgR?: number;
    ratio?: number;
    classicPf?: number;
    costPct?: number;
    netPct?: number;
    grossPct?: number;
    minPf?: number;
    pass?: boolean;
    scale?: string;
    neutral?: number;
    plus1x?: number;
  };
  profitFactor?: number;
  pf?: number;
  pfNeutral?: number;
  pfPlus1xCost?: number;
  pfScale?: string;
  variants?: {
    slRatio?: number;
    slAuto?: boolean;
    slPick?: string;
    trailKey?: string;
    trailArmPct?: number;
    trailGivePct?: number;
    trailAuto?: boolean;
    trailPick?: string;
    slGrid?: Array<{ ratio: number; rr: number; slPct: number; tpPct: number; selected: boolean }>;
    slScores?: Array<{ key: string; n: number; ratio: number; pf: number; selected: boolean }>;
    trailScores?: Array<{ key: string; n: number; ratio: number; pf: number; selected: boolean; inRange?: boolean }>;
  };
  sets?: {
    enabled?: boolean;
    ready?: boolean;
    lookback?: number;
    pfWindow?: number;
    deactN?: number;
    minPf?: number;
    maxDdS?: number;
    autoDeact?: boolean;
    useHistoricGate?: boolean;
    indGate?: Record<string, KindStat>;
    setCount?: number;
    minStep?: number;
    minStepCfg?: number;
    stepMax?: number;
    stepAdapt?: boolean;
    steps?: number[];
    activeCount?: number;
    validatedCount?: number;
    histFills?: number;
    liveFills?: number;
    liveProcessed?: number;
    liveActive?: number;
    liveOverview?: {
      processed?: number;
      active?: number;
      deactivated?: number;
      fills?: number;
      last15Ratio?: number;
      last15N?: number;
      netAvg?: number;
      wr?: number;
      maxDdS?: number;
      costSubtracted?: boolean;
      source?: string;
      rows?: Array<{
        id?: string;
        pack?: string;
        n?: number;
        last15Ratio?: number;
        netAvg?: number;
        maxDdS?: number;
        wr?: number;
        active?: boolean;
        deactReason?: string;
        costSubtracted?: boolean;
      }>;
    };
    barsSymbols?: number;
    lanes?: Array<{
      type?: string;
      id?: string;
      label?: string;
      progress?: {
        phase?: string;
        pct?: number;
        detail?: string;
        ready?: boolean;
        cycle?: number;
      };
      activeCount?: number;
      validatedCount?: number;
      setCount?: number;
      ready?: boolean;
      histFills?: number;
      running?: boolean;
      halted?: boolean;
    }>;
    progress?: {
      phase?: string;
      pct?: number;
      symbol?: string;
      setId?: string;
      barsDone?: number;
      barsTotal?: number;
      setsDone?: number;
      setsTotal?: number;
      symbolsDone?: number;
      symbolsTotal?: number;
      elapsedMs?: number;
      lastRunMs?: number;
      cycle?: number;
      detail?: string;
      ready?: boolean;
      error?: string;
    };
    rows?: Array<{
      id: string;
      pack?: string;
      parentSetId?: string;
      stage?: string;
      stageQualified?: string;
      stageLedger?: Record<string, unknown>;
      basePf?: number;
      mainPf?: number;
      realPf?: number;
      axisKey?: string;
      relativeCount?: number;
      volumeRatio?: number;
      indicationKind?: string;
      strategyAdjustments?: Record<string, unknown>;
      tf: string;
      slRatio: number;
      trailKey: string;
      step?: number;
      tpPct?: number;
      n: number;
      liveN: number;
      histN?: number;
      wins: number;
      last15Ratio: number;
      last15Classic: number;
      last15N: number;
      last15R: number;
      last25AvgR: number;
      last25N: number;
      last25AvgPnl: number;
      maxDdS: number;
      avgDdS: number;
      ddEpisodes: number;
      wr?: number;
      expectancy?: number;
      grossPf?: number;
      netPf?: number;
      grossEv?: number;
      netEv?: number;
      evaluation?: Record<string, unknown>;
      pairedEvaluation?: { normal?: Record<string, unknown>; adjusted?: Record<string, unknown>; deltas?: Record<string, unknown> };
      adjustmentDeltas?: Record<string, unknown>;
      avgHoldS?: number;
      classicPf?: number;
      intern?: {
        pf15?: number;
        classic15?: number;
        avgR15?: number;
        avgR25?: number;
        maxDdS?: number;
        avgDdS?: number;
        wr?: number;
        E?: number;
        avgHoldS?: number;
        n?: number;
        liveN?: number;
      };
      active: boolean;
      deactReason: string;
      locked: boolean;
      source?: string;
      live?: {
        n?: number;
        last15Ratio?: number;
        last15N?: number;
        netAvg?: number;
        wr?: number;
        maxDdS?: number;
        validated?: boolean;
        costSubtracted?: boolean;
        source?: string;
      };
    }>;
  };
  internBest?: Array<{
    id?: string;
    pack?: string;
    last15Ratio?: number;
    last25AvgR?: number;
    maxDdS?: number;
    n?: number;
    liveN?: number;
    netAvg?: number;
    active?: boolean;
    deactReason?: string;
    source?: string;
  }>;
  exits?: {
    enabled?: boolean;
    ignoreTp?: boolean;
    bestOf?: boolean;
    lockOn?: boolean;
    peakOn?: boolean;
    revOn?: boolean;
    timeOn?: boolean;
    lockPct?: number;
    optSlPct?: number;
    lastPick?: string;
    lanes?: Array<{
      key: string;
      n: number;
      wins: number;
      last15Ratio: number;
      last25AvgR: number;
      maxDdS: number;
      active: boolean;
      deactReason: string;
      selected?: boolean;
    }>;
  };
  byIndication?: Record<string, KindStat>;
  byStrategy?: Record<string, StrategyStat>;
  klinesTf?: Record<string, number>;
  cts?: Record<string, unknown>;
};


export function statsMatchesConn(s: LiveStats, conn: string): boolean {
  const got = String(s.connType || "").toLowerCase();
  const cid = String(s.connection || "").toLowerCase();
  const unit = String(s.unit || "").toLowerCase();
  if (conn === "live") {
    if (got === "overall" || cid === "overall") return false;
    if (got === "vst" || cid.includes("x02") || unit === "vst") return false;
    return got === "live" || cid.includes("x01") || unit === "usdt";
  }
  if (conn === "vst") {
    if (got === "overall" || cid === "overall") return false;
    if (got === "live" || cid.includes("x01") || unit === "usdt") return false;
    return got === "vst" || cid.includes("x02") || unit === "vst";
  }
  if (conn === "overall") return got === "overall" || cid === "overall";
  return false;
}

export function pickView(stats: LiveStats | null, conn: string): LiveStats | null {
  if (!stats) return null;
  if (statsMatchesConn(stats, conn)) return stats;
  const sliced = viewFromSnapshot(stats, conn);
  if (sliced) return sliced;
  if (conn === "overall") {
    return {
      ...stats,
      connType: stats.connType || "overall",
      connection: stats.connection || "overall",
    };
  }
  return null;
}

function cidPrefix(conn: string): string {
  if (conn === "live") return "Gx01";
  if (conn === "vst") return "Gx02";
  return "";
}

export function viewFromSnapshot(s: LiveStats, conn: string): LiveStats | null {
  if (statsMatchesConn(s, conn)) return s;
  const overall = String(s.connType || s.connection || "").toLowerCase() === "overall";
  if (!overall) return null;
  if (conn === "overall") return s;
  const lane = (s.lanes || []).find((l) => l.type === conn);
  if (!lane) return null;
  const prefix = cidPrefix(conn);
  const unitWant = conn === "vst" ? "vst" : "usdt";
  const open = (s.open || []).filter((p) => {
    const cid = String(p.clientId || "");
    if (prefix && cid.startsWith(prefix)) return true;
    const u = String(p.unit || "").toLowerCase();
    return Boolean(u) && u === unitWant;
  });
  return {
    ...s,
    connType: conn,
    connection: lane.id || (conn === "live" ? "bingx-x01" : "bingx-x02"),
    unit: lane.unit || (conn === "vst" ? "VST" : "USDT"),
    mode: lane.mode || (conn === "vst" ? "VST_DEMO" : "LIVE_MAINNET"),
    exchange: lane.exchange || (conn === "vst" ? "BingX VST" : "BingX"),
    running: lane.running,
    halted: lane.halted,
    haltReason: lane.haltReason ?? s.haltReason ?? null,
    paused: lane.paused,
    equity: lane.equity,
    available: lane.available ?? s.available,
    unrealized: lane.unrealized ?? 0,
    sessionPnl: lane.sessionPnl,
    systemPnl: lane.systemPnl ?? lane.sessionPnl,
    systemGrow: lane.systemGrow,
    systemLoss: lane.systemLoss,
    wins: lane.wins,
    losses: lane.losses,
    winRate: lane.wins + lane.losses ? (lane.wins / (lane.wins + lane.losses)) * 100 : 0,
    openCount: lane.openCount,
    exchangeOpenCount: lane.exchangeOpenCount ?? s.exchangeOpenCount,
    simOpenCount: lane.simOpenCount ?? s.simOpenCount,
    simUPnl: lane.simUPnl ?? s.simUPnl,
    open,
    symbolCount: lane.symbolCount ?? s.symbolCount,
    scanMs: lane.scanMs ?? s.scanMs,
    lastError: lane.lastError ?? s.lastError,
    pf: lane.pf,
    progressPct: lane.progressPct ?? s.progressPct,
    progressPhase: lane.progressPhase ?? s.progressPhase,
    progressDetail: lane.progressDetail ?? s.progressDetail,
    progressReady: lane.progressReady ?? s.progressReady,
    progressSymbol: lane.progressSymbol ?? s.progressSymbol,
    progressSetId: lane.progressSetId ?? s.progressSetId,
    progressSymbolsDone: lane.progressSymbolsDone ?? s.progressSymbolsDone,
    progressSymbolsTotal: lane.progressSymbolsTotal ?? s.progressSymbolsTotal,
    progressSetsDone: lane.progressSetsDone ?? s.progressSetsDone,
    progressSetsTotal: lane.progressSetsTotal ?? s.progressSetsTotal,
    progressBarsDone: lane.progressBarsDone ?? s.progressBarsDone,
    progressBarsTotal: lane.progressBarsTotal ?? s.progressBarsTotal,
    progressElapsedMs: lane.progressElapsedMs ?? s.progressElapsedMs,
    progressLastRunMs: lane.progressLastRunMs ?? s.progressLastRunMs,
    progressCycle: lane.progressCycle ?? s.progressCycle,
    progressError: lane.progressError ?? s.progressError,
    klinesReady: lane.klinesReady ?? s.klinesReady,
    alive: lane.alive ?? s.alive,
    cycle: lane.cycle ?? s.cycle,
  };
}

async function fetchJson(url: string, timeoutMs = 4000): Promise<unknown | null> {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const r = await fetch(url, { cache: "no-store", signal: ac.signal });
    if (!r.ok) return null;
    const j = await r.json();
    return j && typeof j === "object" ? j : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

export async function fetchLiveStats(conn = "overall"): Promise<LiveStats | null> {
  if (conn === "overall") {
    const [snap, ov] = await Promise.all([
      fetchJson("/live-stats.json", 8000) as Promise<LiveStats | null>,
      fetchJson("/stats.json?conn=overall", 8000) as Promise<LiveStats | null>,
    ]);
    const best =
      ov && (ov.running || (Array.isArray(ov.lanes) && ov.lanes.length))
        ? ov
        : snap;
    if (!best) return snap || ov;
    return viewFromSnapshot(best, conn) || pickView(best, conn);
  }
  const live = (await fetchJson(`/stats.json?conn=${encodeURIComponent(conn)}`, 8000)) as LiveStats | null;
  if (live && statsMatchesConn(live, conn)) return live;
  if (live) {
    const fromLive = viewFromSnapshot(live, conn);
    if (fromLive) return fromLive;
  }
  const snap = (await fetchJson("/live-stats.json", 8000)) as LiveStats | null;
  if (snap) return viewFromSnapshot(snap, conn);
  return null;
}

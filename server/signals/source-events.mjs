// Pure, completed-candle producer. No network, account or order imports.
import { SIGNAL_SOURCES } from './ao/signal-config.ts';
import { indicationState } from './ao/indications/registry.ts';
import { SeriesCache } from './ao/indications/cache.ts';
import { signalCoordinationId, signalCoordinationVoteCounts, signalCoordinationStateFromVotes,
  SIGNAL_COORDINATION_RATIOS } from './ao/signal-coordination.ts';

export const catalogue = SIGNAL_SOURCES.map(s => ({...s}));

/** @param {number[][]} rows @param {number} tf @returns {number[][]} */
export function aggregate(rows, tf) {
  if (![1,5,15,30].includes(tf)) throw new Error('Unsupported Signals timeframe');
  /** @type {number | undefined} */
  let last;
  for (const r of rows) {
    if (r.length!==6 || r.some(x=>!Number.isFinite(x)) || r[0]%60000 ||
        Math.min(r[1],r[3],r[4])<=0 || r[3]>Math.min(r[1],r[4]) || r[2]<Math.max(r[1],r[4]) || r[5]<0 ||
        (last!==undefined && r[0]!==last+60000)) throw new Error('Invalid/gapped Signals OHLCV');
    last=r[0];
  }
  const out=[];
  for (let i=0;i+tf<=rows.length;i++) {
    if (rows[i][0]%(tf*60000)) continue;
    const part=rows.slice(i,i+tf);
    const first=part.at(0), tail=part.at(-1);
    if (!first || !tail) throw new Error('Incomplete Signals timeframe aggregate');
    out.push([first[0],first[1],Math.max(...part.map(r=>r[2])),Math.min(...part.map(r=>r[3])),tail[4],part.reduce((a,r)=>a+r[5],0)]);
    i+=tf-1;
  }
  return out;
}

/** @param {number[][]} rows @param {string} symbol @param {number} tf */
function barsObject(rows, symbol, tf) {
  return {
    sym: symbol,
    tfMin: tf,
    n: rows.length,
    t: Float64Array.from(rows, r => r[0]),
    o: Float64Array.from(rows, r => r[1]),
    h: Float64Array.from(rows, r => r[2]),
    l: Float64Array.from(rows, r => r[3]),
    c: Float64Array.from(rows, r => r[4]),
    v: Float64Array.from(rows, r => r[5]),
  };
}

/** @param {number[][]} rows @param {ArrayLike<number>} values @param {Record<string, string | number>} identity @param {number} tf */
function eventSeries(rows, values, identity, tf) {
  if (!values || values.length!==rows.length || Array.from(values).some(v=>![-1,0,1].includes(v))) throw new Error('Invalid source states');
  const events=[];
  for (let i=1;i<rows.length;i++) if (values[i] && values[i]!==values[i-1]) events.push([rows[i][0]+tf*60000,values[i]]);
  return {identity,completedBars:rows.length,events};
}

/**
 * @param {Record<string, number[][]>} data
 * @param {{sources?: string[] | 'all', timeframes?: number[], coordination?: boolean}} [options]
 */
export function sourceEvents(data, {sources='all',timeframes=[1,5,15,30],coordination=true}={}) {
  const chosen=sources==='all'?catalogue:catalogue.filter(s=>sources.includes(s.name));
  if (!chosen.length || (sources!=='all' && (new Set(sources).size!==sources.length || chosen.length!==sources.length))) throw new Error('Unknown/duplicate Signals sources');
  if (!timeframes.length || new Set(timeframes).size!==timeframes.length) throw new Error('Duplicate/empty Signals timeframes');
  const out=[];
  for (const [symbol,raw] of Object.entries(data).sort()) for (const tf of timeframes) {
    const rows=aggregate(raw,tf), cache=new SeriesCache(barsObject(rows,symbol,tf));
    const bySource=[];
    for (const source of chosen) {
      const variants=[];
      for (const [sourceRange,suffix] of [['short','s'],['medium','m']]) {
        const values=indicationState(`sig-${source.name}-${suffix}`,cache);
        if (!values) throw new Error(`Missing source states for ${source.name} ${sourceRange}`);
        variants.push(values);
        out.push(eventSeries(rows,values,{symbol,source:source.name,sourceRange,tf,coordination:'source'},tf));
      }
      bySource.push(variants);
    }
    if (coordination && chosen.length>=3) for (const mask of /** @type {const} */ ([1,2,3])) {
      const variants=bySource.map(v=>v.filter((_,i)=>mask&(1<<i)));
      const counts=signalCoordinationVoteCounts(variants,rows.length);
      for (const ratio of SIGNAL_COORDINATION_RATIOS) {
        const source=signalCoordinationId(chosen.map(s=>s.name),ratio,3,mask);
        const values=signalCoordinationStateFromVotes(counts,ratio,3,2);
        out.push(eventSeries(rows,values,{symbol,source,sourceRange:mask===1?'short':mask===2?'medium':'combined',tf,coordination:'dominating'},tf));
      }
    }
    cache.clear();
  }
  return {schema:'cts-g-signal-events-v1',catalogue:chosen,series:out,
    clock:'event timestamp is the end of a complete UTC candle; fill may use the next minute open',
    strategyRanges:['signals','exit'],sourceRangeMeaning:'short/medium are indicator parameter variants, not strategy ranges'};
}

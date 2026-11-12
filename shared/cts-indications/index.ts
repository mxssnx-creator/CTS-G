import {StreamMath} from "./origin/indications/stream-math.ts";
export {StreamMath};
import {INDICATIONS,indicationState} from "./origin/indications/registry.ts";
import {SeriesCache} from "./origin/indications/cache.ts";
import {MarketSource} from "./origin/indications/market.ts";
import type {Bars} from "./origin/domain/types.ts";

export type PortedBar={timestamp:number;open:number;high:number;low:number;close:number;volume:number};
export const PORTED_INDICATIONS=INDICATIONS.filter(s=>s.kind==="inverse"||s.kind==="tactic");
export const PORTED_BY_ID=new Map(PORTED_INDICATIONS.map(s=>[s.id,s]));
export const DEFAULT_TACTIC_SOURCES=["tactic-dca-bounce-8-percent-0.5-1-0.25","tactic-dca-trend-20-atr-1-2-0.5","tactic-dca-vwap-40-atr-1-3-0.5","tactic-dca-sweep-20-percent-0.5-2-0.25"];
export const DEFAULT_INVERSE_SOURCES=["inverse-known-ema-9-21","inverse-turn-ema-9-21","inverse-zscore-20","inverse-impulse-20","inverse-failed-break-20","inverse-rsi2-turn-20"];
export function portedBars(symbol:string,tfMin:number,rows:readonly PortedBar[]):Bars{
 if(![1,5,15,30].includes(tfMin))throw new Error("Unsupported indication timeframe");
 let prev=-Infinity;
 for(const r of rows){if(![r.timestamp,r.open,r.high,r.low,r.close,r.volume].every(Number.isFinite)||r.timestamp<=prev||r.timestamp<=0||r.low<=0||r.high<Math.max(r.open,r.close)||r.low>Math.min(r.open,r.close)||r.volume<0)throw new Error("Invalid closed-bar indication data");prev=r.timestamp;}
 const col=(k:keyof PortedBar)=>Float64Array.from(rows,r=>r[k]);
 return {sym:symbol,tfMin,n:rows.length,t:col("timestamp"),o:col("open"),h:col("high"),l:col("low"),c:col("close"),v:col("volume")};
}
/** Exact CTS-A-O source states; inversion occurs before entry, never on a settled return. */
export function portedStates(symbol:string,tfMin:number,rows:readonly PortedBar[],ids:readonly string[],peers:readonly {symbol:string;bars:readonly PortedBar[]}[]=[], stream?:StreamMath){
 const b=portedBars(symbol,tfMin,rows),k=new SeriesCache(b);if(stream)k.enableStreaming(stream);
 const unique=new Set<string>([symbol]);
 const marketBars:Bars[]=[b];
 for(const peer of peers){if(unique.has(peer.symbol))throw new Error("Duplicate market reference symbol");unique.add(peer.symbol);marketBars.push(portedBars(peer.symbol,tfMin,peer.bars));}
 k.setMarket(new MarketSource(marketBars));
 const states:Record<string,Int8Array>={},available:Record<string,boolean>={};
 for(const id of new Set(ids)){
   if(!PORTED_BY_ID.has(id))throw new Error(`Unknown Tactic/Inverse source ${id}`);
   const pair=/^inverse-pair-(btc|eth)-/.exec(id);
   available[id]=tfMin===30&&id.startsWith("inverse-")?false:pair?symbol!==pair[1].toUpperCase()+"-USDT"&&unique.has(pair[1].toUpperCase()+"-USDT")
     :id.includes("market-")?marketBars.length>1:true;
   states[id]=available[id]?indicationState(id,k)??new Int8Array(b.n):new Int8Array(b.n);
 }
 return {states,available};
}

/** CTS-A-O Follow admission events: invert in the source, then emit once at each nonzero direction onset. */
export function entryOnsets(states:Int8Array):Int8Array{const out=new Int8Array(states.length);for(let i=1;i<states.length;i++)if(states[i]!==0&&states[i]!==states[i-1])out[i]=states[i];return out;}

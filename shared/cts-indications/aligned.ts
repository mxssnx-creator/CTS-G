import {portedStates,entryOnsets,type PortedBar} from "./index.ts";
export interface SourceRequest{name:string;id:string;tf:number}
export function aggregateClosedBars(rows:readonly PortedBar[],tf:number){
 if(![1,5,15,30].includes(tf))throw new Error("Unsupported timeframe");
 const bars:PortedBar[]=[],at:number[]=[];let bucket=-1,group:PortedBar[]=[];
 const finish=(i:number)=>{if(group.length===tf&&group[0].timestamp===bucket&&group.every((b,j)=>b.timestamp===bucket+j*60000)){
   bars.push({timestamp:bucket,open:group[0].open,high:Math.max(...group.map(b=>b.high)),low:Math.min(...group.map(b=>b.low)),close:group.at(-1)!.close,volume:group.reduce((v,b)=>v+b.volume,0)});at.push(i);
 }};
 rows.forEach((bar,i)=>{const next=Math.floor(bar.timestamp/(tf*60000))*tf*60000;if(next!==bucket){if(group.length)finish(i-1);group=[];bucket=next;}group.push(bar);});if(group.length)finish(rows.length-1);
 return {bars,at};
}
export function alignedSourceStates(symbol:string,rows:readonly PortedBar[],requests:readonly SourceRequest[],peers:readonly {symbol:string;bars:readonly PortedBar[]}[]=[]){
 if(rows.length>5000||requests.length>384||new Set(requests.map(r=>r.name)).size!==requests.length)throw new Error("Source batch exceeds bounds or repeats identity");
 const states:Record<string,number[]>={},available:Record<string,boolean>={};
 for(const tf of new Set(requests.map(r=>r.tf))){
  const own=aggregateClosedBars(rows,tf),selected=requests.filter(r=>r.tf===tf);
  const result=portedStates(symbol,tf,own.bars,selected.map(r=>r.id),peers.map(p=>({symbol:p.symbol,bars:aggregateClosedBars(p.bars,tf).bars})));
  for(const request of selected){const out=Array<number>(rows.length).fill(0),source=entryOnsets(result.states[request.id]);for(let j=0;j<source.length;j++)out[own.at[j]]=source[j];states[request.name]=out;available[request.name]=result.available[request.id];}
 }
 return {states,available};
}

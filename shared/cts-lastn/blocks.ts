/** Completed-position blocks, newest first. Inputs already contain costs exactly once. */
export const LONG_COUNTS = Object.freeze(Array.from({length:13},(_,i)=>10+i*5));
export const SHORT_COUNTS = Object.freeze(Array.from({length:6},(_,i)=>5+i*3));
export const MINIMAL_COUNTS = Object.freeze([1,2,3,4,5]);
export type Polarity = 'positive' | 'negative';
export interface BlockPolicy { long:number; short:number; minimal:number; shortPolarity:Polarity; minimalPolarity:Polarity }
export const DEFAULT_BLOCK_POLICY: Readonly<BlockPolicy> = Object.freeze({long:20,short:10,minimal:3,shortPolarity:'positive',minimalPolarity:'positive'});
export function checkBlockPolicy(p:BlockPolicy):void {
  if(!p||![p.long,p.short,p.minimal].every(Number.isSafeInteger)||p.long<1||p.long>500||p.short<0||p.short>200||p.minimal<0||p.minimal>5||
    !['positive','negative'].includes(p.shortPolarity)||!['positive','negative'].includes(p.minimalPolarity))throw new Error('Invalid disjoint Last-N policy');
}
export const blockRequired=(p:BlockPolicy)=>p.long+p.short+p.minimal;
export const blockPolicyKey=(p:BlockPolicy)=>JSON.stringify(['disjoint-positions-v1',p.long,p.short,p.minimal,p.shortPolarity,p.minimalPolarity]);
/** No max(short/long) clipping: every requested combination has its own exact identity. */
export function* blockPolicies():Generator<Readonly<BlockPolicy>> {
  for(const long of LONG_COUNTS)for(const short of SHORT_COUNTS)for(const minimal of MINIMAL_COUNTS)
    for(const shortPolarity of ['positive','negative'] as const)for(const minimalPolarity of ['positive','negative'] as const)
      yield Object.freeze({long,short,minimal,shortPolarity,minimalPolarity});
}
export interface BlockPrefix { n:number; gp:Float64Array; gl:Float64Array; unknown:Uint32Array; values:Float64Array; wins:Uint32Array; losses:Uint32Array }
export function blockPrefix(values:ArrayLike<number|null>,limit=95):BlockPrefix {
  if(!Number.isSafeInteger(limit)||limit<0)throw new Error('Invalid block retention');
  const n=Math.min(values.length,limit),gp=new Float64Array(n+1),gl=new Float64Array(n+1),unknown=new Uint32Array(n+1),raw=new Float64Array(n),wins=new Uint32Array(n+1),losses=new Uint32Array(n+1);
  for(let i=0;i<n;i++){
    const v=values[i],known=typeof v==='number'&&Number.isFinite(v);
    gp[i+1]=gp[i]+(known?Math.max(0,v):0);gl[i+1]=gl[i]+(known?Math.max(0,-v):0);unknown[i+1]=unknown[i]+Number(!known);
    raw[i]=known?v:NaN;wins[i+1]=wins[i]+Number(known&&v>0);losses[i+1]=losses[i]+Number(known&&v<0);
  }
  return {n,gp,gl,unknown,values:raw,wins,losses};
}
/** Recover a small sub-window when subtracting large prefix totals would erase it. */
function directSum(values:Float64Array,start:number,end:number,kind:'gain'|'loss'|'net'):number {
  let sum=0,correction=0;
  for(let i=start;i<end;i++){
    const v=values[i];if(!Number.isFinite(v))continue;
    const x=kind==='gain'?Math.max(v,0):kind==='loss'?Math.max(-v,0):v;
    const next=sum+x;
    correction+=Math.abs(sum)>=Math.abs(x)?(sum-next)+x:(x-next)+sum;
    sum=next;
  }
  return sum+correction;
}
function needsRecovery(a:number,b:number,difference:number,count:number,n:number):boolean {
  return count>0&&Number.isFinite(a)&&Number.isFinite(b)&&difference<=Number.EPSILON*(Math.abs(a)+Math.abs(b))*(n+1);
}
export interface BlockResult { offset:number; requested:number; count:number; gp:number; gl:number; net:number; pf:number|null; state:'qualified'|'failed'|'insufficient'|'unknown' }
export function blockWindow(pre:BlockPrefix,offset:number,n:number,minPf:number,polarity:Polarity='positive',signOnly=false):BlockResult {
  const start=Math.min(pre.n,offset),end=Math.min(pre.n,offset+n),count=end-start;
  let gp=pre.gp[end]-pre.gp[start],gl=pre.gl[end]-pre.gl[start];
  if(needsRecovery(pre.gp[start],pre.gp[end],gp,pre.wins[end]-pre.wins[start],pre.n))gp=directSum(pre.values,start,end,'gain');
  if(needsRecovery(pre.gl[start],pre.gl[end],gl,pre.losses[end]-pre.losses[start],pre.n))gl=directSum(pre.values,start,end,'loss');
  let net=gp-gl;
  if(Number.isFinite(gp)&&Number.isFinite(gl)&&Math.abs(net)<=Number.EPSILON*(gp+gl)*(count+1))net=directSum(pre.values,start,end,'net');
  const unknown=pre.unknown[end]!==pre.unknown[start]||![gp,gl,net].every(Number.isFinite);
  const pf=unknown||count<n?null:gl>0?gp/gl:gp>0?Infinity:0;
  const passed=n===0||(polarity==='negative'?net<0:net>0&&(signOnly||pf!==null&&pf>=minPf));
  return {offset,requested:n,count,gp,gl,net,pf,state:unknown?'unknown':count<n?'insufficient':passed?'qualified':'failed'};
}
/** One prefix per exact configuration can evaluate all 1,560 policies without copying position arrays. */
export function evaluateBlocks(pre:BlockPrefix,p:BlockPolicy,minPf:number) {
  checkBlockPolicy(p);if(!Number.isFinite(minPf)||minPf<=1)throw new Error('Last-N PF floor must exceed one');
  const minimal=blockWindow(pre,0,p.minimal,minPf,p.minimalPolarity,true);
  const short=blockWindow(pre,p.minimal,p.short,minPf,p.shortPolarity);
  const long=blockWindow(pre,p.minimal+p.short,p.long,minPf);
  const rows=[long,short,minimal];const state:BlockResult["state"]=rows.some(w=>w.state==='unknown')?'unknown':rows.some(w=>w.state==='insufficient')?'insufficient':rows.every(w=>w.state==='qualified')?'qualified':'failed';
  return {key:blockPolicyKey(p),required:blockRequired(p),count:Math.min(pre.n,blockRequired(p)),state,long,short,minimal};
}

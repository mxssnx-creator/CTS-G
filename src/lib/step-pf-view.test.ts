import test from "node:test";
import assert from "node:assert/strict";
import { defaultOverlay } from "./config-model.ts";
import { stepPfRows } from "./step-pf-view.ts";

const evidence = (step: number, side: number, auto: number) => ({step, side, tpPct:step*.1, manual:1.2,
  auto, used:auto, orders:20, admitted:10, pfAll:1.1, pfAdmitted:1.4});

test("Auto PF display preserves both directions and wide-step table mapping", () => {
  const overlay = {...defaultOverlay(), directionMode:"split" as const, minPfByRange:{}};
  const rows = stepPfRows(overlay, [evidence(7,1,1.12), evidence(7,-1,1.38), evidence(35,1,1.26), evidence(35,-1,1.56)]);
  assert.equal(rows.length,98);
  assert.deepEqual(rows.filter(r=>r.step===7).map(r=>[r.side,r.used]), [[1,1.12],[-1,1.38]]);
  assert.deepEqual(rows.filter(r=>r.step===50).map(r=>[r.side,r.bucket,r.used]), [[1,35,1.26],[-1,35,1.56]]);
});

test("Range floors, manual mode and missing-direction evidence remain explicit", () => {
  const overlay = {...defaultOverlay(), directionMode:"split" as const, minPfByRange:{wide:1.45}};
  const rows=stepPfRows(overlay,[evidence(35,-1,1.1),evidence(7,-1,1.2)]);
  assert.deepEqual(rows.filter(r=>r.step===50).map(r=>r.used),[1.45,1.45]);
  assert.deepEqual(rows.filter(r=>r.step===50).map(r=>r.source),["Range override","Range override"]);
  assert.equal(rows.find(r=>r.step===7&&r.side===-1)?.source,"Auto");
  assert.equal(rows.find(r=>r.step===7&&r.side===1)?.source,"Manual");
  assert.equal(rows.find(r=>r.step===35&&r.side===1)?.evidence, undefined);
  const manual=stepPfRows({...overlay,minPfStepAuto:false,minPfByRange:{}},[evidence(7,1,1.8)]);
  assert.equal(manual.find(r=>r.step===7&&r.side===1)?.used,1.14);
});

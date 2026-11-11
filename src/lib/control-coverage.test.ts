import { test } from "node:test";
import assert from "node:assert/strict";
import { controlCoverage } from "./control-coverage.ts";
import type { LiveOpen } from "./live-stats.ts";

const base = {qty:10,sl:95,tp:108,slOid:"100",tpOid:"101",controls:true,ctrlVerified:true,closePosition:true} as LiveOpen;
test("coverage follows explicit close-all or complete quantity, never a 95% allowance", () => {
  assert.equal(controlCoverage({...base,ctrlQty:1}),"Reported covered");
  assert.equal(controlCoverage({...base,closePosition:false,ctrlQty:9.6}),"Quantity gap");
  assert.equal(controlCoverage({...base,closePosition:false,ctrlQty:10}),"Reported covered");
  assert.equal(controlCoverage({...base,ctrlUndercovered:true}),"Quantity gap");
});
test("missing IDs and old/unknown snapshots do not certify protection", () => {
  assert.equal(controlCoverage({...base,slOid:""}),"Missing pair");
  assert.equal(controlCoverage({...base,ctrlVerified:undefined}),"Unverified");
  assert.equal(controlCoverage({...base,closePosition:undefined}),"Unverified");
  assert.equal(controlCoverage({...base,closePosition:false,ctrlQty:NaN}),"Unverified");
  assert.equal(controlCoverage({...base,secSl:NaN}),"Unverified");
  assert.equal(controlCoverage({...base,tp:0}),"Unverified");
  assert.equal(controlCoverage({...base,qty:0}),"Unverified");
});

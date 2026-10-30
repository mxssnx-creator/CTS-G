import { test } from "node:test";
import assert from "node:assert/strict";
import { executionScopeText, scopeStatus } from "./execution-scope.ts";

test("execution scope distinguishes return PF, cash PF and fee provenance", () => {
  const text = executionScopeText({ orders: 2, wins: 1, basis: "net-position-return", pf: .8,
    returnOrders: 2, missingReturns: 0, cashPf: .016, cashOrders: 2, missingCash: 0,
    feeSources: { "reported-fee": 1, "configured-estimate": 1 } });
  assert.match(text, /position return PF 0\.80 \(2\/2 recorded closes\)/);
  assert.match(text, /cash PF 0\.02 \(2\/2\)/);
  assert.match(text, /1 reported, 1 configured estimate/);
});

test("empty, missing, no-loss and legacy values stay distinct", () => {
  const missing = executionScopeText({ orders: 2, wins: 1, basis: "net-position-return", pf: null,
    returnOrders: 1, missingReturns: 1, cashPf: 1.5, cashOrders: 2 });
  assert.match(missing, /position return PF — \(1\/2 recorded closes, 1 missing\)/);
  assert.match(executionScopeText({ orders: 1, wins: 1, basis: "net-position-return", pf: null,
    noLoss: true, returnOrders: 1 }), /position return PF no observed losses/);
  assert.match(executionScopeText({ orders: 0, wins: 0, pf: null }), /basis unverified/);
  assert.match(executionScopeText({ orders: 2, wins: 1, pf: 4 }), /position return PF — · legacy PF 4\.00 \(basis unverified\)/);
});

test("an unjudged scope is not presented as open", () => {
  assert.equal(scopeStatus(true), "open");
  assert.equal(scopeStatus(false), "blocked");
  assert.equal(scopeStatus(undefined), "not evaluated");
});

import { test } from "node:test";
import assert from "node:assert/strict";
import { SECTIONS, SECTION_GROUPS, SECTION_LABEL, groupOf } from "./settings-menu.ts";

test("settings menus hold every section exactly once, each with a label", () => {
  const listed = SECTION_GROUPS.flatMap((g) => g.sections);
  assert.deepEqual([...listed].sort(), [...SECTIONS].sort());
  assert.equal(new Set(listed).size, listed.length);
  for (const s of SECTIONS) assert.ok(SECTION_LABEL[s]);
  assert.equal(groupOf("block"), "stacking");
  assert.equal(groupOf("overview"), "run");
});

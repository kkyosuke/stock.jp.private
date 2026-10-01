import assert from "node:assert/strict";
import test from "node:test";
import worker, { tick } from "./worker.mjs";

test("retired worker never uses network or credentials even with old configuration", async () => {
  const oldFetch = globalThis.fetch;
  globalThis.fetch = () => { throw new Error("Network is forbidden"); };
  try {
    for (const date of ["2026-09-11T09:30:00Z", "2026-09-11T12:00:00Z", "2026-09-12T01:00:00Z"]) {
      const env = new Proxy({}, { get() { throw new Error("Credentials must not be read"); } });
      const result = await tick(env, new Date(date));
      assert.equal(result.status, "RETIRED_ASSISTANT_OWNED");
      assert.deepEqual(result.actions, []);
      assert.deepEqual(result.errors, []);
      await worker.scheduled({ scheduledTime: Date.parse(date) }, env);
    }
  } finally { globalThis.fetch = oldFetch; }
});

test("HTTP has no dispatch endpoint", async () => {
  assert.equal(worker.fetch().status, 404);
});

import assert from "node:assert/strict";
import test from "node:test";
import { GitHub, priceCycle, retryAllowed, tick } from "./worker.mjs";

const env = { PUBLIC_REPOSITORY: "owner/public", PRIVATE_REPOSITORY: "owner/private" };
const now = new Date("2026-09-11T11:00:00Z"); // Friday 20:00 JST
const run = (overrides = {}) => ({
  head_branch: "main", event: "workflow_dispatch", status: "completed", conclusion: "success",
  created_at: "2026-09-11T09:30:00Z", updated_at: "2026-09-11T09:40:00Z", ...overrides,
});

class FakeGitHub {
  dispatched = [];
  history = {};
  tip = "a".repeat(40);
  pointer = "a".repeat(40);
  pulls = [];
  disabled = [];
  broken = [];
  async request(path) {
    if (this.broken.some(prefix => path.startsWith(prefix))) throw new Error("GitHub 403");
    if (path.endsWith("/commits/main")) return { sha: this.tip };
    if (path.includes("/contents/stock.jp")) return { sha: this.pointer };
    if (path.includes("/pulls?")) return this.pulls;
    return { state: this.disabled.some(name => path.endsWith(name)) ? "disabled_manually" : "active" };
  }
  async runs(repo, workflow) { return this.history[workflow] ?? []; }
  async dispatch(repo, workflow, inputs) { this.dispatched.push({ repo, workflow, inputs }); }
}

test("price cycle handles JST midnight and weekends without Saturday collection", () => {
  for (const [at, expected] of [
    ["2026-09-11T09:29:00Z", "2026-09-10T09:30:00.000Z"],
    ["2026-09-11T09:30:00Z", "2026-09-11T09:30:00.000Z"],
    ["2026-09-11T18:00:00Z", "2026-09-11T09:30:00.000Z"],
    ["2026-09-14T09:00:00Z", "2026-09-11T09:30:00.000Z"],
  ]) assert.equal(priceCycle(new Date(at)).toISOString(), expected);
});

test("retries are bounded and never overlap pending, successful or recent runs", () => {
  assert.equal(retryAllowed([], now), true);
  assert.equal(retryAllowed([run({ conclusion: "failure" })], now), true);
  for (const history of [
    [run()], [run({ status: "queued", conclusion: null })],
    [run({ conclusion: "failure", updated_at: now.toISOString() })],
    [run({ conclusion: "failure" }), run({ conclusion: "failure" })],
  ]) assert.equal(retryAllowed(history, now), false);
});

test("missing public and health runs are dispatched only on main", async () => {
  const api = new FakeGitHub();
  const result = await tick(env, now, api);
  assert.deepEqual(result.errors, []);
  assert.deepEqual(api.dispatched.map(x => x.workflow), ["daily-stock-prices.yml", "operation-health.yml"]);
  assert.deepEqual(api.dispatched[0].inputs, { lookback_days: "7" });
});

test("running public collection does not cause duplicate collection", async () => {
  const api = new FakeGitHub();
  api.history["daily-stock-prices.yml"] = [run({ status: "in_progress", conclusion: null })];
  api.history["operation-health.yml"] = [run()];
  await tick(env, now, api);
  assert.equal(api.dispatched.length, 0);
});

test("a prior successful no-op sync does not block adoption of a later public commit", async () => {
  const api = new FakeGitHub();
  api.tip = "b".repeat(40);
  api.history["daily-stock-prices.yml"] = [run()];
  api.history["operation-health.yml"] = [run()];
  api.history["update-public-submodule.yml"] = [run()];
  await tick(env, now, api);
  assert.deepEqual(api.dispatched.map(x => x.workflow), ["update-public-submodule.yml"]);
});

test("pending pointer PR is not bypassed or repeatedly updated", async () => {
  const api = new FakeGitHub();
  api.tip = "b".repeat(40);
  api.pulls = [{ created_at: "2026-09-11T06:00:00Z" }];
  const result = await tick(env, now, api);
  assert.ok(result.errors.some(x => x.includes("pending")));
  assert.ok(!api.dispatched.some(x => x.workflow === "update-public-submodule.yml"));
});

test("sync is bounded after two failures; health still runs", async () => {
  const api = new FakeGitHub();
  api.tip = "b".repeat(40);
  api.history["update-public-submodule.yml"] = [run({ conclusion: "failure" }), run({ conclusion: "failure" })];
  const result = await tick(env, now, api);
  assert.ok(result.errors.some(x => x.includes("Two failed syncs")));
  assert.ok(api.dispatched.some(x => x.workflow === "operation-health.yml"));
});

test("disabled or inaccessible upstream workflows do not prevent health dispatch", async () => {
  for (const broken of [false, true]) {
    const api = new FakeGitHub();
    if (broken) api.broken = ["owner/public/"];
    else api.disabled = ["daily-stock-prices.yml"];
    const result = await tick(env, now, api);
    assert.ok(result.errors.length);
    assert.ok(!api.dispatched.some(x => x.workflow === "daily-stock-prices.yml"));
    assert.ok(api.dispatched.some(x => x.workflow === "operation-health.yml"));
  }
});

test("API filters PR/development runs and dispatches canonical main", async () => {
  const requests = [];
  const api = new GitHub("test-secret", async (url, options) => {
    requests.push({ url, options });
    return options.method === "POST" ? new Response(null, { status: 204 }) : Response.json({
      total_count: 3, workflow_runs: [run(), run({ event: "pull_request" }), run({ head_branch: "feature" })],
    });
  });
  assert.equal((await api.runs("owner/public", "test.yml", now)).length, 1);
  await api.dispatch("owner/public", "test.yml");
  assert.equal(JSON.parse(requests[1].options.body).ref, "main");
  assert.equal(requests[0].options.redirect, "error");
});

test("API failures do not expose response bodies or token in error messages", async () => {
  const api = new GitHub("test-secret", async () => new Response("private data test-secret", { status: 403 }));
  await assert.rejects(api.request("owner/private/actions/workflows/test.yml"), error =>
    error.message.includes("403") && !error.message.includes("test-secret") && !error.message.includes("private data"));
});

test("30-minute grace period avoids racing the scheduled collection", async () => {
  const api = new FakeGitHub();
  await tick(env, new Date("2026-09-11T09:47:00Z"), api);
  assert.ok(!api.dispatched.some(x => x.workflow === "daily-stock-prices.yml"));
});

test("scheduler PAT expiration is reported without credentials", async () => {
  const api = new FakeGitHub();
  api.expiration = "2026-09-20 00:00:00 UTC";
  const result = await tick(env, now, api);
  assert.ok(result.errors.some(x => x.includes("expires within 14 days")));
});

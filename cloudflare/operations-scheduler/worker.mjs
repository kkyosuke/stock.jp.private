const DAY = 86_400_000;
const MINUTE = 60_000;
const JST = 9 * 60 * MINUTE;

// Most recent weekday 18:30 JST, including overnight catch-up after midnight.
export function priceCycle(now) {
  const local = new Date(now.getTime() + JST);
  const cycle = new Date(Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate(), 18, 30));
  if (cycle > local) cycle.setTime(cycle.getTime() - DAY);
  while ([0, 6].includes(cycle.getUTCDay())) cycle.setTime(cycle.getTime() - DAY);
  return new Date(cycle.getTime() - JST);
}

export class GitHub {
  constructor(token, fetcher = fetch) {
    if (!token) throw new Error("Configure AUTOMATION_PAT");
    this.token = token;
    this.fetcher = fetcher;
  }

  async request(path, body) {
    const response = await this.fetcher(`https://api.github.com/repos/${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: {
        Authorization: `Bearer ${this.token}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "stock-jp-operations-scheduler",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      redirect: "error",
      signal: AbortSignal.timeout(20_000),
    });
    // Never include response bodies or credentials in Cloudflare logs.
    if (!response.ok) throw new Error(`GitHub ${response.status}: ${path.split("?")[0]}`);
    this.expiration = response.headers.get("GitHub-Authentication-Token-Expiration") || this.expiration;
    return response.status === 204 ? null : response.json();
  }

  async runs(repo, workflow, since) {
    const query = new URLSearchParams({ branch: "main", created: `>=${since.toISOString()}`, per_page: "100" });
    const result = await this.request(`${repo}/actions/workflows/${workflow}/runs?${query}`);
    if (result.total_count >= 100) throw new Error("Too many workflow runs; dispatch suppressed");
    // PR checks and runs from development branches must not suppress main dispatch.
    return result.workflow_runs.filter(run => run.head_branch === "main" && ["schedule", "workflow_dispatch"].includes(run.event));
  }

  async dispatch(repo, workflow, inputs = {}) {
    await this.request(`${repo}/actions/workflows/${workflow}/dispatches`, { ref: "main", inputs });
  }
}

export function retryAllowed(runs, now, maxRuns = 2) {
  if (runs.some(run => run.status !== "completed" || run.conclusion === "success")) return false;
  if (runs.length >= maxRuns) return false;
  return runs.every(run => now - new Date(run.updated_at || run.created_at) >= 30 * MINUTE);
}

async function enabled(api, repo, workflow) {
  const state = await api.request(`${repo}/actions/workflows/${workflow}`);
  if (state.state !== "active") throw new Error(`${repo}/${workflow} is ${state.state}; manual re-enable required`);
}

export async function tick(env, now = new Date(), api = new GitHub(env.AUTOMATION_PAT)) {
  const publicRepo = env.PUBLIC_REPOSITORY;
  const privateRepo = env.PRIVATE_REPOSITORY;
  for (const repo of [publicRepo, privateRepo]) {
    if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo || "")) throw new Error("Invalid repository configuration");
  }
  const result = { checked_at: now.toISOString(), actions: [], errors: [] };
  const attempt = async (name, action) => {
    try { await action(); } catch (error) { result.errors.push(`${name}: ${error.message}`); }
  };

  await attempt("prices", async () => {
    const workflow = "daily-stock-prices.yml";
    await enabled(api, publicRepo, workflow);
    const cycle = priceCycle(now);
    // Allow GitHub's schedule a 30-minute head start, then recover missing runs.
    if (now - cycle < 30 * MINUTE) return;
    const runs = await api.runs(publicRepo, workflow, cycle);
    if (retryAllowed(runs, now)) {
      await api.dispatch(publicRepo, workflow, { lookback_days: "7" });
      result.actions.push("prices dispatched");
    } else if (runs.length && runs.every(run => run.status === "completed" && run.conclusion !== "success")) {
      result.errors.push("prices: unsuccessful; bounded retry exhausted or cooling down");
    }
  });

  await attempt("sync", async () => {
    const workflow = "update-public-submodule.yml";
    await enabled(api, privateRepo, workflow);
    const [tip, pointer] = await Promise.all([
      api.request(`${publicRepo}/commits/main`),
      api.request(`${privateRepo}/contents/stock.jp?ref=main`),
    ]);
    if (!tip.sha || !pointer.sha) throw new Error("Missing commit SHA");
    if (tip.sha === pointer.sha) return;
    const pulls = await api.request(`${privateRepo}/pulls?state=open&base=main&head=${privateRepo.split("/")[0]}:automation/update-public-submodule&per_page=100`);
    if (pulls.length) {
      if (pulls.some(pr => now - new Date(pr.created_at) > 3 * 60 * MINUTE)) result.errors.push("sync: pointer PR pending more than 3 hours");
      return; // Wait for required checks and auto-merge; never bypass them.
    }
    const runs = await api.runs(privateRepo, workflow, new Date(now - DAY));
    if (runs.some(run => run.status !== "completed" || now - new Date(run.updated_at || run.created_at) < 30 * MINUTE)) return;
    // A successful no-op earlier in the day does not suppress a new public SHA.
    const failures = runs.filter(run => run.conclusion !== "success");
    if (failures.length >= 2) throw new Error("Two failed syncs in 24 hours; manual review required");
    await api.dispatch(privateRepo, workflow);
    result.actions.push("sync dispatched");
  });

  await attempt("health", async () => {
    const workflow = "operation-health.yml";
    await enabled(api, privateRepo, workflow);
    // Independent of GitHub cron, including weekends and upstream failures.
    const runs = await api.runs(privateRepo, workflow, new Date(now - 6 * 60 * MINUTE));
    if (!runs.length) {
      await api.dispatch(privateRepo, workflow);
      result.actions.push("health dispatched");
    }
  });
  if (api.expiration) {
    const expiration = new Date(api.expiration.replace(" UTC", "Z").replace(" ", "T"));
    if (!Number.isFinite(expiration.getTime())) result.errors.push("scheduler PAT: invalid expiration header");
    else if (expiration - now <= 14 * DAY) result.errors.push("scheduler PAT expires within 14 days; rotate and redeploy");
  }
  return result;
}

export default {
  async scheduled(controller, env) {
    const result = await tick(env, new Date(controller.scheduledTime));
    console.log(JSON.stringify(result));
    if (result.errors.length) throw new Error(result.errors.join("; "));
  },
  fetch() { return new Response("Not found", { status: 404 }); },
};

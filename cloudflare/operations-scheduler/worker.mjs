// Retired compatibility worker. Never dispatch, collect, retry, or use credentials.
// Deploying this source is a separate, explicitly approved migration action.
export async function tick(_env, now = new Date()) {
  return {
    checked_at: now.toISOString(),
    status: "RETIRED_ASSISTANT_OWNED",
    actions: [],
    errors: [],
  };
}

export default {
  async scheduled(controller, env) {
    console.log(JSON.stringify(await tick(env, new Date(controller.scheduledTime))));
  },
  fetch() { return new Response("Not found", { status: 404 }); },
};

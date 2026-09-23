// AgentMail -> GitHub relay.
//
// GitHub's repository_dispatch endpoint refuses any body key it does not know,
// and AgentMail's webhook body has several. This worker accepts AgentMail's
// call and forwards a minimal dispatch to GitHub. Secrets:
//   GITHUB_TOKEN  fine-grained token, Contents read/write on the repo
//   RELAY_SECRET  shared with AgentMail as the X-Relay-Secret header
// Vars:
//   REPO          owner/name

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("uarb-agent relay", { status: 200 });
    }
    if (env.RELAY_SECRET && request.headers.get("x-relay-secret") !== env.RELAY_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    let event = {};
    try {
      event = await request.json();
    } catch (e) {
      // an empty or non-JSON ping still wakes the workflow
    }
    const type = typeof event.event_type === "string" && event.event_type.startsWith("message.received") ? "message.received" : "mail";
    const inbox = event.message && event.message.inbox_id ? String(event.message.inbox_id) : "";
    const r = await fetch(`https://api.github.com/repos/${env.REPO}/dispatches`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "uarb-agent-relay",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ event_type: type, client_payload: { inbox, event_id: event.event_id || "" } }),
    });
    return new Response(r.status === 204 ? "dispatched" : `github ${r.status}`, { status: r.status === 204 ? 200 : 502 });
  },
};

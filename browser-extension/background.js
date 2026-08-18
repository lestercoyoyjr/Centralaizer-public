// Background service worker — the only place that talks to the hub.
// Content scripts message us; we fetch localhost with the extension's
// host_permissions, which bypasses page CORS entirely. (The hub also sends
// CORS headers for these sites as a belt-and-suspenders for direct fetches.)

const HUB = "http://localhost:3001";

async function handle(msg) {
  if (msg.type === "search") {
    const url = `${HUB}/api/search?` + new URLSearchParams({ q: msg.q, n: String(msg.n || 5) });
    const r = await fetch(url);
    return { ok: r.ok, data: await r.json() };
  }
  if (msg.type === "write") {
    const r = await fetch(`${HUB}/api/memories`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        agent_id: msg.agent_id,
        content: msg.content,
        memory_type: msg.memory_type || "semantic",
      }),
    });
    return { ok: r.ok, data: await r.json() };
  }
  return { ok: false, error: "unknown message type" };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  handle(msg)
    .then(sendResponse)
    .catch((e) => sendResponse({ ok: false, error: String(e) }));
  return true; // keep the channel open for the async response
});

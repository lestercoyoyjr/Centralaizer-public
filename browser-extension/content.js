// Content script — injects a small "Recall / Remember" toolbar into browser AI
// chats and bridges the prompt box to the local Centralaizer hub.
//
// DOM selectors for each site's prompt box are brittle by nature (these apps
// change their markup often). PROMPT_SELECTORS lists known ones per host with a
// generic fallback; update them here if a site stops working.
(() => {
  const HOST = location.hostname;
  const AGENT_ID =
    HOST.includes("openai") || HOST.includes("chatgpt") ? "chatgpt-web" :
    HOST.includes("gemini") ? "gemini-web" :
    HOST.includes("qwen") ? "qwen-web" :
    HOST.includes("claude") ? "claude-web" :
    HOST.includes("perplexity") ? "perplexity-web" :
    HOST.includes("poe") ? "poe-web" :
    HOST.includes("copilot") ? "copilot-web" :
    HOST.includes("deepseek") ? "deepseek-web" :
    HOST.includes("grok") ? "grok-web" :
    HOST.replace(/^www\.|\.(com|ai|google\.com)$/g, "").split(".")[0] + "-web";

  // Auto-recall-on-send (opt-in, persisted). When on, pressing Enter first pulls
  // relevant memories into the prompt, then submits. Default OFF and fail-safe:
  // any error just lets your Enter through unchanged.
  let autoOn = false;     // auto-recall on Enter
  let captureOn = false;  // auto-Remember the assistant's reply after an auto-send
  try {
    chrome.storage?.local.get(["czrAuto", "czrCapture"], (v) => {
      autoOn = !!(v && v.czrAuto); captureOn = !!(v && v.czrCapture);
      refreshAutoBtn(); refreshCaptureBtn();
    });
  } catch (_) {}

  const PROMPT_SELECTORS = [
    "#prompt-textarea",                 // ChatGPT (contenteditable div)
    "div[contenteditable='true']",      // ChatGPT / Gemini rich editors
    "rich-textarea .ql-editor",         // Gemini
    "textarea",                         // Qwen and generic fallback
  ];

  function findPrompt() {
    const active = document.activeElement;
    if (active && (active.tagName === "TEXTAREA" || active.isContentEditable)) return active;
    for (const sel of PROMPT_SELECTORS) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  function getText(el) {
    if (!el) return "";
    return (el.tagName === "TEXTAREA" || el.tagName === "INPUT") ? el.value : el.innerText;
  }

  function setText(el, text) {
    if (!el) return;
    el.focus();
    if (el.tagName === "TEXTAREA" || el.tagName === "INPUT") {
      el.value = text;
      el.dispatchEvent(new Event("input", { bubbles: true }));
    } else {
      // contenteditable — replace content and notify the editor
      el.innerText = text;
      el.dispatchEvent(new InputEvent("input", { bubbles: true }));
    }
  }

  function send(msg) {
    return new Promise((resolve) => chrome.runtime.sendMessage(msg, resolve));
  }

  // Pure: format hits into a context block to prepend to the prompt.
  function formatRecall(hits) {
    if (!hits || !hits.length) return "";
    const lines = ["[Relevant memories from my shared hub:]"];
    for (const h of hits) lines.push(`- (${h.memory_type}) ${h.content}`);
    return lines.join("\n") + "\n\n";
  }

  function toast(text, ok = true) {
    const t = document.createElement("div");
    t.className = "czr-toast" + (ok ? "" : " czr-err");
    t.textContent = text;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 2600);
  }

  async function onRecall() {
    const el = findPrompt();
    const q = getText(el).trim();
    if (!q) return toast("Type your question first, then Recall.", false);
    const res = await send({ type: "search", q, n: 4 });
    if (!res || !res.ok) return toast("Hub unreachable — is Centralaizer running?", false);
    const relevant = res.data.filter((h) => h.score >= 0.7);
    if (!relevant.length) return toast("No relevant memories found.");
    setText(el, formatRecall(relevant) + q);
    toast(`Recalled ${relevant.length} memory(ies) into your prompt.`);
  }

  async function onRemember() {
    const sel = String(window.getSelection() || "").trim();
    const el = findPrompt();
    const content = sel || getText(el).trim();
    if (!content) return toast("Select text (or type) to remember.", false);
    const res = await send({ type: "write", agent_id: AGENT_ID, content });
    if (!res || !res.ok) return toast("Hub unreachable — is Centralaizer running?", false);
    const s = res.data && res.data.status;
    toast(s === "quarantined" ? "Sent to quarantine for review." : "Remembered ✓");
  }

  // Programmatically submit the prompt: prefer the site's send button (reliable);
  // a synthetic Enter usually won't trigger the editor, so the button is primary.
  function submitPrompt() {
    const btn = document.querySelector(
      "button[data-testid='send-button'], button[aria-label*='Send'], button[aria-label*='send'], button[type='submit']"
    );
    if (btn && !btn.disabled) { btn.click(); return true; }
    return false;
  }

  // Enter interceptor for auto mode. Fail-safe: only acts when auto is on, you're
  // in the prompt, and there's un-augmented text; on any miss it does nothing and
  // your Enter behaves normally.
  document.addEventListener("keydown", async (e) => {
    if (!autoOn || e.key !== "Enter" || e.shiftKey || e.isComposing) return;
    const el = findPrompt();
    if (!el || e.target !== el) return;
    const text = getText(el).trim();
    if (!text || text.startsWith("[Relevant memories")) return; // empty or already augmented → let it send
    e.preventDefault();
    e.stopPropagation();
    try {
      const res = await send({ type: "search", q: text, n: 4 });
      if (res && res.ok) {
        const relevant = res.data.filter((h) => h.score >= 0.7);
        if (relevant.length) setText(el, formatRecall(relevant) + text);
      }
    } catch (_) { /* ignore — still submit below */ }
    // submit; if no send button found, the memories are injected and one more
    // Enter (now starting with the memory block) sends as normal.
    setTimeout(submitPrompt, 0);
    // optional auto-Remember: grab the assistant's reply once it has rendered
    if (captureOn) setTimeout(captureReply, 5000);
  }, true);

  // Best-effort: read the latest assistant message. These selectors are brittle
  // (the sites change markup often) — update them if capture stops working.
  function lastAssistantText() {
    const perHost =
      HOST.includes("gemini") ? "message-content, .model-response-text" :
      HOST.includes("qwen") ? "[class*='assistant'], .markdown" :
      "[data-message-author-role='assistant']"; // chatgpt / openai
    const nodes = document.querySelectorAll(perHost + ",[data-message-author-role='assistant']");
    const el = nodes[nodes.length - 1];
    return el ? el.innerText.trim() : "";
  }

  async function captureReply() {
    const text = lastAssistantText();
    if (!text || text.length < 20) return;
    const res = await send({ type: "write", agent_id: AGENT_ID, content: text.slice(0, 4000), memory_type: "episodic" });
    if (res && res.ok) toast("Captured the reply to memory.");
  }

  function refreshCaptureBtn() {
    const b = document.getElementById("czr-capture");
    if (b) { b.textContent = "Capture: " + (captureOn ? "on" : "off"); b.classList.toggle("czr-on", captureOn); }
  }

  function refreshAutoBtn() {
    const b = document.getElementById("czr-auto");
    if (b) { b.textContent = "Auto: " + (autoOn ? "on" : "off"); b.classList.toggle("czr-on", autoOn); }
  }

  function mountToolbar() {
    if (document.getElementById("czr-bar")) return;
    const bar = document.createElement("div");
    bar.id = "czr-bar";
    bar.innerHTML =
      '<span class="czr-logo">🧠</span>' +
      '<button id="czr-recall" title="Pull relevant memories into your prompt">Recall</button>' +
      '<button id="czr-remember" title="Save the selected text (or prompt) to the hub">Remember</button>' +
      '<button id="czr-auto" title="Auto-recall: inject relevant memories when you press Enter">Auto: off</button>' +
      '<button id="czr-capture" title="Auto-Remember: save the assistant reply after an auto-send (needs Auto on)">Capture: off</button>';
    document.body.appendChild(bar);
    bar.querySelector("#czr-recall").addEventListener("click", onRecall);
    bar.querySelector("#czr-remember").addEventListener("click", onRemember);
    bar.querySelector("#czr-auto").addEventListener("click", () => {
      autoOn = !autoOn;
      try { chrome.storage?.local.set({ czrAuto: autoOn }); } catch (_) {}
      refreshAutoBtn();
      toast("Auto-recall " + (autoOn ? "on — memories inject on Enter" : "off"));
    });
    bar.querySelector("#czr-capture").addEventListener("click", () => {
      captureOn = !captureOn;
      try { chrome.storage?.local.set({ czrCapture: captureOn }); } catch (_) {}
      refreshCaptureBtn();
      toast(captureOn ? "Auto-capture on — replies saved after auto-send" : "Auto-capture off");
    });
    refreshAutoBtn();
    refreshCaptureBtn();
  }

  // SPA navigations can wipe the toolbar — re-mount if it disappears.
  mountToolbar();
  new MutationObserver(() => { if (!document.getElementById("czr-bar")) mountToolbar(); })
    .observe(document.documentElement, { childList: true, subtree: true });
})();

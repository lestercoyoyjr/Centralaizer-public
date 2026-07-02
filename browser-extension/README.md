# Centralaizer Bridge (browser extension)

Browser AIs (ChatGPT, Claude.ai, Gemini, Qwen, Perplexity, Poe, Copilot,
DeepSeek, Grok) run in the cloud and can't reach your local MCP hub. This Manifest V3 extension bridges them: it adds a small
**Recall / Remember** toolbar to those chat pages that talks to your local hub
at `http://localhost:3001` — so the same shared memory your local agents use
becomes available in the browser too. Nothing leaves your machine.

## How it works

- **Recall** — takes what you've typed in the prompt box, searches the hub, and
  prepends the most relevant memories (score ≥ 0.7) into your prompt before you send.
- **Remember** — saves the selected text (or your prompt) to the hub as a memory,
  tagged with the site as the agent (`chatgpt-web`, `gemini-web`, `qwen-web`),
  so it shows up under "Active agents" in the Memory Viewer.
- **Auto** (toggle, off by default) — when on, pressing **Enter** first pulls
  relevant memories (score ≥ 0.7) into your prompt, then submits — so recall
  happens without clicking. Fail-safe: on any error your Enter sends normally.
- **Capture** (toggle, off by default) — when on (and Auto on), the assistant's
  reply is saved to the hub a few seconds after an auto-send, tagged by site.
  Best-effort: reply selectors are brittle and may need updating per site.

> Privacy note: anything recalled into a cloud chat becomes part of the prompt
> sent to that provider (OpenAI/Google). Writes are PII-masked, so placeholders
> (`EMAIL_1`…) are what travel — but the substantive text does reach the cloud.
> This is unavoidable for a cloud model to "use" a memory.

All hub calls go through the background service worker, which is allowed to reach
`localhost` via `host_permissions` — so it works regardless of page CORS. The hub
also CORS-allows these specific sites.

## Install (load unpacked)

1. Make sure the hub is running: `./setup_and_run.sh` (or `python main.py`).
2. Open `chrome://extensions` (or `edge://extensions`), enable **Developer mode**.
3. **Load unpacked** → select this `browser-extension/` folder.
4. Open ChatGPT / Gemini / Qwen Chat — a 🧠 toolbar appears bottom-right.

## Caveats

- **Prompt-box selectors are brittle.** These sites change their markup often. If
  Recall/Remember can't find the input, update `PROMPT_SELECTORS` in `content.js`.
- Requires the hub running locally; if it's down, the buttons show a clear toast
  and do nothing (they never block the page).
- This is an MVP: manual buttons, not automatic injection. A future version could
  auto-recall on send.

# Making agents use the memory hub by default

Connecting an agent to Centralaizer (via the MCP config) only makes the memory
*tools available*. Whether the agent actually **uses** them is up to the model.
Two layers make that automatic:

## Layer 1 — server instructions (already on, reaches every agent)

The MCP server sends usage instructions to **every** client on connect (see
`core/mcp/server.py`). No per-app setup needed — Claude Desktop, Cursor, Claude
Code, and VS Code Copilot all receive: *"search the hub at the start of a task,
write durable facts when you finish."* This is the universal nudge.

## Layer 2 — per-app instruction files (stronger, optional)

For apps that support a custom-instructions file, drop the block below in so the
guidance is reinforced in the model's own context. **Paste this block:**

```md
## Shared memory (Centralaizer)
You have a shared, cross-agent memory hub via the `centralaizer` MCP server.
- Before starting any task, call `memory_search` with the request to retrieve
  relevant prior context (decisions, configs, conventions, past outcomes).
- After completing a task or learning a durable fact, call `memory_write` to
  persist it so other agents can reuse it.
- For multi-step work, call `session_start` at the beginning and `session_end`
  at the end. If a named workflow may exist, call `skill_get` first.
```

Where each app reads it:

| App | File | Scope |
|-----|------|-------|
| **Claude Code** | `~/.claude/CLAUDE.md` (global) or `<project>/CLAUDE.md` | user / project |
| **Cursor** | `<project>/.cursor/rules/centralaizer.mdc` or `.cursorrules` | project |
| **VS Code Copilot** | `<project>/.github/copilot-instructions.md` | project (repo) |
| **Claude Desktop** | *(no instructions file — relies on Layer 1)* | — |

## Layer 3 — automatic recall (Claude Code hook)

Layers 1–2 *nudge* the model; the model still decides whether to call
`memory_search`. To make recall **guaranteed**, Claude Code can run a
`UserPromptSubmit` hook that queries the hub on every prompt and injects the
most relevant memories into context — no reliance on the model choosing.

`scripts/recall_hook.py` does this: it reads the prompt, calls `GET /api/search`,
and injects hits scoring above a relevance threshold (silent below it, and
fails open if the hub is down so it never blocks a prompt). Enable it in
`.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "timeout": 5,
          "command": "<repo>/.venv/bin/python <repo>/scripts/recall_hook.py" } ] }
    ]
  }
}
```

For recall in *every* Claude Code session (not just this repo), put the same
block in `~/.claude/settings.json`. Open `/hooks` once (or restart) to activate.
Only Claude Code supports prompt hooks today; other apps rely on layers 1–2.

## Requirements for sharing to work

- The hub must be **running** (`./setup_and_run.sh` or `python main.py`) whenever
  agents use it — it's local, so if it's off there is nothing to read or write.
- Each agent must have been **restarted** after connecting (configs are read at startup).
- Sharing is **same-machine only**. MCP-capable local apps connect directly.
  Browser assistants (ChatGPT, Gemini, Qwen Chat) can't reach `localhost` over
  MCP, but the **`browser-extension/`** bridge connects them via Recall/Remember
  buttons — see [browser-extension/README.md](../browser-extension/README.md).

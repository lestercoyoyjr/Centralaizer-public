# Connecting your AI agents to LocalMem

LocalMem exposes a standard MCP endpoint at `http://localhost:3000/mcp`.
Any MCP-compatible agent can connect with a one-line config change.

---

## Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)
or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "localmem": {
      "url": "http://localhost:3000/mcp"
    }
  }
}
```

Restart Claude Desktop. You will see `localmem` listed in the tools panel.

---

## Cursor / Claude Code

In your project `.cursor/mcp.json` (or global `~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "localmem": {
      "url": "http://localhost:3000/mcp"
    }
  }
}
```

---

## Open WebUI (Ollama front-end)

Open WebUI supports MCP via its tool plugin system.
Add a new tool with endpoint `http://localhost:3000/mcp`.

---

## VS Code Copilot / GitHub Copilot Chat

Add to `.vscode/settings.json`:

```json
{
  "github.copilot.chat.mcpServers": {
    "localmem": {
      "url": "http://localhost:3000/mcp"
    }
  }
}
```

---

## Available MCP tools

| Tool | What it does |
|---|---|
| `memory_write` | Persist a memory (trust-gated, PII-masked) |
| `memory_search` | Multi-signal retrieval (semantic + FTS5 + graph) |
| `memory_context_get` | Read shared session context |
| `memory_context_set` | Write shared session context |
| `skill_get` | Retrieve a named workflow/playbook |
| `skill_record` | Record skill outcome (drives promotion) |
| `session_start` | Begin an episodic session |
| `session_end` | Close session, persist as episodic memory |

---

## Suggested agent system prompt addition

```
You have access to a local memory hub via MCP (localmem).
- Before starting any task, call memory_search to retrieve relevant context.
- After completing a task, call memory_write to persist the outcome.
- For multi-step workflows, call session_start at the beginning and session_end at the end.
- If a skill exists for the current task, call skill_get first, then skill_record when done.
```

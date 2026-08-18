# Cursor auto-recall with Centralaizer

Cursor has **no hook mechanism** (unlike Claude Code's `UserPromptSubmit`), so recall can't be
*forced*. The strongest lever is a **Cursor Rule** — auto-loaded into every conversation — that
tells Cursor's AI to query the memory hub. Combined with the MCP connection (which lets it actually
call the tools), this makes recall happen without you asking.

## 1. MCP connection (once)

Confirm `~/.cursor/mcp.json` contains the hub (the agent-connect installer adds it):

```json
{ "mcpServers": { "centralaizer": { "url": "http://localhost:3000/mcp" } } }
```

## 2. The rule — pick the scope you want

**Per-project** (this repo already has it): `.cursor/rules/centralaizer-memory.mdc` with
`alwaysApply: true`. Drop the same file into any other repo to enable it there:

```bash
mkdir -p /path/to/repo/.cursor/rules && cp .cursor/rules/centralaizer-memory.mdc /path/to/repo/.cursor/rules/
```

**Global (every Cursor project — recommended for "any topic anywhere"):** Cursor stores global rules
in Settings, not a file, so paste this into **Cursor Settings → Rules → User Rules**:

```
This machine runs Centralaizer, a local shared-memory hub (MCP server `centralaizer`,
http://localhost:3000, zero cloud egress) holding context from every AI tool I use.
- Before starting any non-trivial task, call `memory_search` with my request (or its key terms)
  to pull relevant prior decisions, fixes, and facts; use anything relevant and say when you did.
- After a durable decision, discovered fact, or landed fix, call `memory_write` with a concise
  1–2 sentence summary so the next session in any tool has it.
- Skip both for trivial edits. Never invent memories — persist only what actually happened.
```

## Honest limits

A rule is a **strong nudge, not a hard hook** — the model still decides whether to call the tool, so
recall in Cursor is more reliable than nothing but less guaranteed than Claude Code's hook (which
injects memories unconditionally). Capture (write) via the editor-sweep is already automatic and
tool-independent; this only strengthens the *read* side.

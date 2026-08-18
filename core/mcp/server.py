"""
Local MCP server — exposes memory tools to every MCP-compatible agent.

Tools exposed
-------------
memory_write   — write a memory (goes through trust gate)
memory_search  — multi-signal retrieval (semantic + FTS5 + graph)
memory_context — return current shared context store for the session
memory_list    — list recent memories (paginated)
skill_get      — retrieve a named skill/workflow
skill_record   — record a skill use + outcome (drives promotion)
session_start  — begin a tracked episodic session
session_end    — close a session, store summary

All operations are local. No network call leaves localhost.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

from fastmcp import FastMCP
from pydantic import Field

from config.settings import settings
from core.memory.engine import (
    write_memory, search_memory, approve_quarantine, reject_quarantine,
)
from core.memory.models import WriteRequest, MemoryType
from core.privacy.filter import mask
from core.storage.database import get_conn

# ── Shared context store (gap #5: CA-MCP) ─────────────────────────────────────
# Keyed by session_id so multiple agents can share context within a workflow.
_context_store: dict[str, dict] = {}

mcp = FastMCP(
    name="localmem",
    # These instructions are sent to every MCP client on connect — the single
    # place that reaches ALL connected agents — so they nudge the model to use
    # the hub by default instead of only when explicitly asked.
    instructions=(
        "This is a SHARED, cross-agent memory hub — treat it as your long-term memory. "
        "Other AI tools on this machine read and write the same hub, so anything you store "
        "here becomes available to all of them.\n"
        "Use it by default, without being asked:\n"
        "1. At the START of a task, call memory_search with the user's request to pull in "
        "relevant prior context (decisions, configs, conventions, past outcomes) before you act.\n"
        "2. When you learn a durable fact or finish a task, call memory_write to persist it "
        "(facts → semantic, events → episodic, how-tos → procedural).\n"
        "3. For multi-step work that spans agents, call session_start at the beginning and "
        "session_end at the end.\n"
        "4. If a named workflow might exist, call skill_get first, then skill_record the outcome.\n"
        "Keep writes concise and factual; speculative or low-confidence writes are held for "
        "human review by the trust gate."
    ),
)


# ── memory_write ───────────────────────────────────────────────────────────────

@mcp.tool()
def memory_write(
    agent_id: str,
    content: str,
    memory_type: str = "semantic",
    metadata: str = "{}",
    owner: str = "shared",
    session_id: str | None = None,
) -> str:
    """
    Persist a memory. Content is PII-masked before storage.
    Returns JSON with status (stored|quarantined|merged), id, trust score.
    """
    masked = mask(content)
    req = WriteRequest(
        agent_id=agent_id,
        content=masked.text,
        memory_type=MemoryType(memory_type),
        metadata=json.loads(metadata) if isinstance(metadata, str) else metadata,
        owner=owner,
    )
    result = write_memory(req)

    # Update shared context store if inside a session
    if session_id and result["status"] == "stored":
        _context_store.setdefault(session_id, {"memory_ids": [], "agent_ids": []})
        _context_store[session_id]["memory_ids"].append(result["id"])
        if agent_id not in _context_store[session_id]["agent_ids"]:
            _context_store[session_id]["agent_ids"].append(agent_id)

    return json.dumps(result)


# ── memory_search ──────────────────────────────────────────────────────────────

@mcp.tool()
def memory_search(
    query: str,
    agent_id: str | None = None,
    memory_type: str | None = None,
    owner: str | None = None,
    n: int = 10,
) -> str:
    """
    Multi-signal memory retrieval: semantic vector + FTS5 full-text + graph expansion.
    Returns JSON array of memories ordered by relevance score.
    """
    mtype = MemoryType(memory_type) if memory_type else None
    results = search_memory(query, agent_id=agent_id, memory_type=mtype, owner=owner, n=n)
    return json.dumps([
        {
            "id": r.memory.id,
            "content": r.memory.content,
            "memory_type": r.memory.memory_type.value,
            "score": r.score,
            "matched_via": r.matched_via,
            "metadata": r.memory.metadata,
            "trust_score": r.memory.trust_score,
            "created_at": r.memory.created_at.isoformat(),
        }
        for r in results
    ])


# ── memory_context (shared context store) ─────────────────────────────────────

@mcp.tool()
def memory_context_get(session_id: str) -> str:
    """Return the shared context store for a session (gap #5 — CA-MCP)."""
    ctx = _context_store.get(session_id, {})
    return json.dumps(ctx)


@mcp.tool()
def memory_context_set(session_id: str, key: str, value: str) -> str:
    """Write a key-value pair into the shared context store for a session."""
    _context_store.setdefault(session_id, {})
    _context_store[session_id][key] = value
    return json.dumps({"ok": True, "session_id": session_id, "key": key})


# ── skill_get / skill_record ───────────────────────────────────────────────────

@mcp.tool()
def skill_get(name: str) -> str:
    """Retrieve a named skill/workflow template from procedural memory."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM skills WHERE name=? AND level != 'draft'", (name,)
        ).fetchone()
    if not row:
        return json.dumps({"error": f"Skill '{name}' not found or still in draft"})
    return json.dumps({
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "template": row["template"],
        "level": row["level"],
        "use_count": row["use_count"],
    })


@mcp.tool()
def skill_record(name: str, success: bool) -> str:
    """Record a skill use outcome — drives the draft → active → crystallized promotion."""
    with get_conn() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """UPDATE skills
               SET use_count=use_count+1,
                   success_count=success_count+?,
                   updated_at=?
               WHERE name=?""",
            (1 if success else 0, now, name),
        )
        row = conn.execute("SELECT use_count, success_count FROM skills WHERE name=?", (name,)).fetchone()
    return json.dumps({"ok": True, "use_count": row["use_count"] if row else 0})


# ── session_start / session_end ────────────────────────────────────────────────

@mcp.tool()
def session_start(agent_id: str, caused_by: str | None = None) -> str:
    """Begin an episodic session. Returns a session_id to pass in subsequent tool calls."""
    import uuid
    sid = str(uuid.uuid4())
    _context_store[sid] = {
        "agent_id": agent_id,
        "caused_by": caused_by,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "memory_ids": [],
        "agent_ids": [agent_id],
    }
    return json.dumps({"session_id": sid})


@mcp.tool()
def session_end(session_id: str, summary: str) -> str:
    """Close a session and persist it as an episodic memory."""
    ctx = _context_store.pop(session_id, {})
    req = WriteRequest(
        agent_id=ctx.get("agent_id", "system"),
        content=summary,
        memory_type=MemoryType.episodic,
        metadata={
            "session_id": session_id,
            "memory_ids": ctx.get("memory_ids", []),
            "caused_by": ctx.get("caused_by"),
        },
    )
    result = write_memory(req)
    return json.dumps({"ok": True, "session_memory_id": result.get("id")})


def run_server() -> None:
    mcp.run(transport="streamable-http", host=settings.mcp_host, port=settings.mcp_port)

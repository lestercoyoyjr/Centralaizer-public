"""FastAPI web app — Memory Viewer UI. Runs on localhost:3001."""
from __future__ import annotations
import io, json, sys, zipfile
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import jinja2
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from config.settings import settings
from core.memory.engine import (
    write_memory, search_memory, delete_memory,
    approve_quarantine, reject_quarantine,
    list_memories, list_quarantine, get_stats,
    run_decay, promote_skills,
)
from core.memory.models import WriteRequest, MemoryType
from core.storage import graph_store
from core.storage.database import get_conn

BASE = Path(__file__).parent

app = FastAPI(title="Centralaizer", docs_url="/api/docs")

# CORS — let the browser-bridge extension's content scripts on these AI sites
# reach the hub directly. Narrow allowlist (not "*") so a random site you visit
# can't probe your local memory. The server binds 127.0.0.1, so this is only
# ever reachable from your own machine anyway.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://chatgpt.com", "https://chat.openai.com",
        "https://gemini.google.com", "https://chat.qwen.ai",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

# Use jinja2 directly — avoids starlette 1.x + jinja2 3.1.6 cache-key bug
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(BASE / "templates")),
    autoescape=True,
)

def render(name: str, ctx: dict) -> HTMLResponse:
    tmpl = _jinja_env.get_template(name)
    return HTMLResponse(tmpl.render(**ctx))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    stats = get_stats()
    memories = list_memories(limit=15)
    quarantine = list_quarantine(limit=10)
    with get_conn() as conn:
        skills = [dict(r) for r in conn.execute(
            "SELECT * FROM skills ORDER BY level, success_count DESC LIMIT 20"
        ).fetchall()]
    return render("index.html", {
        "stats": stats, "memories": memories,
        "quarantine": quarantine, "skills": skills, "page": "dashboard",
    })


@app.get("/memories", response_class=HTMLResponse)
async def memories_page(request: Request,
                         memory_type: str = "", owner: str = "",
                         q: str = "", offset: int = 0):
    PAGE = 50
    if q:
        mtype = MemoryType(memory_type) if memory_type else None
        # ponytail: paginate by over-fetching then slicing — fine at this scale
        results = search_memory(q, memory_type=mtype, owner=owner or None, n=offset + PAGE + 1)
        window = results[offset:offset + PAGE]
        has_next = len(results) > offset + PAGE
        memories = [dict(
            id=r.memory.id, content=r.memory.content,
            memory_type=r.memory.memory_type.value,
            agent_id=r.memory.agent_id, trust_score=r.memory.trust_score,
            decayed_score=r.memory.decayed_score, access_count=r.memory.access_count,
            owner=r.memory.owner,
            accessed_at=r.memory.accessed_at.isoformat(),
            score=r.score, matched_via=", ".join(r.matched_via),
        ) for r in window]
    else:
        memories = list_memories(memory_type=memory_type or None, owner=owner or None,
                                 limit=PAGE, offset=offset)
        has_next = len(memories) == PAGE
    stats_total = get_stats()["total"]
    # ponytail: numbered pages only for the list (search has no exact total); window if it grows
    total_pages = None if q else max(1, (stats_total + PAGE - 1) // PAGE)
    return render("memories.html", {
        "memories": memories, "total": stats_total,
        "memory_type": memory_type, "owner": owner,
        "q": q, "offset": offset, "page_size": PAGE, "has_next": has_next,
        "current_page": offset // PAGE + 1, "total_pages": total_pages,
        "page": "memories",
    })


@app.get("/quarantine", response_class=HTMLResponse)
async def quarantine_page(request: Request):
    entries = list_quarantine(limit=100)
    return render("quarantine.html", {"entries": entries, "page": "quarantine"})


@app.get("/skills", response_class=HTMLResponse)
async def skills_page(request: Request):
    with get_conn() as conn:
        skills = [dict(r) for r in conn.execute(
            "SELECT * FROM skills ORDER BY level, success_count DESC"
        ).fetchall()]
    return render("skills.html", {
        "skills": skills, "page": "skills",
        "settings": settings,
    })


# ── REST API ──────────────────────────────────────────────────────────
@app.get("/api/stats")
async def api_stats():
    return get_stats()

@app.get("/api/export")
async def api_export():
    """Download all memories (+ skills + quarantine) as a portable .zip of JSON."""
    with get_conn() as conn:
        memories   = [dict(r) for r in conn.execute("SELECT * FROM memories").fetchall()]
        skills     = [dict(r) for r in conn.execute("SELECT * FROM skills").fetchall()]
        quarantine = [dict(r) for r in conn.execute("SELECT * FROM quarantine").fetchall()]
    stamp = datetime.now(timezone.utc).isoformat()
    manifest = {
        "format": "centralaizer-export/v1",
        "exported_at": stamp,
        "counts": {"memories": len(memories), "skills": len(skills), "quarantine": len(quarantine)},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        z.writestr("memories.json", json.dumps(memories, indent=2, default=str))
        z.writestr("skills.json", json.dumps(skills, indent=2, default=str))
        z.writestr("quarantine.json", json.dumps(quarantine, indent=2, default=str))
    fname = f"centralaizer-memories-{stamp[:10]}.zip"
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )

@app.get("/api/search")
async def api_search(q: str, n: int = 5, memory_type: str = "", owner: str = "", offset: int = 0):
    """JSON memory search — powers the Claude Code recall hook and future bridges."""
    mtype = MemoryType(memory_type) if memory_type else None
    results = search_memory(q, memory_type=mtype, owner=owner or None, n=offset + n)[offset:]
    return [
        {
            "content": r.memory.content,
            "memory_type": r.memory.memory_type.value,
            "agent_id": r.memory.agent_id,
            "score": round(r.score, 4),
            "matched_via": r.matched_via,
        }
        for r in results
    ]

@app.get("/.well-known/agent-card.json")
async def agent_card():
    """A2A Agent Card — advertises Centralaizer as a shared-memory service so
    A2A-native agents can discover it (RFC 8615 well-known URI). Transport is MCP
    (localhost:MCP_PORT/mcp); this card is discovery metadata, not an A2A task
    endpoint — Centralaizer stores knowledge, it doesn't orchestrate tasks."""
    base = f"http://{settings.mcp_host}:{settings.mcp_port}/mcp"
    skill = lambda i, name, desc, tags: {
        "id": i, "name": name, "description": desc, "tags": tags,
        "inputModes": ["text/plain", "application/json"],
        "outputModes": ["application/json"],
    }
    return JSONResponse({
        "protocolVersion": "1.0",
        "name": "Centralaizer",
        "description": "Local-first shared memory hub for AI agents. A common "
                       "blackboard agents read/write via MCP — trust-gated, "
                       "PII-masked, with multi-signal retrieval. Zero cloud egress.",
        "url": base,
        "preferredTransport": "MCP",
        "version": "0.1.0",
        "provider": {"organization": "Centralaizer", "url": "https://github.com/lestercoyoyjr/Centralaizer-public"},
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            skill("memory-write", "Persist memory",
                  "Store a memory (semantic/episodic/procedural/relational); "
                  "Bayesian trust gate + PII mask, low-trust writes quarantined.",
                  ["memory", "write", "provenance"]),
            skill("memory-search", "Recall memory",
                  "Multi-signal retrieval: vector + FTS5 full-text + knowledge-graph expansion.",
                  ["memory", "search", "retrieval", "rag"]),
            skill("shared-context", "Shared session context",
                  "Read/write cross-agent context keyed by session, for agent hand-off.",
                  ["context", "handoff", "session"]),
            skill("skills", "Workflow playbooks",
                  "Retrieve and record named workflows on a draft→active→crystallized ladder.",
                  ["skill", "procedural", "playbook"]),
        ],
    })

@app.get("/api/agents")
async def api_agents():
    """Per-agent provenance: who has written how much, and how trusted."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent_id, COUNT(*) c, AVG(trust_score) t "
            "FROM memories GROUP BY agent_id ORDER BY c DESC"
        ).fetchall()
    return [{"agent_id": r["agent_id"], "count": r["c"], "avg_trust": round(r["t"], 2)} for r in rows]

@app.get("/api/graph")
async def api_graph():
    """Knowledge-graph edges with content snippets, deduped to one per unordered pair."""
    edges = graph_store.list_edges(limit=200)
    ids = {e["src"] for e in edges} | {e["dst"] for e in edges}
    content: dict[str, str] = {}
    if ids:
        ph = ",".join("?" * len(ids))
        with get_conn() as conn:
            for r in conn.execute(f"SELECT id, content FROM memories WHERE id IN ({ph})", list(ids)):
                content[r["id"]] = r["content"]
    seen, out = set(), []
    for e in edges:
        key = (*sorted((e["src"], e["dst"])), e["edge_type"])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "src": content.get(e["src"], e["src"])[:60],
            "dst": content.get(e["dst"], e["dst"])[:60],
            "edge_type": e["edge_type"],
            "weight": round(e["weight"], 3),
        })
    return out

@app.post("/api/memories")
async def api_write(body: dict):
    req = WriteRequest(**body)
    return write_memory(req)

@app.delete("/api/memories/{memory_id}")
async def api_delete(memory_id: str):
    delete_memory(memory_id)
    return {"ok": True}

@app.post("/api/quarantine/{entry_id}/approve")
async def api_approve(entry_id: str):
    ok = approve_quarantine(entry_id)
    if not ok:
        raise HTTPException(404)
    return {"ok": True}

@app.post("/api/quarantine/{entry_id}/reject")
async def api_reject(entry_id: str):
    ok = reject_quarantine(entry_id)
    if not ok:
        raise HTTPException(404)
    return {"ok": True}

@app.post("/api/manager/decay")
async def api_decay():
    with get_conn() as conn:
        scanned = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
    archived = run_decay()
    with get_conn() as conn:
        lowest = [
            {"content": r["content"][:60], "decayed_score": r["decayed_score"],
             "accessed_at": r["accessed_at"]}
            for r in conn.execute(
                "SELECT content, decayed_score, accessed_at FROM memories "
                "ORDER BY decayed_score ASC LIMIT 5"
            ).fetchall()
        ]
    return {"scanned": scanned, "archived": archived,
            "half_life_days": settings.decay_half_life_days, "lowest": lowest}

@app.post("/api/manager/promote")
async def api_promote():
    promoted = promote_skills()
    with get_conn() as conn:
        skills = [
            {"name": r["name"], "level": r["level"], "success_count": r["success_count"]}
            for r in conn.execute(
                "SELECT name, level, success_count FROM skills ORDER BY success_count DESC"
            ).fetchall()
        ]
    return {"promoted": promoted, "threshold": settings.skill_promotion_threshold, "skills": skills}

@app.post("/api/skills")
async def api_add_skill(body: dict):
    import uuid
    skill_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO skills(id,name,description,template,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (skill_id, body["name"], body.get("description",""), body.get("template",""), now, now),
        )
    return {"ok": True, "id": skill_id}


def run_ui():
    import uvicorn
    uvicorn.run(app, host=settings.ui_host, port=settings.ui_port, log_level="warning")

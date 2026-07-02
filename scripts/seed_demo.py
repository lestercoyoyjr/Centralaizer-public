#!/usr/bin/env python
"""
Seed Centralaizer with demo data for screenshots / walkthroughs.

Populates every surface of the app:
  - memories across all four types (semantic / episodic / procedural / relational)
    from several agents, including one with PII to show masking, and a few that
    are deliberately back-dated so the decay column shows variation;
  - quarantine entries (hedged, low-trust writes that the trust gate holds back);
  - skills at each rung of the promotion ladder (draft / active / crystallized).

Usage:
    python scripts/seed_demo.py            # add demo data on top of what's there
    python scripts/seed_demo.py --reset    # wipe existing data first, then seed

Writes go through the real engine (trust gate, PII masking, vector + graph
indexing), so the result is exactly what a live system would hold.
Requires Ollama running for embeddings (the setup script installs it).
"""
from __future__ import annotations
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import settings
from core.memory.engine import write_memory, run_decay
from core.memory.models import WriteRequest, MemoryType
from core.storage import vector_store, graph_store
from core.storage.database import get_conn

# (agent_id, memory_type, content, days_since_access)
MEMORIES = [
    ("claude-desktop", "semantic",   "the vector store uses cosine similarity over 768-dimensional local embeddings.", 0),
    ("claude-desktop", "semantic",   "retrieval fuses three signals: vector similarity, full-text search, and a knowledge graph.", 3),
    ("cursor",         "semantic",   "the embedding model is required to run; the larger reasoning model is optional.", 12),
    ("cursor",         "procedural", "to run the test suite, change into the tests directory and run pytest.", 1),
    ("claude-code",    "procedural", "to install everything and launch, run the setup script and confirm the plan.", 0),
    ("claude-code",    "procedural", "before a release: run the tests, tag the commit, then push to the main branch.", 21),
    ("claude-code",    "episodic",   "connected four local editors to the memory hub and seeded demo data for review.", 0),
    ("vscode-copilot", "episodic",   "strengthened the trust gate so speculative writes are flagged for human review.", 7),
    ("vscode-copilot", "relational", "the trust gate depends on each agent's reputation prior.", 30),
    ("open-webui",     "relational", "graph edges link memories that are semantically similar to one another.", 5),
    ("open-webui",     "semantic",   "reach the on-call engineer at oncall@example.com or call 555-0142 for incidents.", 0),
]

# hedged / uncertain writes — the trust gate routes these to quarantine
QUARANTINE = [
    ("cursor",         "i think the cache might use redis. maybe. not totally sure though."),
    ("open-webui",     "possibly the api runs on port 8000, but i could be wrong about that."),
    ("vscode-copilot", "i believe we might deploy on fridays, perhaps. not certain."),
]

# (name, description, template, use_count, success_count, level)
SKILLS = [
    ("triage-bug",     "Reproduce, isolate, and label an incoming bug report",   "1. reproduce 2. bisect 3. label", 2,  1,  "draft"),
    ("summarize-pr",   "Summarize a pull request for reviewers",                 "1. read diff 2. group changes 3. note risks", 4, 3, "draft"),
    ("run-test-suite", "Run the full pytest suite and report failures",          "cd tests && pytest -q", 9, 7, "active"),
    ("cut-release",    "Tag, build, and publish a release",                      "1. tests 2. tag 3. push 4. notes", 24, 20, "crystallized"),
]


def reset() -> None:
    with get_conn() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM memories").fetchall()]
        c.execute("DELETE FROM memories")
        c.execute("DELETE FROM quarantine")
        c.execute("DELETE FROM skills")
    for mid in ids:
        try:
            vector_store.delete(mid)
            graph_store.remove_node(mid)
        except Exception:
            pass
    print(f"  reset: cleared {len(ids)} memories, plus quarantine + skills")


def seed_memories() -> None:
    now = datetime.now(timezone.utc)
    stored = quarantined = 0
    for agent, mtype, content, days in MEMORIES:
        res = write_memory(WriteRequest(agent_id=agent, content=content, memory_type=MemoryType(mtype)))
        if res["status"] in ("stored", "merged"):
            stored += 1
            if days:  # back-date so the decay column shows variation
                with get_conn() as c:
                    c.execute("UPDATE memories SET accessed_at=?, created_at=? WHERE id=?",
                              ((now - timedelta(days=days)).isoformat(),
                               (now - timedelta(days=days)).isoformat(), res["id"]))
    for agent, content in QUARANTINE:
        if write_memory(WriteRequest(agent_id=agent, content=content))["status"] == "quarantined":
            quarantined += 1
    run_decay()  # recompute decayed_score so back-dated memories show decay
    print(f"  memories: {stored} stored, {quarantined} sent to quarantine")


def seed_skills() -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as c:
        for name, desc, tmpl, use, succ, level in SKILLS:
            c.execute(
                "INSERT INTO skills(id,name,description,template,use_count,success_count,level,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), name, desc, tmpl, use, succ, level, now, now),
            )
    print(f"  skills: {len(SKILLS)} added (draft / active / crystallized)")


def main() -> None:
    if "--reset" in sys.argv:
        reset()
    print("Seeding demo data…")
    seed_memories()
    seed_skills()
    with get_conn() as c:
        total = c.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
        pend = c.execute("SELECT COUNT(*) c FROM quarantine WHERE reviewed=0").fetchone()["c"]
        skl = c.execute("SELECT COUNT(*) c FROM skills").fetchone()["c"]
    print(f"\nDone — {total} memories, {pend} pending quarantine, {skl} skills.")
    print(f"Open the Memory Viewer at http://localhost:{settings.ui_port} to explore.")


if __name__ == "__main__":
    main()

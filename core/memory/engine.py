"""Central memory engine."""
from __future__ import annotations
import json, uuid, sys, re
from datetime import datetime, timezone
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent.parent))

from config.settings import settings
from core.memory.models import Memory, MemoryType, WriteRequest, QuarantineEntry, SearchResult
from core.memory.trust import compute_trust, update_agent_prior
from core.privacy.filter import mask
from core.storage import vector_store, graph_store
from core.storage.database import get_conn

_agent_priors: dict[str, float] = {}

# ── WRITE ─────────────────────────────────────────────────────────────
def write_memory(req: WriteRequest) -> dict:
    # Privacy gate: strip PII *before* the content is scored, embedded, or stored.
    # The placeholder map is intentionally not persisted (see core.privacy.filter),
    # so raw emails/phones/keys/names never land in any local store. Masking is
    # idempotent — re-masking already-masked placeholders is a no-op.
    content = mask(req.content).text

    trust, reason = compute_trust(req.agent_id, content, _agent_priors)

    if trust < settings.trust_threshold:
        entry = QuarantineEntry(
            agent_id=req.agent_id, content=content,
            metadata=req.metadata, trust_score=trust, reason=reason,
        )
        _save_quarantine(entry)
        return {"status": "quarantined", "id": entry.id, "trust": trust, "reason": reason}

    similar = vector_store.search(content, n_results=3)
    for hit in similar:
        if hit["distance"] < (1 - settings.dedup_threshold):
            _merge_metadata(hit["id"], req.metadata)
            return {"status": "merged", "id": hit["id"], "trust": trust}

    mem = Memory(
        agent_id=req.agent_id, memory_type=req.memory_type,
        content=content, metadata=req.metadata,
        trust_score=trust, owner=req.owner,
    )
    _save_memory(mem)
    _index_graph(mem)
    return {"status": "stored", "id": mem.id, "trust": trust}

def _save_memory(mem: Memory) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO memories(id,agent_id,memory_type,content,metadata,trust_score,owner,created_at,accessed_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (mem.id, mem.agent_id, mem.memory_type.value, mem.content,
             json.dumps(mem.metadata), mem.trust_score, mem.owner,
             mem.created_at.isoformat(), mem.accessed_at.isoformat()),
        )
    vector_store.upsert(mem.id, mem.content, {
        "memory_type": mem.memory_type.value, "agent_id": mem.agent_id, "owner": mem.owner,
    })

def _save_quarantine(entry: QuarantineEntry) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO quarantine(id,agent_id,content,metadata,trust_score,reason,created_at) VALUES(?,?,?,?,?,?,?)",
            (entry.id, entry.agent_id, entry.content, json.dumps(entry.metadata),
             entry.trust_score, entry.reason, entry.created_at.isoformat()),
        )

def _merge_metadata(memory_id: str, new_meta: dict) -> None:
    with get_conn() as conn:
        row = conn.execute("SELECT metadata FROM memories WHERE id=?", (memory_id,)).fetchone()
        if row:
            existing = json.loads(row["metadata"])
            existing.update(new_meta)
            conn.execute("UPDATE memories SET metadata=? WHERE id=?", (json.dumps(existing), memory_id))

def _index_graph(mem: Memory) -> None:
    similar = vector_store.search(mem.content, n_results=4)
    for hit in similar:
        if hit["id"] != mem.id and hit["distance"] < 0.5:
            w = round(1.0 - hit["distance"], 4)
            graph_store.add_edge(mem.id, hit["id"], "semantic", weight=w)
            graph_store.add_edge(hit["id"], mem.id, "semantic", weight=w)

# ── SEARCH ────────────────────────────────────────────────────────────
def search_memory(query: str, agent_id: str | None = None,
                  memory_type: MemoryType | None = None,
                  owner: str | None = None, n: int = 10) -> list[SearchResult]:
    scores: dict[str, dict] = {}

    # Retrieve a wide candidate pool, fuse signals, THEN cut to n. Using n for the
    # per-signal retrieval starves fusion: a caller asking for n=3 would get only
    # the top-3 bm25 FTS docs, so a real match could miss its FTS/graph boost and
    # sit on its semantic score alone. ponytail: pool = max(n*5, 30) is plenty here.
    cand = max(n * 5, 30)

    where: dict = {}
    if memory_type:
        where["memory_type"] = memory_type.value
    for hit in vector_store.search(query, n_results=cand, where=where or None):
        mid = hit["id"]
        scores.setdefault(mid, {"score": 0.0, "matched_via": []})
        scores[mid]["score"] += (1.0 - hit["distance"])
        scores[mid]["matched_via"].append("semantic")

    for row in _fts_search(query, limit=cand):
        mid = row["id"]
        scores.setdefault(mid, {"score": 0.0, "matched_via": []})
        scores[mid]["score"] += 0.6
        scores[mid]["matched_via"].append("fts5")

    # expand from the current top-scored hits, not dict insertion order
    top_ids = sorted(scores, key=lambda k: scores[k]["score"], reverse=True)[:3]
    for tid in top_ids:
        for edge in graph_store.neighbors(tid, limit=5):
            mid = edge["id"]
            scores.setdefault(mid, {"score": 0.0, "matched_via": []})
            scores[mid]["score"] += edge["weight"] * 0.4
            scores[mid]["matched_via"].append(f"graph:{edge['edge_type']}")

    ranked = sorted(scores, key=lambda k: scores[k]["score"], reverse=True)[:n]
    results = []
    for mid in ranked:
        mem = _load_memory(mid)
        if not mem:
            continue
        if owner and mem.owner not in ("shared", owner):
            continue
        _bump_access(mid)
        results.append(SearchResult(memory=mem, score=round(scores[mid]["score"], 4),
                                    matched_via=scores[mid]["matched_via"]))
    return results

def _fts_search(query: str, limit: int = 10) -> list[dict]:
    # OR the individual terms (each quoted to neutralize FTS5 operators) instead of
    # matching the whole query as one exact phrase — phrase-match killed recall.
    # bm25 `rank` still floats docs matching more/rarer terms to the top.
    terms = [t for t in re.findall(r"\w+", query) if len(t) > 1]
    if not terms:
        return []
    match = " OR ".join(f'"{t}"' for t in terms)
    try:
        with get_conn() as conn:
            rows = conn.execute(
                'SELECT id FROM memories_fts WHERE content MATCH ? ORDER BY rank LIMIT ?',
                (match, limit),
            ).fetchall()
        return [{"id": r["id"]} for r in rows]
    except Exception:
        return []

def _load_memory(memory_id: str) -> Memory | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
    if not row:
        return None
    return Memory(
        id=row["id"], agent_id=row["agent_id"],
        memory_type=MemoryType(row["memory_type"]),
        content=row["content"], metadata=json.loads(row["metadata"]),
        trust_score=row["trust_score"], access_count=row["access_count"],
        decayed_score=row["decayed_score"], owner=row["owner"],
        created_at=datetime.fromisoformat(row["created_at"]),
        accessed_at=datetime.fromisoformat(row["accessed_at"]),
    )

def _bump_access(memory_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE memories SET access_count=access_count+1, accessed_at=? WHERE id=?",
                     (now, memory_id))

# ── LIST / STATS ──────────────────────────────────────────────────────
def list_memories(memory_type: str | None = None, owner: str | None = None,
                  limit: int = 50, offset: int = 0) -> list[dict]:
    q = "SELECT * FROM memories WHERE 1=1"
    params: list = []
    if memory_type:
        q += " AND memory_type=?"; params.append(memory_type)
    if owner:
        q += " AND owner=?"; params.append(owner)
    q += " ORDER BY decayed_score DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]

def list_quarantine(limit: int = 100) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM quarantine WHERE reviewed=0 ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]

def get_stats() -> dict:
    with get_conn() as conn:
        total   = conn.execute("SELECT COUNT(*) as c FROM memories").fetchone()["c"]
        by_type = conn.execute("SELECT memory_type, COUNT(*) as c FROM memories GROUP BY memory_type").fetchall()
        pending = conn.execute("SELECT COUNT(*) as c FROM quarantine WHERE reviewed=0").fetchone()["c"]
        skills  = conn.execute("SELECT COUNT(*) as c FROM skills").fetchone()["c"]
        agents  = conn.execute("SELECT COUNT(DISTINCT agent_id) as c FROM memories").fetchone()["c"]
    return {
        "total": total,
        "by_type": {r["memory_type"]: r["c"] for r in by_type},
        "quarantine_pending": pending,
        "skills": skills,
        "agents": agents,
        "graph_edges": graph_store.link_count(),
        "vector_count": vector_store.count(),
    }

# ── DELETE ────────────────────────────────────────────────────────────
def delete_memory(memory_id: str) -> bool:
    with get_conn() as conn:
        conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    vector_store.delete(memory_id)
    graph_store.remove_node(memory_id)
    return True

# ── MANAGER ───────────────────────────────────────────────────────────
def run_decay() -> int:
    now = datetime.now(timezone.utc)
    hl  = settings.decay_half_life_days
    archived = 0
    with get_conn() as conn:
        rows = conn.execute("SELECT id, trust_score, accessed_at FROM memories").fetchall()
        for row in rows:
            accessed = datetime.fromisoformat(row["accessed_at"])
            if accessed.tzinfo is None:
                accessed = accessed.replace(tzinfo=timezone.utc)
            days = (now - accessed).total_seconds() / 86400
            decayed = row["trust_score"] * (0.5 ** (days / hl))
            conn.execute("UPDATE memories SET decayed_score=? WHERE id=?",
                         (round(decayed, 6), row["id"]))
            if decayed < 0.05:
                conn.execute("DELETE FROM memories WHERE id=?", (row["id"],))
                vector_store.delete(row["id"])
                graph_store.remove_node(row["id"])
                archived += 1
    return archived

def promote_skills() -> int:
    promoted = 0
    with get_conn() as conn:
        rows = conn.execute("SELECT id, success_count, level FROM skills").fetchall()
        for row in rows:
            level, sc = row["level"], row["success_count"]
            new_level = level
            if level == "draft"  and sc >= settings.skill_promotion_threshold:
                new_level = "active"
            elif level == "active" and sc >= settings.skill_promotion_threshold * 3:
                new_level = "crystallized"
            if new_level != level:
                conn.execute("UPDATE skills SET level=?, updated_at=? WHERE id=?",
                             (new_level, datetime.now(timezone.utc).isoformat(), row["id"]))
                promoted += 1
    return promoted

def approve_quarantine(entry_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM quarantine WHERE id=?", (entry_id,)).fetchone()
        if not row:
            return False
        req = WriteRequest(agent_id=row["agent_id"], content=row["content"],
                           metadata=json.loads(row["metadata"]))
        conn.execute("UPDATE quarantine SET reviewed=1 WHERE id=?", (entry_id,))
    write_memory(req)
    _agent_priors.update(update_agent_prior(_agent_priors, row["agent_id"], approved=True))
    return True

def reject_quarantine(entry_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT agent_id FROM quarantine WHERE id=?", (entry_id,)).fetchone()
        if not row:
            return False
        conn.execute("UPDATE quarantine SET reviewed=1 WHERE id=?", (entry_id,))
    _agent_priors.update(update_agent_prior(_agent_priors, row["agent_id"], approved=False))
    return True

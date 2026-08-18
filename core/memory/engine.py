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

def _is_review_flagged(md: dict) -> bool:
    return bool(md.get("semantic_review") or md.get("provenance_stale"))


def list_review_queue(limit: int = 100) -> list[dict]:
    """Memories flagged by a forget — either semantic paraphrase (semantic_review) or an
    id-keyed derivative of erased data (provenance_stale). For the erasure review UI."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, agent_id, content, metadata, created_at FROM memories "
            "WHERE metadata LIKE '%semantic_review%' OR metadata LIKE '%provenance_stale%' "
            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        md = json.loads(r["metadata"]) if r["metadata"] else {}
        if not _is_review_flagged(md):
            continue  # LIKE can false-match content; confirm by parsing
        out.append({"id": r["id"], "agent_id": r["agent_id"], "content": r["content"],
                    "created_at": r["created_at"],
                    "kind": "semantic" if md.get("semantic_review") else "provenance",
                    "sources": md.get("semantic_sources") or md.get("stale_sources") or []})
    return out


def count_quarantine() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM quarantine WHERE reviewed=0").fetchone()[0]


def count_review_queue() -> int:
    # exact (unbounded) count for the sidebar badge — LIKE-prefilter then parse-confirm.
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT metadata FROM memories "
            "WHERE metadata LIKE '%semantic_review%' OR metadata LIKE '%provenance_stale%'").fetchall()
    return sum(1 for r in rows if _is_review_flagged(json.loads(r["metadata"]) if r["metadata"] else {}))

def clear_review_flag(memory_id: str) -> bool:
    """Dismiss a false-positive flag: strip the review markers, keep the memory."""
    with get_conn() as conn:
        row = conn.execute("SELECT metadata FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            return False
        md = json.loads(row["metadata"]) if row["metadata"] else {}
        for k in ("semantic_review", "semantic_sources", "provenance_stale", "stale_sources"):
            md.pop(k, None)
        conn.execute("UPDATE memories SET metadata=? WHERE id=?", (json.dumps(md), memory_id))
    return True

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

# ── DELETE / FORGET ───────────────────────────────────────────────────
def delete_memory(memory_id: str) -> bool:
    with get_conn() as conn:
        conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    vector_store.delete(memory_id)
    graph_store.remove_node(memory_id)
    return True


def verify_forgotten(memory_id: str) -> list[str]:
    """Return the list of stores that STILL contain the id (empty = fully erased).
    Checks every place a memory or its keyed derivatives can live: SQLite row, FTS5
    index, ChromaDB vector, DuckDB graph edges."""
    leaks = []
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM memories WHERE id=?", (memory_id,)).fetchone():
            leaks.append("sqlite")
        if conn.execute("SELECT 1 FROM memories_fts WHERE id=?", (memory_id,)).fetchone():
            leaks.append("fts")
    if vector_store.has(memory_id):
        leaks.append("vector")
    if graph_store.has(memory_id):
        leaks.append("graph")
    return leaks


def _derived_from(source_id: str) -> list[str]:
    """Ids of memories that record `source_id` in metadata.derived_from.
    ponytail: LIKE-prefilter (metadata is small JSON) then confirm by parsing."""
    with get_conn() as conn:
        rows = conn.execute("SELECT id, metadata FROM memories WHERE metadata LIKE ?",
                            (f"%{source_id}%",)).fetchall()
    out = []
    for r in rows:
        try:
            if source_id in (json.loads(r["metadata"]).get("derived_from") or []):
                out.append(r["id"])
        except Exception:
            pass
    return out


def _flag(memory_id: str, source_id: str, marker: str, src_key: str) -> None:
    # mark a memory (in its metadata) as implicated by a forgotten source — flagged for
    # review/re-derivation, not deleted (its own content may still be useful).
    with get_conn() as conn:
        row = conn.execute("SELECT metadata FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            return
        md = json.loads(row["metadata"]) if row["metadata"] else {}
        md[marker] = True
        md[src_key] = sorted(set(md.get(src_key, []) + [source_id]))
        conn.execute("UPDATE memories SET metadata=? WHERE id=?", (json.dumps(md), memory_id))


def _flag_stale(memory_id: str, source_id: str) -> None:
    _flag(memory_id, source_id, "provenance_stale", "stale_sources")


_SEMANTIC_THRESHOLD = 0.80   # cosine ≥ this ⇒ likely restates the forgotten content.
                             # Biased toward recall for GDPR erasure (a missed leak is the failure;
                             # over-flagging is fine — matches are flagged for review, not deleted).
                             # NB: PII masking diverges per-memory (spaCy NER isn't deterministic
                             # across contexts), so masked paraphrases score a few points lower than
                             # raw — a real recall ceiling of tracing over privacy-masked content.


def _semantic_derivatives(content: str, exclude_id: str, limit: int = 20) -> list[str]:
    """Ids of memories whose content is highly similar to `content` — likely paraphrase or
    restate it even without an id-keyed link. The reach GDPR erasure needs beyond exact provenance."""
    try:
        hits = vector_store.search(content, n_results=limit)
    except Exception:
        return []
    return [h["id"] for h in hits
            if h["id"] != exclude_id and (1.0 - h["distance"]) >= _SEMANTIC_THRESHOLD]


def forget_memory(memory_id: str, reason: str = "manual", trace_semantic: bool = True) -> dict:
    """Delete a memory, VERIFY it's gone from every store, retry once on any leak,
    tombstone the outcome, and flag its derivatives:
      - CASCADE (id-keyed): memories whose metadata.derived_from includes it → provenance_stale.
      - SEMANTIC (content): memories that paraphrase/restate it (cosine ≥ threshold) →
        semantic_review, for GDPR-grade erasure that reaches beyond exact provenance.
    Semantic tracing is a targeted-erasure feature (default on); run_decay disables it (bulk
    compaction isn't an erasure request, and a vector search per memory would be costly + noisy)."""
    # read content BEFORE deleting — semantic candidates (which survive the delete) need it
    with get_conn() as conn:
        row = conn.execute("SELECT content FROM memories WHERE id=?", (memory_id,)).fetchone()
    content = row["content"] if row else ""

    delete_memory(memory_id)
    leaks = verify_forgotten(memory_id)
    if leaks:                       # ponytail: one retry covers a transient store hiccup
        delete_memory(memory_id)
        leaks = verify_forgotten(memory_id)

    cascaded = _derived_from(memory_id)
    for d in cascaded:
        _flag_stale(d, memory_id)   # ponytail: one hop — a stale summary isn't erased, so no chain

    semantic = []
    if trace_semantic and content:
        semantic = [s for s in _semantic_derivatives(content, memory_id) if s not in cascaded]
        for s in semantic:
            _flag(s, memory_id, "semantic_review", "semantic_sources")

    verified = not leaks
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO forgotten(id, forgotten_at, reason, verified, leaks, cascaded, semantic) "
            "VALUES(?,?,?,?,?,?,?)",
            (memory_id, now, reason, int(verified), json.dumps(leaks),
             json.dumps(cascaded), json.dumps(semantic)))
    return {"id": memory_id, "verified": verified, "leaks": leaks,
            "cascaded": cascaded, "semantic": semantic}

# ── MANAGER ───────────────────────────────────────────────────────────
_ARCHIVE_AGENT = "decay-archive"
_ARCHIVE_MODEL = "qwen2.5:3b"   # small local model, same as session_hook; zero cloud egress


def _archive_summary(content: str) -> str:
    """Distill a memory into one factual sentence before it's archived, so the gist
    survives the deletion. Truncation fallback keeps it working offline."""
    try:
        import ollama
        r = ollama.chat(model=_ARCHIVE_MODEL,
                        messages=[{"role": "user", "content":
                            "Compress this memory into ONE factual sentence, preserving names, "
                            "numbers, and URLs. Output only the sentence.\n\n" + content[:2000]}],
                        options={"temperature": 0})
        s = (r.get("message", {}).get("content") or "").strip()
        if s:
            return s[:400]
    except Exception:
        pass
    return content[:200]   # lossy but keeps the gist


def run_decay() -> int:
    now = datetime.now(timezone.utc)
    hl  = settings.decay_half_life_days
    to_forget = []
    with get_conn() as conn:
        rows = conn.execute("SELECT id, agent_id, content, trust_score, accessed_at FROM memories").fetchall()
        for row in rows:
            accessed = datetime.fromisoformat(row["accessed_at"])
            if accessed.tzinfo is None:
                accessed = accessed.replace(tzinfo=timezone.utc)
            days = (now - accessed).total_seconds() / 86400
            decayed = row["trust_score"] * (0.5 ** (days / hl))
            conn.execute("UPDATE memories SET decayed_score=? WHERE id=?",
                         (round(decayed, 6), row["id"]))
            if decayed < 0.05:
                to_forget.append((row["id"], row["agent_id"], row["content"]))
    # summarize-then-forget (outside the SELECT loop — these open their own conns)
    archived = 0
    for mid, agent, content in to_forget:
        # forget BEFORE writing the distillation, so cascade doesn't flag the fresh summary.
        # trace_semantic=False: decay is bulk compaction, not an erasure request.
        res = forget_memory(mid, reason="decay", trace_semantic=False)
        archived += 1
        if not res["verified"]:
            print(f"[forget] WARNING: {mid} still in {res['leaks']} after retry", file=sys.stderr)
        # distill non-archive memories into a provenance-linked survivor; don't re-summarize
        # an archive summary (that would loop summaries-of-summaries) — it's terminal.
        if agent != _ARCHIVE_AGENT:
            write_memory(WriteRequest(
                agent_id=_ARCHIVE_AGENT, content=_archive_summary(content),
                memory_type=MemoryType.semantic, metadata={"derived_from": [mid], "archived_from": mid}))
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

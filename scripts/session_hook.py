#!/usr/bin/env python3
"""
Claude Code SessionEnd hook — save the session to Centralaizer.

Reads the hook payload on stdin (has transcript_path), pulls the user's prompts
out of the transcript, and writes them to the hub as one episodic memory so a
later session can recall what you worked on.

Wire in .claude/settings.json:
  "SessionEnd": [ { "hooks": [ { "type": "command",
     "command": "python3 /ABS/scripts/session_hook.py" } ] } ]

Fails open (hub down / bad input -> exit 0, never blocks).
Stores a concise local-LLM summary per session (not the raw transcript) so
memories stay accurate and findable. Falls back to the first prompt verbatim if
the summarizer is unavailable.
"""
import json
import os
import sys
import urllib.request

HUB = "http://localhost:3001"
SUMMARY_MODEL = "qwen2.5:3b"   # local Ollama model; zero cloud egress
SUMMARY_TIMEOUT = 45           # seconds per summarize call; over this → verbatim fallback


# phrases that only appear if the small model parroted the instruction instead
# of summarizing — used to reject echoes and fall back to verbatim.
_ECHO_MARKERS = ("factual summary", "comma-separated keywords", "work session in 1-2",
                 "repeat or mention", "session text")


_BUDGET = 12000   # chars of transcript fed to the summarizer


def _verbatim(items, proj):
    # prefer the user's own lines for the fallback; items may be role-tagged.
    users = [x[6:] for x in items if x.startswith("User: ")] or items
    return (f"[{proj}] " + " ".join(users[:2]))[:600]   # accurate, findable fallback


def _budget(items):
    # keep the head (task setup) AND the tail (decisions/outcomes land at the end),
    # so long sessions don't get summarized from their opening moves alone.
    joined = "\n".join(items)
    if len(joined) <= _BUDGET:
        return joined
    return joined[:3000] + "\n…\n" + joined[-(_BUDGET - 3000):]


def summarize(items, proj):
    """1–2 factual sentences + topic keywords via local Ollama. Verbatim fallback.
    `items` may be plain user prompts or role-tagged ('User:'/'Claude:') lines that
    also carry assistant decisions and findings."""
    try:
        import ollama
        instruction = (
            "You summarize developer work sessions (a transcript of User and Claude turns). "
            "Output ONLY a 1-2 sentence factual summary capturing what was decided, found, or "
            "changed, followed by a line 'Topics:' with up to 6 comma-separated keywords "
            "(names, files, systems, URLs). Do not repeat or mention these instructions. "
            "Use only the transcript.\n\n--- session ---\n" + _budget(items)
        )
        # hard request timeout — a stalled Ollama call must NOT hang the sweep (that
        # death-spiralled the scheduler); fall through to the verbatim fallback instead.
        r = ollama.Client(timeout=SUMMARY_TIMEOUT).chat(
                        model=SUMMARY_MODEL,
                        messages=[{"role": "user", "content": instruction}],
                        options={"temperature": 0})
        s = (r.get("message", {}).get("content") or "").strip()
        low = s.lower()
        if s and not any(m in low for m in _ECHO_MARKERS):
            return f"[{proj}] {s}"[:1400]
    except Exception:
        pass
    return _verbatim(items, proj)


def user_texts(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") != "user":
                continue
            c = o.get("message", {}).get("content")
            if isinstance(c, str):
                t = c
            elif isinstance(c, list):
                t = " ".join(b.get("text", "") for b in c
                              if isinstance(b, dict) and b.get("type") == "text")
            else:
                t = ""
            t = t.strip()
            if not t or t.startswith("Relevant memories from the shared hub"):
                continue
            out.append(t)
    return out


def session_texts(path):
    # interleaved User + Claude prose, so the summary captures decisions/findings —
    # not just the user's intent. Skips tool_use/tool_result noise and recall injection.
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            typ = o.get("type")
            if typ not in ("user", "assistant"):
                continue
            c = o.get("message", {}).get("content")
            if isinstance(c, str):
                t = c
            elif isinstance(c, list):
                t = " ".join(b.get("text", "") for b in c
                              if isinstance(b, dict) and b.get("type") == "text")
            else:
                t = ""
            t = " ".join(t.split()).strip()
            if not t or t.startswith("Relevant memories from the shared hub"):
                continue
            out.append(("User: " if typ == "user" else "Claude: ") + t)
    return out


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    path = payload.get("transcript_path")
    if not path or not os.path.exists(path):
        return 0
    items = session_texts(path)
    if not items:
        return 0
    cwd = os.path.basename(payload.get("cwd", "").rstrip("/")) or "session"
    _post("claude-code", summarize(items, cwd))
    return 0


def _post(agent_id, content, mtype="episodic"):
    body = json.dumps({"agent_id": agent_id, "content": content, "memory_type": mtype}).encode()
    try:
        req = urllib.request.Request(HUB + "/api/memories", data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


# incremental watermarks. ponytail: hard-coded to the default LM_DATA_DIR
# (~/.localmem); session_hook is stdlib-only and doesn't import config.settings.
WATERMARK = os.path.expanduser("~/.localmem/.last_session_sweep")
NATIVE_WATERMARK = os.path.expanduser("~/.localmem/.last_native_sync")
EDITOR_WATERMARK = os.path.expanduser("~/.localmem/.last_editor_sweep")
CURSOR_ROWID = os.path.expanduser("~/.localmem/.last_cursor_rowid")
QUIET_SECONDS = 600   # only import transcripts idle this long — i.e. sessions that have ended
_CURSOR_DB = os.path.expanduser("~/Library/Application Support/Cursor/User/globalStorage/state.vscdb")
_CODE_WS = os.path.expanduser("~/Library/Application Support/Code/User/workspaceStorage")


def _proj(path):
    return os.path.basename(os.path.dirname(path)).lstrip("-").replace("-", "/")


def _import(path):
    items = session_texts(path)
    if not items:
        return False
    return _post("claude-code", summarize(items, _proj(path)))


def _read_watermark(path):
    try:
        return float(open(path).read().strip())
    except Exception:
        return 0.0


def _write_watermark(path, ts):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").write(str(ts))
    except Exception:
        pass


def _backfill():
    # one-time import of ALL existing Claude Code transcripts into the hub
    import glob
    import time
    base = os.path.expanduser("~/.claude/projects")
    n = 0
    for path in glob.glob(os.path.join(base, "*", "*.jsonl")):
        if _import(path):
            n += 1
            print(f"  [{n}] {_proj(path)}")
    _write_watermark(WATERMARK, time.time())   # so the scheduler sweep only picks up sessions after now
    print(f"backfilled {n} session(s) into the hub")


SWEEP_MAX_PER_RUN = 15   # ~14s/summary typical → stays under the scheduler's 300s subprocess timeout
                         # (and per-file watermark means a mid-run kill still keeps progress)


def _sweep():
    # incremental + RESUMABLE: import sessions gone quiet (ended) since the last sweep.
    # Process oldest-first and advance the watermark AFTER EACH file, so a timeout mid-run
    # never loses progress (the old "write watermark only at the end" spiralled: one timeout
    # → retry an ever-growing backlog → permanent timeout). Cap per run so a big backlog
    # drains over several ticks instead of blowing the timeout. Idempotent via hub dedup.
    import glob
    import time
    base = os.path.expanduser("~/.claude/projects")
    last = _read_watermark(WATERMARK)
    cutoff = time.time() - QUIET_SECONDS
    cands = []
    for path in glob.glob(os.path.join(base, "*", "*.jsonl")):
        try:
            m = os.path.getmtime(path)
        except OSError:
            continue
        if last < m <= cutoff:
            cands.append((m, path))
    cands.sort()                                   # oldest first → watermark advances monotonically
    n = 0
    for m, path in cands[:SWEEP_MAX_PER_RUN]:
        if _import(path):
            n += 1
        _write_watermark(WATERMARK, m)             # persist progress per-file — survives a timeout
    if len(cands) <= SWEEP_MAX_PER_RUN:            # fully drained this run
        _write_watermark(WATERMARK, cutoff)
    else:
        print(f"  (sweep: {len(cands) - SWEEP_MAX_PER_RUN} more queued for next tick)")
    print(f"swept {n} newly-ended session(s) into the hub")
    return n


def _sync_native(full=False):
    # import Claude Code's own per-project memory markdown into the hub.
    # Incremental by default (only files changed since the last sync) so it's cheap
    # to run on the scheduler timer; pass full=True to force a complete re-sync.
    import glob
    import re
    import time
    base = os.path.expanduser("~/.claude/projects")
    last = 0.0 if full else _read_watermark(NATIVE_WATERMARK)
    started = time.time()
    n = 0
    for path in glob.glob(os.path.join(base, "*", "memory", "*.md")):
        if os.path.basename(path) == "MEMORY.md":  # the index, not a memory
            continue
        try:
            if os.path.getmtime(path) <= last:
                continue
            text = open(path, encoding="utf-8").read()
        except OSError:
            continue
        body = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.S).strip()  # drop frontmatter
        if not body:
            continue
        proj = os.path.basename(os.path.dirname(os.path.dirname(path))).lstrip("-").replace("-", "/")
        if _post("claude-native", (f"[{proj}] " + body)[:1800], "semantic"):
            n += 1
    _write_watermark(NATIVE_WATERMARK, started)
    print(f"synced {n} native memory file(s) into the hub")
    return n


# ── editor-native AIs (Cursor Composer, VS Code Copilot) ─────────────────────
# PROTOTYPE. Claude Code run inside any editor's terminal is already captured via
# transcripts; this reaches the editors' OWN chat AIs, which store to private,
# undocumented, version-fragile formats. Best-effort: skips cleanly if the shape
# changed. Both readers return {session_id: [role-tagged lines]}.

def _copilot_sessions(since):
    import glob
    out = {}
    for path in glob.glob(os.path.join(_CODE_WS, "*", "chatSessions", "*.json")):
        try:
            if os.path.getmtime(path) <= since:
                continue
            d = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        lines = []
        for req in d.get("requests", []):
            u = (req.get("message") or {}).get("text", "")
            if u.strip():
                lines.append("User: " + " ".join(u.split()))
            resp = req.get("response") or []
            parts = resp if isinstance(resp, list) else [resp]
            a = " ".join(str(p.get("value", "")) if isinstance(p, dict) else str(p) for p in parts)
            if a.strip():
                lines.append("Claude: " + " ".join(a.split()))
        if lines:
            out[d.get("sessionId", os.path.basename(path))[:8]] = lines
    return out


def _cursor_composer_id(key):
    # key = "bubbleId:<composer>:<bubble>"
    parts = key.split(":")
    return parts[1] if len(parts) >= 3 else None


_CURSOR_FIRST_WINDOW = 5000   # first run: only look back this many rows, not all history


def _key_range(prefix):
    # exclusive upper bound for an indexed prefix range scan: bump the last byte.
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _cursor_dirty(since_rowid, cap=15):
    # True incremental: cursorDiskKV rows carry a monotonic rowid, so only composers
    # with a bubble newer than the watermark are re-summarized. Returns
    # (sessions, new_max_rowid, dropped). Read-only (mode=ro) reads latest committed
    # data incl. WAL without locking Cursor's live DB.
    # Perf: the table is ~600k+ bubbles with a BINARY-collation UNIQUE index on key, so
    # a case-insensitive LIKE 'prefix%' can't use it (full scan). We drive off the rowid
    # index for change detection and use `key >= lo AND key < hi` RANGE scans per composer.
    # ponytail: rowid is stable unless Cursor VACUUMs (rare) — then a tick may re-post
    # (dedup merges) or miss one (caught on the next edit). Fine for best-effort capture.
    if not os.path.exists(_CURSOR_DB):
        return {}, since_rowid, 0
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{_CURSOR_DB}?mode=ro", uri=True)
        maxrow = conn.execute("SELECT max(rowid) FROM cursorDiskKV").fetchone()[0] or since_rowid
        since = since_rowid if since_rowid > 0 else max(0, maxrow - _CURSOR_FIRST_WINDOW)
        lo, hi = _key_range("bubbleId:")
        dirty = {}   # composer -> its newest bubble rowid, among bubbles past the watermark
        for rowid, key in conn.execute(
                "SELECT rowid, key FROM cursorDiskKV WHERE rowid > ? AND key >= ? AND key < ?",
                (since, lo, hi)):
            comp = _cursor_composer_id(key)
            if comp and rowid > dirty.get(comp, 0):
                dirty[comp] = rowid
        comps = sorted(dirty, key=dirty.get, reverse=True)[:cap]   # most-recently-active first
        dropped = len(dirty) - len(comps)
        sessions = {}
        for comp in comps:
            clo, chi = _key_range(f"bubbleId:{comp}:")
            lines = []
            for (val,) in conn.execute(
                    "SELECT value FROM cursorDiskKV WHERE key >= ? AND key < ? ORDER BY rowid",
                    (clo, chi)):
                try:
                    b = json.loads(val)
                except Exception:
                    continue
                t = (b.get("text") or "").strip()
                if not t:
                    continue
                lines.append(("User: " if b.get("type") == 1 else "Claude: ") + " ".join(t.split()))
            if lines:
                sessions[comp[:8]] = lines
        conn.close()
        return sessions, maxrow, dropped
    except Exception:
        return {}, since_rowid, 0


def _sweep_editors():
    # summarize new/active editor-native AI chats into the hub. Copilot is gated by
    # file mtime; Cursor is truly incremental by rowid (only changed composers).
    import time
    last = _read_watermark(EDITOR_WATERMARK)
    n = 0
    for sid, lines in _copilot_sessions(last).items():
        if _post("vscode-copilot", summarize(lines, f"copilot/{sid}")):
            n += 1
    since = int(_read_watermark(CURSOR_ROWID))
    sessions, maxrow, dropped = _cursor_dirty(since)
    for sid, lines in sessions.items():
        if _post("cursor", summarize(lines, f"cursor/{sid}")):
            n += 1
    if dropped:
        print(f"  (cursor: {dropped} older changed composers skipped this tick — cap)")
    _write_watermark(CURSOR_ROWID, maxrow)
    _write_watermark(EDITOR_WATERMARK, time.time())
    print(f"swept {n} editor-native AI session(s) into the hub")
    return n


def _selfcheck():
    import tempfile
    lines = [
        {"type": "user", "message": {"role": "user", "content": "add a dark mode toggle"}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "now make it public"}]}},
        {"type": "user", "message": {"role": "user", "content": "Relevant memories from the shared hub: ..."}},
        {"type": "assistant", "message": {"role": "assistant", "content": "done"}},
    ]
    fd, p = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(json.dumps(x) for x in lines))
    got = user_texts(p)
    os.unlink(p)
    assert got == ["add a dark mode toggle", "now make it public"], got

    # echo guard: an instruction-parroting output must be rejected (→ verbatim),
    # a real summary must pass through.
    echo = "Summarize this work session in 1-2 precise, factual sentences. Topics: a, b"
    real = "Added a dark mode toggle and published the repo. Topics: dark mode, release"
    lo = lambda s: any(m in s.lower() for m in _ECHO_MARKERS)
    assert lo(echo) and not lo(real), (lo(echo), lo(real))

    # sweep window: import only files modified after the watermark AND quiet >=QUIET
    # (ended), so active sessions and already-swept ones are skipped.
    last, cutoff = 100.0, 200.0
    inwin = lambda m: last < m <= cutoff
    assert not inwin(150 + QUIET_SECONDS) and inwin(150) and not inwin(50), "sweep window"

    # session_texts: captures User AND Claude prose, drops tool noise + recall injection.
    conv = [
        {"type": "user", "message": {"role": "user", "content": "fix the bug"}},
        {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": "Found it: off-by-one in the loop."}]}},
        {"type": "user", "message": {"role": "user",
            "content": [{"type": "tool_result", "content": "exit 0"}]}},          # tool noise → skip
        {"type": "user", "message": {"role": "user",
            "content": "Relevant memories from the shared hub: ..."}},            # recall → skip
    ]
    fd2, p2 = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd2, "w") as f:
        f.write("\n".join(json.dumps(x) for x in conv))
    st = session_texts(p2)
    os.unlink(p2)
    assert st == ["User: fix the bug", "Claude: Found it: off-by-one in the loop."], st

    # budget keeps the head (task) AND the tail (decision at the end) for long sessions.
    big = _budget(["User: " + "a" * 9000, "Claude: " + "b" * 9000, "Claude: DECISION_AT_END"])
    assert big.startswith("User: a") and "DECISION_AT_END" in big, "budget head+tail"

    # cursor composer-id parse (the incremental grouping key)
    assert _cursor_composer_id("bubbleId:COMP-1:bub-2") == "COMP-1"
    assert _cursor_composer_id("otherKey") is None, "composer id parse"
    # indexed prefix range bound: [lo, hi) must bracket exactly the prefix's keys
    lo, hi = _key_range("bubbleId:")
    assert lo == "bubbleId:" and hi == "bubbleId;", (lo, hi)
    assert lo <= "bubbleId:zzz" < hi and not (lo <= "bubbleIdX" < hi), "range bound"
    print("selfcheck ok:", got, "| echo-guard | sweep-window | assistant-capture | budget | cursor-parse | key-range")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(_selfcheck())
    elif "--backfill" in sys.argv:
        sys.exit(_backfill())
    elif "--sweep" in sys.argv:
        _sweep()
        sys.exit(0)
    elif "--sync-native" in sys.argv:
        _sync_native(full="--full" in sys.argv)
        sys.exit(0)
    elif "--sweep-editors" in sys.argv:
        _sweep_editors()
        sys.exit(0)
    else:
        sys.exit(main())

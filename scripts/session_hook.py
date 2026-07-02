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


# phrases that only appear if the small model parroted the instruction instead
# of summarizing — used to reject echoes and fall back to verbatim.
_ECHO_MARKERS = ("factual summary", "comma-separated keywords", "work session in 1-2",
                 "repeat or mention", "session text")


def _verbatim(prompts, proj):
    return (f"[{proj}] " + " ".join(prompts[:2]))[:600]   # accurate, findable fallback


def summarize(prompts, proj):
    """1–2 factual sentences + topic keywords via local Ollama. Verbatim fallback."""
    joined = "\n".join(prompts)[:8000]
    try:
        import ollama
        instruction = (
            "You summarize developer work sessions. Output ONLY a 1-2 sentence factual "
            "summary followed by a line 'Topics:' with up to 6 comma-separated keywords "
            "(names, files, systems, URLs). Do not repeat or mention these instructions. "
            "Use only the session text.\n\n--- session ---\n" + joined
        )
        r = ollama.chat(model=SUMMARY_MODEL,
                        messages=[{"role": "user", "content": instruction}],
                        options={"temperature": 0})
        s = (r.get("message", {}).get("content") or "").strip()
        low = s.lower()
        if s and not any(m in low for m in _ECHO_MARKERS):
            return f"[{proj}] {s}"[:1400]
    except Exception:
        pass
    return _verbatim(prompts, proj)


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


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    path = payload.get("transcript_path")
    if not path or not os.path.exists(path):
        return 0
    prompts = user_texts(path)
    if not prompts:
        return 0
    cwd = os.path.basename(payload.get("cwd", "").rstrip("/")) or "session"
    _post("claude-code", summarize(prompts, cwd))
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


# incremental-sweep watermark. ponytail: hard-coded to the default LM_DATA_DIR
# (~/.localmem); session_hook is stdlib-only and doesn't import config.settings.
WATERMARK = os.path.expanduser("~/.localmem/.last_session_sweep")
QUIET_SECONDS = 600   # only import transcripts idle this long — i.e. sessions that have ended


def _proj(path):
    return os.path.basename(os.path.dirname(path)).lstrip("-").replace("-", "/")


def _import(path):
    prompts = user_texts(path)
    if not prompts:
        return False
    return _post("claude-code", summarize(prompts, _proj(path)))


def _read_watermark():
    try:
        return float(open(WATERMARK).read().strip())
    except Exception:
        return 0.0


def _write_watermark(ts):
    try:
        os.makedirs(os.path.dirname(WATERMARK), exist_ok=True)
        open(WATERMARK, "w").write(str(ts))
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
    _write_watermark(time.time())   # so the scheduler sweep only picks up sessions after now
    print(f"backfilled {n} session(s) into the hub")


def _sweep():
    # incremental: import sessions that have gone quiet (ended) since the last sweep.
    # Safe to run on a timer — the scheduler calls this, so abruptly-closed sessions
    # (which never fire SessionEnd) still get captured. Idempotent via the hub's dedup.
    import glob
    import time
    base = os.path.expanduser("~/.claude/projects")
    last = _read_watermark()
    cutoff = time.time() - QUIET_SECONDS
    n = 0
    for path in glob.glob(os.path.join(base, "*", "*.jsonl")):
        try:
            m = os.path.getmtime(path)
        except OSError:
            continue
        if last < m <= cutoff and _import(path):
            n += 1
    _write_watermark(cutoff)
    print(f"swept {n} newly-ended session(s) into the hub")
    return n


def _sync_native():
    # import Claude Code's own per-project memory markdown into the hub
    import glob
    import re
    base = os.path.expanduser("~/.claude/projects")
    n = 0
    for path in glob.glob(os.path.join(base, "*", "memory", "*.md")):
        if os.path.basename(path) == "MEMORY.md":  # the index, not a memory
            continue
        try:
            text = open(path, encoding="utf-8").read()
        except Exception:
            continue
        body = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.S).strip()  # drop frontmatter
        if not body:
            continue
        proj = os.path.basename(os.path.dirname(os.path.dirname(path))).lstrip("-").replace("-", "/")
        if _post("claude-native", (f"[{proj}] " + body)[:1800], "semantic"):
            n += 1
    print(f"synced {n} native memory file(s) into the hub")


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
    print("selfcheck ok:", got, "| echo-guard ok | sweep-window ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(_selfcheck())
    elif "--backfill" in sys.argv:
        sys.exit(_backfill())
    elif "--sweep" in sys.argv:
        _sweep()
        sys.exit(0)
    elif "--sync-native" in sys.argv:
        sys.exit(_sync_native())
    else:
        sys.exit(main())

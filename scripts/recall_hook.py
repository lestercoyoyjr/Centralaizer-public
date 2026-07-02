#!/usr/bin/env python3
"""
Claude Code UserPromptSubmit hook — automatic memory recall.

On every prompt, query the Centralaizer hub and inject the most relevant
memories into the model's context, so recall happens automatically instead of
only when the model decides to call memory_search.

Wire it up in settings.json:

    {
      "hooks": {
        "UserPromptSubmit": [
          { "hooks": [ { "type": "command",
              "command": "python3 /ABS/PATH/scripts/recall_hook.py" } ] }
        ]
      }
    }

Design notes:
  - stdlib only (no project deps) and a hard 2s timeout, so it adds minimal
    latency and never depends on the venv or Ollama being importable here.
  - fails OPEN: any error, or the hub being down, prints nothing and exits 0 —
    a recall hook must never block or break a prompt.
  - relevance-gated: only memories scoring >= SCORE_MIN are injected, capped at
    TOP_N, so unrelated prompts add zero noise / tokens.
"""
import json
import sys
import urllib.parse
import urllib.request

HUB = "http://localhost:3001"
SCORE_MIN = 0.7    # fused-score floor; graph-expansion lifts unrelated hits to
                   # ~0.5, while genuine matches sit at 1.0+, so 0.7 separates them
TOP_N = 3
TIMEOUT = 2.0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    prompt = (payload.get("prompt") or "").strip()
    if len(prompt) < 4:
        return 0

    try:
        url = f"{HUB}/api/search?" + urllib.parse.urlencode({"q": prompt, "n": TOP_N})
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            hits = json.load(resp)
    except Exception:
        return 0  # hub down / no network — stay silent, never block

    relevant = [h for h in hits if h.get("score", 0) >= SCORE_MIN][:TOP_N]
    if not relevant:
        return 0

    lines = ["Relevant memories from the shared hub (Centralaizer):"]
    for h in relevant:
        lines.append(f"- [{h['memory_type']}] {h['content']}")
    lines.append("(Use these if helpful; verify before relying on them.)")
    context = "\n".join(lines)

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())

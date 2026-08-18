#!/usr/bin/env bash
# Container entrypoint: wait for the Ollama service, ensure the embedding model
# is present, then launch the hub. Pulling here (not in the image) keeps the
# image small and lets the model live in the ollama volume.
set -e

python - <<'PY'
import os, time, urllib.request, ollama

host = os.environ.get("OLLAMA_HOST", "http://ollama:11434").rstrip("/")
model = os.environ.get("LM_EMBEDDING_MODEL", "nomic-embed-text")

# wait for the ollama service to accept connections
for _ in range(60):
    try:
        urllib.request.urlopen(host + "/api/tags", timeout=2)
        break
    except Exception:
        time.sleep(2)

# pull the embedding model if it isn't already there (idempotent, fast if present)
try:
    have = {m.get("model", "") for m in (ollama.list().get("models", []) or [])}
    if not any(n.startswith(model) for n in have):
        print(f"[entrypoint] pulling embedding model {model} …", flush=True)
        ollama.pull(model)
    else:
        print(f"[entrypoint] embedding model {model} already present", flush=True)
except Exception as e:
    print(f"[entrypoint] WARNING: could not ensure model {model}: {e}", flush=True)
PY

exec python main.py

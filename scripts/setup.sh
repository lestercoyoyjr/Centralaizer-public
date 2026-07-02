#!/usr/bin/env bash
# LocalMem — first-time setup
set -euo pipefail

echo "=== LocalMem setup ==="

# 1. Python check
python3 --version || { echo "Python 3.11+ required"; exit 1; }

# 2. Virtual env
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  echo "Created .venv"
fi
source .venv/bin/activate

# 3. Install package
pip install -e ".[dev]" --quiet

# 4. spaCy model
python -m spacy download en_core_web_sm --quiet || true

# 5. Ollama check
if command -v ollama &>/dev/null; then
  echo "Pulling embedding model (nomic-embed-text)…"
  ollama pull nomic-embed-text
  echo "Pulling reasoning model (qwen3:32b — large download, skip if already present)…"
  ollama pull qwen3:32b || echo "  → Skipped (pull manually: ollama pull qwen3:32b)"
else
  echo "WARNING: Ollama not found. Install from https://ollama.com and run:"
  echo "  ollama pull nomic-embed-text"
  echo "  ollama pull qwen3:32b"
fi

# 6. .env scaffold
if [ ! -f ".env" ]; then
  cat > .env << 'EOF'
# LocalMem environment — edit as needed
LM_DATA_DIR=~/.localmem
LM_MCP_PORT=3000
LM_UI_PORT=3001
LM_REASONING_MODEL=qwen3:32b
LM_EMBEDDING_MODEL=nomic-embed-text
LM_TRUST_THRESHOLD=0.6
LM_DECAY_HALF_LIFE_DAYS=30
EOF
  echo "Created .env (edit to customise)"
fi

echo ""
echo "=== Setup complete ==="
echo "Start with:  source .venv/bin/activate && python main.py"
echo "MCP endpoint: http://localhost:3000/mcp"
echo "Memory UI:    http://localhost:3001"

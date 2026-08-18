# Centralaizer — containerized hub (MCP :3000 + Memory Viewer :3001).
# Ollama runs as a separate service (see docker-compose.yml).
FROM python:3.12-slim

# build-essential: some wheels (hnswlib via chromadb) compile from source on slim.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Runtime deps mirror pyproject.toml [project].dependencies (pyproject's build
# backend is non-standard, so we install the explicit list rather than `pip install .`).
RUN pip install --no-cache-dir \
      "fastmcp>=2.0" "chromadb>=0.5" "ollama>=0.3" "networkx>=3.3" "duckdb>=1.0" \
      "spacy>=3.7" "apscheduler>=3.10" "fastapi>=0.111" "uvicorn[standard]>=0.30" \
      "jinja2>=3.1" "python-multipart>=0.0.9" "pydantic>=2.7" "pydantic-settings>=2.3" \
      "rich>=13.7" "httpx>=0.27" "python-dotenv>=1.0" \
 && python -m spacy download en_core_web_sm

COPY . .

# Storage + binding defaults for the container (compose overrides as needed).
# Binding 0.0.0.0 is REQUIRED so the published ports are reachable from the host.
ENV LM_DATA_DIR=/data \
    LM_DB_PATH=/data/memory.db \
    LM_CHROMA_DIR=/data/chroma \
    LM_GRAPH_PATH=/data/graph.duckdb \
    LM_MCP_HOST=0.0.0.0 \
    LM_UI_HOST=0.0.0.0 \
    OLLAMA_HOST=http://ollama:11434

EXPOSE 3000 3001
ENTRYPOINT ["bash", "docker/entrypoint.sh"]

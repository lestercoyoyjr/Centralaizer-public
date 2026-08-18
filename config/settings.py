"""
Runtime configuration — all values can be overridden via .env or environment variables.
"""
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="LM_", extra="ignore")

    # ── Paths ──────────────────────────────────────────────────────────────
    data_dir: Path = Path.home() / ".localmem"
    db_path: Path = Path.home() / ".localmem" / "memory.db"
    chroma_dir: Path = Path.home() / ".localmem" / "chroma"
    graph_path: Path = Path.home() / ".localmem" / "graph.duckdb"

    # ── MCP server ─────────────────────────────────────────────────────────
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 3000

    # ── Web UI ─────────────────────────────────────────────────────────────
    ui_host: str = "127.0.0.1"
    ui_port: int = 3001

    # ── Ollama ─────────────────────────────────────────────────────────────
    ollama_base_url: str = "http://localhost:11434"
    reasoning_model: str = "qwen3:32b"
    embedding_model: str = "nomic-embed-text"
    embedding_dim: int = 768

    # ── Memory engine ──────────────────────────────────────────────────────
    dedup_threshold: float = 0.92        # cosine similarity above which we merge
    decay_half_life_days: int = 30       # memories halve in score every N days
    trust_threshold: float = 0.6         # writes below this go to quarantine
    skill_promotion_threshold: int = 5   # N successful uses → promote to crystallized

    # ── Privacy ────────────────────────────────────────────────────────────
    spacy_model: str = "en_core_web_sm"
    pii_entity_types: list[str] = ["PERSON", "EMAIL", "ORG", "GPE", "PHONE"]

    # ── Manager schedule ───────────────────────────────────────────────────
    manager_interval_minutes: int = 15


settings = Settings()

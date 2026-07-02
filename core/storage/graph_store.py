"""
Knowledge graph store — DuckDB tables representing four edge types:

  semantic   — similar meaning
  temporal   — happened before / after
  causal     — A caused / enabled B
  entity     — share a named entity (person, project, system)

This implements the MAGMA insight: four orthogonal relational views
let the memory engine retrieve across different query intents.
"""
from __future__ import annotations
import duckdb
from contextlib import contextmanager
from typing import Generator, Literal

from config.settings import settings

EdgeType = Literal["semantic", "temporal", "causal", "entity"]

DDL = """
CREATE TABLE IF NOT EXISTS edges (
    src       VARCHAR NOT NULL,
    dst       VARCHAR NOT NULL,
    edge_type VARCHAR NOT NULL,
    weight    DOUBLE  NOT NULL DEFAULT 1.0,
    label     VARCHAR,
    created_at TIMESTAMP DEFAULT now(),
    PRIMARY KEY (src, dst, edge_type)
);
CREATE INDEX IF NOT EXISTS idx_edges_src  ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst  ON edges(dst);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);
"""


@contextmanager
def _conn() -> Generator[duckdb.DuckDBPyConnection, None, None]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(settings.graph_path))
    con.execute(DDL)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def add_edge(
    src: str,
    dst: str,
    edge_type: EdgeType,
    weight: float = 1.0,
    label: str | None = None,
) -> None:
    with _conn() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO edges(src, dst, edge_type, weight, label)
            VALUES (?, ?, ?, ?, ?)
            """,
            [src, dst, edge_type, weight, label],
        )


def neighbors(
    memory_id: str,
    edge_type: EdgeType | None = None,
    limit: int = 20,
) -> list[dict]:
    with _conn() as con:
        if edge_type:
            rows = con.execute(
                "SELECT dst, edge_type, weight, label FROM edges WHERE src=? AND edge_type=? ORDER BY weight DESC LIMIT ?",
                [memory_id, edge_type, limit],
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT dst, edge_type, weight, label FROM edges WHERE src=? ORDER BY weight DESC LIMIT ?",
                [memory_id, limit],
            ).fetchall()
    return [{"id": r[0], "edge_type": r[1], "weight": r[2], "label": r[3]} for r in rows]


def remove_node(memory_id: str) -> None:
    with _conn() as con:
        con.execute("DELETE FROM edges WHERE src=? OR dst=?", [memory_id, memory_id])

def edge_count() -> int:
    """Raw directed-edge rows (each undirected link is stored twice)."""
    with _conn() as con:
        return con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]


def link_count() -> int:
    """Distinct undirected links — what the user sees in the Graph-edges viewer.
    The engine stores semantic edges in both directions, so this halves the row
    count for symmetric links. Keeps the dashboard stat consistent with the list."""
    with _conn() as con:
        return con.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT DISTINCT least(src, dst) a, greatest(src, dst) b, edge_type FROM edges"
            ")"
        ).fetchone()[0]


def list_edges(limit: int = 200) -> list[dict]:
    """All edges, heaviest first — for the Graph-edges viewer in the UI."""
    with _conn() as con:
        rows = con.execute(
            "SELECT src, dst, edge_type, weight FROM edges ORDER BY weight DESC LIMIT ?",
            [limit],
        ).fetchall()
    return [{"src": r[0], "dst": r[1], "edge_type": r[2], "weight": r[3]} for r in rows]


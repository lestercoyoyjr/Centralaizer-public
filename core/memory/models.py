"""
Core data models for the memory engine.
"""
from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Any
import uuid
from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uid() -> str:
    return str(uuid.uuid4())


class MemoryType(str, Enum):
    semantic   = "semantic"    # facts, knowledge, concepts
    episodic   = "episodic"    # what happened in a session
    procedural = "procedural"  # how to do things (→ skills)
    relational = "relational"  # entity / graph nodes


class MemoryLevel(str, Enum):
    """For procedural memories (skills): promotion ladder."""
    draft        = "draft"
    active       = "active"
    crystallized = "crystallized"


class WriteRequest(BaseModel):
    """What an agent sends when writing a memory."""
    agent_id:    str
    content:     str
    memory_type: MemoryType = MemoryType.semantic
    metadata:    dict[str, Any] = Field(default_factory=dict)
    owner:       str = "shared"  # "shared" or a user ID


class Memory(BaseModel):
    id:            str          = Field(default_factory=_uid)
    agent_id:      str
    memory_type:   MemoryType
    content:       str
    metadata:      dict[str, Any] = Field(default_factory=dict)
    trust_score:   float        = 1.0
    access_count:  int          = 0
    decayed_score: float        = 1.0
    owner:         str          = "shared"
    created_at:    datetime     = Field(default_factory=_now)
    accessed_at:   datetime     = Field(default_factory=_now)


class QuarantineEntry(BaseModel):
    id:          str      = Field(default_factory=_uid)
    agent_id:    str
    content:     str
    metadata:    dict[str, Any] = Field(default_factory=dict)
    trust_score: float
    reason:      str
    created_at:  datetime = Field(default_factory=_now)
    reviewed:    bool     = False


class Skill(BaseModel):
    id:            str  = Field(default_factory=_uid)
    name:          str
    description:   str
    template:      str
    use_count:     int  = 0
    success_count: int  = 0
    level:         MemoryLevel = MemoryLevel.draft
    created_at:    datetime    = Field(default_factory=_now)
    updated_at:    datetime    = Field(default_factory=_now)


class EpisodicSession(BaseModel):
    id:         str      = Field(default_factory=_uid)
    agent_id:   str
    summary:    str
    memory_ids: list[str] = Field(default_factory=list)
    caused_by:  str | None = None
    started_at: datetime   = Field(default_factory=_now)
    ended_at:   datetime | None = None


class SearchResult(BaseModel):
    memory:   Memory
    score:    float           # combined relevance score
    matched_via: list[str]    # e.g. ["semantic", "fts5", "graph:causal"]

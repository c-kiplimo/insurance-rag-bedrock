"""Domain and API models. Pure data: no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, Field


class ChunkType(StrEnum):
    CLAUSE = "clause"
    TABLE = "table"
    LIMITATION = "limitation"  # exclusions, disclaimers, conditions


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_id: str
    doc_id: str
    doc_title: str
    section_id: str
    section_path: str
    ordinal: int
    chunk_type: ChunkType
    text: str
    page_start: int | None = None
    source_url: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    chunk: Chunk
    score: float
    expanded: bool = False  # True if pulled in as structural context, not by search


# ---- API schemas -----------------------------------------------------------------
class QueryRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=2000)
    bypass_cache: bool = False


class Citation(BaseModel):
    source_id: str  # "S1", "S2": the label the LLM cites
    doc_title: str
    section_path: str
    page: int | None = None
    chunk_id: str
    chunk_type: str
    score: float
    excerpt: str


class QueryResponse(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    status: str = "ok"  # ok | no_answer | blocked | quality_failed
    cache_hit: bool = False
    request_id: str = "-"
    trace_id: str | None = None
    timings_ms: dict[str, float] = Field(default_factory=dict)


# ---- internal pipeline structures --------------------------------------------------
@dataclass(slots=True)
class RewriteResult:
    query: str
    hyde_passage: str | None = None


@dataclass(slots=True)
class PipelineRun:
    """A response plus the raw context, used by the evaluation harness."""

    response: QueryResponse
    contexts: list[str] = field(default_factory=list)

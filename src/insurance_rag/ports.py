"""Ports (interfaces) the orchestrator depends on.

The pipeline only knows these Protocols. Bedrock, OpenSearch and Redis are adapters
behind them, which is what makes the pipeline unit-testable without AWS and lets you swap
a component (e.g. Cohere rerank, a different vector store) without touching orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from insurance_rag.models import QueryResponse, RetrievedChunk


@dataclass(slots=True)
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(slots=True)
class GuardrailVerdict:
    blocked: bool = False
    text: str | None = None  # sanitised text (PII masked) or the block message
    reason: str | None = None


@dataclass(slots=True)
class PromptTemplate:
    name: str
    version: str
    system: str
    user: str
    temperature: float = 0.0
    max_tokens: int = 800

    def render(self, **variables: str) -> tuple[str, str]:
        from insurance_rag.prompts import render_template

        return render_template(self.system, variables), render_template(self.user, variables)


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...


class LLM(Protocol):
    def generate(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float,
        model_id: str | None = None,
    ) -> LLMResult: ...


class Reranker(Protocol):
    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        """Return (index into documents, relevance score), best first."""
        ...


class Guardrails(Protocol):
    def check_input(self, text: str) -> GuardrailVerdict: ...
    def check_output(self, answer: str, question: str, sources: list[str]) -> GuardrailVerdict: ...


class Retriever(Protocol):
    def search(self, query_text: str, vector: list[float], k: int) -> list[RetrievedChunk]: ...
    def expand(self, hits: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """Return extra chunks (tables, limitations) belonging to the hits' sections."""
        ...


class SemanticCache(Protocol):
    def get(self, question: str, vector: list[float]) -> QueryResponse | None: ...
    def put(self, question: str, vector: list[float], response: QueryResponse) -> None: ...


class PromptProvider(Protocol):
    def get(self) -> PromptTemplate: ...

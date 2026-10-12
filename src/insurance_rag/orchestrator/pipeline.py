"""The orchestrator: one method per box in the architecture diagram.

    1  redact + embed + cache lookup          (hit => return, skipping everything below)
    2  input guardrails -> rewrite / HyDE
    3  hybrid retrieval (BM25 + k-NN, merged server-side)
    4  rerank, then structural context expansion
    5  prompt assembly -> LLM
    6  output guardrails + quality checks -> cache write -> response

Stages are plain functions over ports, so the whole flow runs in unit tests with fakes.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from insurance_rag.config import Settings
from insurance_rag.logging_setup import request_id_var
from insurance_rag.models import (
    PipelineRun,
    QueryRequest,
    QueryResponse,
    RetrievedChunk,
)
from insurance_rag.observability import (
    current_trace_id,
    record,
    record_generation,
    traced,
)
from insurance_rag.orchestrator.context import NO_ANSWER, build_context
from insurance_rag.orchestrator.quality import check_answer
from insurance_rag.orchestrator.rewrite import QueryRewriter
from insurance_rag.ports import (
    LLM,
    Embedder,
    Guardrails,
    PromptProvider,
    Reranker,
    Retriever,
    SemanticCache,
)
from insurance_rag.redaction import Redactor

log = logging.getLogger(__name__)

BLOCKED_MESSAGE = "This request cannot be processed because it conflicts with usage policy."
QUALITY_MESSAGE = (
    "I could not produce a verified, source-backed answer. Please rephrase or consult the policy."
)


@dataclass(slots=True)
class Dependencies:
    embedder: Embedder
    llm: LLM
    reranker: Reranker
    guardrails: Guardrails
    retriever: Retriever
    cache: SemanticCache
    prompts: PromptProvider
    rewriter: QueryRewriter
    redactor: Redactor


class _Timer:
    def __init__(self) -> None:
        self.ms: dict[str, float] = {}
        self._t0 = time.perf_counter()

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.ms[name] = round((time.perf_counter() - start) * 1000, 1)

    def finish(self) -> dict[str, float]:
        self.ms["total"] = round((time.perf_counter() - self._t0) * 1000, 1)
        return self.ms


class RagPipeline:
    def __init__(self, deps: Dependencies, settings: Settings) -> None:
        self._d = deps
        self._s = settings

    # ------------------------------------------------------------------ public API
    def answer(self, request: QueryRequest) -> QueryResponse:
        return self.run(request).response

    @traced("rag-request")
    def run(self, request: QueryRequest) -> PipelineRun:
        timer = _Timer()
        req_id = request_id_var.get()

        # 1. Redact locally (free, offline) BEFORE anything is embedded, cached or traced.
        question = self._d.redactor.redact(" ".join(request.question.split())).text
        with timer.stage("embed_query"):
            q_vec = self._d.embedder.embed(question)

        if not request.bypass_cache:
            with timer.stage("cache_lookup"):
                cached = self._d.cache.get(question, q_vec)
            if cached is not None:
                log.info("cache hit")
                return self._finish(
                    PipelineRun(cached.model_copy(update={"cache_hit": True})), timer, req_id
                )

        # 2a. Input guardrails (prompt-attack, PII anonymisation, denied topics).
        with timer.stage("input_guardrail"):
            verdict = self._d.guardrails.check_input(question)
        if verdict.blocked:
            return self._finish(
                PipelineRun(QueryResponse(answer=BLOCKED_MESSAGE, status="blocked")), timer, req_id
            )
        question = verdict.text or question

        # 2b. Rewrite / HyDE
        search_text, dense_vec = question, q_vec
        if self._s.enable_rewrite:
            with timer.stage("rewrite"):
                rw = self._d.rewriter.rewrite(question)
            search_text = rw.query
            dense_text = rw.hyde_passage or rw.query
            if dense_text != question:
                with timer.stage("embed_dense"):
                    dense_vec = self._d.embedder.embed(dense_text)
            record(metadata={"rewritten_query": search_text, "hyde_used": bool(rw.hyde_passage)})

        # 3. Hybrid retrieval
        with timer.stage("retrieve"):
            hits = self._d.retriever.search(search_text, dense_vec, self._s.retrieve_k)
        record(metadata={"retrieved": len(hits)})
        if not hits:
            return self._finish(self._no_answer(), timer, req_id)

        # 4. Rerank, then add the tables/limitations that live next to the winners
        with timer.stage("rerank"):
            selected = self._rerank(question, hits)
        if not selected:
            return self._finish(self._no_answer(), timer, req_id)
        if self._s.expand_context:
            with timer.stage("expand_context"):
                selected = selected + self._d.retriever.expand(selected)

        # 5. Assemble the prompt and generate
        context, citations, source_texts = build_context(selected, self._s.max_context_chars)
        prompt = self._d.prompts.get()
        system, user = prompt.render(context=context, question=question)
        with timer.stage("generate"):
            result = self._d.llm.generate(
                system, user, max_tokens=prompt.max_tokens, temperature=prompt.temperature
            )
        record_generation(
            model=self._s.llm_model_id,
            usage_details={"input": result.input_tokens, "output": result.output_tokens},
            metadata={"prompt_name": prompt.name, "prompt_version": prompt.version},
        )

        # 6. Output guardrails + quality checks
        answer = result.text.strip()
        with timer.stage("output_checks"):
            out_verdict = self._d.guardrails.check_output(answer, question, source_texts)
            quality = check_answer(answer, {c.source_id for c in citations})
        if out_verdict.blocked:
            return self._finish(
                PipelineRun(
                    QueryResponse(answer=BLOCKED_MESSAGE, status="blocked"), contexts=source_texts
                ),
                timer,
                req_id,
            )
        answer = out_verdict.text or answer
        if not quality.ok:
            log.warning("quality check failed", extra={"issues": quality.issues})
            return self._finish(
                PipelineRun(
                    QueryResponse(answer=QUALITY_MESSAGE, status="quality_failed"),
                    contexts=source_texts,
                ),
                timer,
                req_id,
            )

        used = [c for c in citations if c.source_id in set(quality.cited)] or citations
        status = "no_answer" if answer == NO_ANSWER else "ok"
        response = QueryResponse(
            answer=answer, citations=[] if status == "no_answer" else used, status=status
        )
        if status == "ok" and not request.bypass_cache:  # bypass = no read AND no write
            self._d.cache.put(question, q_vec, response)
        return self._finish(PipelineRun(response, contexts=source_texts), timer, req_id)

    # ------------------------------------------------------------------ helpers
    @traced("rerank")
    def _rerank(self, question: str, hits: list[RetrievedChunk]) -> list[RetrievedChunk]:
        docs = [f"{h.chunk.section_path}\n{h.chunk.text}" for h in hits]
        ranked = self._d.reranker.rerank(question, docs, self._s.rerank_top_n)
        return [
            RetrievedChunk(chunk=hits[i].chunk, score=score)
            for i, score in ranked
            if score >= self._s.min_relevance
        ]

    @staticmethod
    def _no_answer() -> PipelineRun:
        return PipelineRun(QueryResponse(answer=NO_ANSWER, status="no_answer"))

    @staticmethod
    def _finish(run: PipelineRun, timer: _Timer, req_id: str) -> PipelineRun:
        run.response = run.response.model_copy(
            update={
                "timings_ms": timer.finish(),
                "request_id": req_id,
                "trace_id": current_trace_id(),
            }
        )
        return run

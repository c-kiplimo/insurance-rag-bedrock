from fakes import (
    FakeCache,
    FakeGuardrails,
    FakeLLM,
    FakeReranker,
    FakeRetriever,
    build_pipeline,
    make_chunk,
)
from insurance_rag.models import ChunkType, QueryRequest, RetrievedChunk
from insurance_rag.orchestrator.context import NO_ANSWER
from insurance_rag.orchestrator.pipeline import BLOCKED_MESSAGE, QUALITY_MESSAGE

Q = QueryRequest(question="When is proof of loss due?")


def test_happy_path_returns_cited_answer_and_caches():
    pipeline, parts = build_pipeline()
    resp = pipeline.answer(Q)
    assert resp.status == "ok"
    assert "[S1]" in resp.answer
    assert [c.source_id for c in resp.citations] == ["S1"]
    assert parts["cache"].puts == 1
    assert {"embed_query", "retrieve", "rerank", "generate"} <= set(resp.timings_ms)


def test_cache_hit_skips_every_downstream_stage():
    pipeline, parts = build_pipeline()
    pipeline.answer(Q)  # miss, fills cache
    searched_before = parts["retriever"].searched
    llm_calls_before = len(parts["llm"].calls)

    resp = pipeline.answer(Q)

    assert resp.cache_hit is True
    assert parts["retriever"].searched == searched_before
    assert len(parts["llm"].calls) == llm_calls_before


def test_bypass_cache_neither_reads_nor_writes():
    cache = FakeCache()
    pipeline, _ = build_pipeline(cache=cache)
    pipeline.answer(Q)
    assert cache.puts == 1
    pipeline.answer(QueryRequest(question=Q.question, bypass_cache=True))
    assert cache.puts == 1


def test_no_hits_returns_no_answer_without_calling_llm_to_answer():
    pipeline, parts = build_pipeline(retriever=FakeRetriever([]))
    resp = pipeline.answer(Q)
    assert resp.status == "no_answer" and resp.answer == NO_ANSWER
    assert "answer" not in parts["llm"].calls
    assert parts["cache"].puts == 0


def test_low_relevance_chunks_are_dropped():
    pipeline, parts = build_pipeline(reranker=FakeReranker(score=0.01))
    resp = pipeline.answer(Q)
    assert resp.status == "no_answer"
    assert "answer" not in parts["llm"].calls


def test_input_guardrail_block_stops_pipeline():
    pipeline, parts = build_pipeline(guardrails=FakeGuardrails(block_input=True))
    resp = pipeline.answer(Q)
    assert resp.status == "blocked" and resp.answer == BLOCKED_MESSAGE
    assert parts["retriever"].searched == 0


def test_output_guardrail_block_is_not_cached():
    pipeline, parts = build_pipeline(guardrails=FakeGuardrails(block_output=True))
    resp = pipeline.answer(Q)
    assert resp.status == "blocked"
    assert parts["cache"].puts == 0


def test_answer_without_citation_fails_quality_and_is_not_cached():
    pipeline, parts = build_pipeline(llm=FakeLLM("It is due in 60 days."))
    resp = pipeline.answer(Q)
    assert resp.status == "quality_failed" and resp.answer == QUALITY_MESSAGE
    assert parts["cache"].puts == 0


def test_fabricated_citation_fails_quality():
    pipeline, _ = build_pipeline(llm=FakeLLM("Due in 60 days [S9]."))
    assert pipeline.answer(Q).status == "quality_failed"


def test_model_declining_is_reported_as_no_answer_with_no_citations():
    pipeline, parts = build_pipeline(llm=FakeLLM(NO_ANSWER))
    resp = pipeline.answer(Q)
    assert resp.status == "no_answer" and resp.citations == []
    assert parts["cache"].puts == 0


def test_expanded_limitation_chunks_reach_the_prompt_and_citations():
    hit = RetrievedChunk(make_chunk(1, "Proof of loss must be filed within 60 days."), 1.0)
    limitation = RetrievedChunk(
        make_chunk(
            2, "We do not extend this deadline unless we agree in writing.", ChunkType.LIMITATION
        ),
        0.0,
        expanded=True,
    )
    llm = FakeLLM("Due in 60 days [S1], extendable only in writing [S2].")
    pipeline, _ = build_pipeline(llm=llm, retriever=FakeRetriever([hit], extra=[limitation]))
    resp = pipeline.answer(Q)
    assert resp.status == "ok"
    assert {c.source_id for c in resp.citations} == {"S1", "S2"}
    assert any(c.chunk_type == "limitation" for c in resp.citations)


def test_pii_in_question_is_redacted_before_embedding_and_cache_key():
    cache = FakeCache()
    pipeline, _ = build_pipeline(cache=cache)
    pipeline.answer(QueryRequest(question="Claim for SSN 900-12-3456, when is proof of loss due?"))
    assert all("900-12-3456" not in key for key in cache.store)
    assert any("[SSN]" in key for key in cache.store)

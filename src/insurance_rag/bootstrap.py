"""Composition root: the only place that wires concrete adapters to the pipeline."""

from __future__ import annotations

from dataclasses import dataclass

from opensearchpy import OpenSearch

from insurance_rag.adapters.bedrock import (
    BedrockEmbedder,
    BedrockGuardrails,
    BedrockLLM,
    BedrockReranker,
    NoopGuardrails,
)
from insurance_rag.adapters.opensearch_store import OpenSearchRetriever, build_client
from insurance_rag.adapters.redis_cache import NullCache, RedisSemanticCache
from insurance_rag.config import Settings
from insurance_rag.orchestrator.pipeline import Dependencies, RagPipeline
from insurance_rag.orchestrator.rewrite import QueryRewriter
from insurance_rag.ports import SemanticCache
from insurance_rag.prompts import build_prompt_provider
from insurance_rag.redaction import RegexRedactor


@dataclass(slots=True)
class App:
    pipeline: RagPipeline
    retriever: OpenSearchRetriever
    cache: SemanticCache
    os_client: OpenSearch


def build_app(settings: Settings) -> App:
    os_client = build_client(settings)
    retriever = OpenSearchRetriever(os_client, settings)
    llm = BedrockLLM(settings)
    cache: SemanticCache = (
        NullCache() if settings.cache_mode == "off" else RedisSemanticCache(settings)
    )
    guardrails = BedrockGuardrails(settings) if settings.guardrail_id else NoopGuardrails()
    deps = Dependencies(
        embedder=BedrockEmbedder(settings),
        llm=llm,
        reranker=BedrockReranker(settings),
        guardrails=guardrails,
        retriever=retriever,
        cache=cache,
        prompts=build_prompt_provider(settings),
        rewriter=QueryRewriter(llm, settings.rewrite_model_id),
        redactor=RegexRedactor(),
    )
    return App(RagPipeline(deps, settings), retriever, cache, os_client)

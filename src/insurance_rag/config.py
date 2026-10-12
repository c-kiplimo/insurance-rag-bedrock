"""Typed, validated runtime configuration.

Every setting can be overridden with an ``RAG_``-prefixed environment variable
(e.g. ``RAG_OPENSEARCH_ENDPOINT``). Secrets never have defaults and are never logged.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="RAG_", extra="ignore")

    # --- general -----------------------------------------------------------------
    env: Literal["local", "staging", "prod"] = "local"
    log_level: str = "INFO"
    aws_region: str = "us-east-1"

    # --- Bedrock models ----------------------------------------------------------
    embed_model_id: str = "amazon.titan-embed-text-v2:0"
    embed_dimensions: int = Field(1024, description="Titan v2 supports 256, 512 or 1024")
    llm_model_id: str = "us.amazon.nova-lite-v1:0"
    rewrite_model_id: str | None = Field(None, description="Defaults to llm_model_id")
    llm_max_tokens: int = 800
    llm_temperature: float = 0.0
    # Bedrock rerank models are only offered in some regions: verify in the console.
    rerank_region: str = "us-west-2"
    rerank_model_id: str = "amazon.rerank-v1:0"
    bedrock_timeout_seconds: int = 60

    # --- Guardrails --------------------------------------------------------------
    guardrail_id: str | None = None
    guardrail_version: str = "DRAFT"
    guardrail_grounding: bool = Field(
        False, description="Send answer+sources to the contextual-grounding check"
    )

    # --- Prompt management -------------------------------------------------------
    # If prompt_id is unset the prompt YAML shipped in the image is used.
    prompt_id: str | None = None
    prompt_version: str | None = None
    prompt_refresh_seconds: int = 300

    # --- OpenSearch --------------------------------------------------------------
    opensearch_endpoint: str = "http://localhost:9200"
    opensearch_auth: Literal["none", "basic", "sigv4"] = "none"
    opensearch_service: Literal["es", "aoss"] = "es"
    opensearch_user: str | None = None
    opensearch_password: SecretStr | None = None
    opensearch_index: str = "policy-chunks"
    opensearch_pipeline: str = "hybrid-norm-pipeline"
    keyword_weight: float = 0.3
    vector_weight: float = 0.7

    # --- Retrieval behaviour -----------------------------------------------------
    retrieve_k: int = 30
    rerank_top_n: int = 6
    min_relevance: float = Field(0.05, description="Drop reranked chunks scoring below this")
    expand_context: bool = True
    max_expanded_per_section: int = 2
    max_context_chars: int = 14000
    enable_rewrite: bool = True

    # --- Semantic cache ----------------------------------------------------------
    cache_mode: Literal["semantic", "exact", "off"] = "semantic"
    redis_url: str = "redis://localhost:6379"
    cache_ttl_seconds: int = 86400
    cache_similarity: float = Field(0.95, ge=0.5, le=1.0)
    corpus_version: str = Field("v1", description="Bump after re-ingesting to invalidate cache")

    # --- Observability -----------------------------------------------------------
    langfuse_enabled: bool = False  # Langfuse reads LANGFUSE_* env vars itself

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if abs(self.keyword_weight + self.vector_weight - 1.0) > 1e-6:
            raise ValueError("keyword_weight + vector_weight must equal 1.0")
        if self.opensearch_auth == "basic" and not (
            self.opensearch_user and self.opensearch_password
        ):
            raise ValueError("basic auth needs opensearch_user and opensearch_password")
        if self.rerank_top_n > self.retrieve_k:
            raise ValueError("rerank_top_n cannot exceed retrieve_k")
        return self

    @property
    def rerank_model_arn(self) -> str:
        return f"arn:aws:bedrock:{self.rerank_region}::foundation-model/{self.rerank_model_id}"

    @property
    def cache_namespace(self) -> str:
        """Entries are only shared between requests that would produce the same answer."""
        raw = "|".join(
            [
                self.corpus_version,
                self.prompt_id or "local",
                self.prompt_version or "local",
                self.llm_model_id,
                self.opensearch_index,
            ]
        )
        return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:16]


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""Semantic cache on Redis/Valkey.

Two tiers:
1. exact: key = hash of the normalised (PII-redacted) question: O(1), always available.
2. semantic: KNN over question embeddings using the search module (Redis Stack, or ElastiCache
   for Valkey with vector search enabled). A hit requires cosine similarity >= threshold.

Safety rules:
* Fail open: any cache error is a miss, never a request failure.
* Keys are namespaced by corpus/prompt/model version, so changing any of them invalidates.
* Only successful grounded answers are written (the pipeline enforces this).
* The raw question is never stored: only its hash, its embedding and the answer.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

import numpy as np
import redis
from redis.commands.search.field import TagField, VectorField
from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query

from insurance_rag.config import Settings
from insurance_rag.models import QueryResponse

log = logging.getLogger(__name__)

_INDEX = "rag:cache:idx"
_PREFIX = "rag:cache:"


def normalise(question: str) -> str:
    return re.sub(r"\s+", " ", question.strip().lower())


class RedisSemanticCache:
    def __init__(self, settings: Settings) -> None:
        self._r: Any = redis.Redis.from_url(
            settings.redis_url, socket_timeout=0.25, socket_connect_timeout=0.5
        )
        self._ns = settings.cache_namespace
        self._ttl = settings.cache_ttl_seconds
        self._dim = settings.embed_dimensions
        self._max_dist = 1.0 - settings.cache_similarity
        self._semantic = settings.cache_mode == "semantic"
        self._index_ready = False

    # -- setup -----------------------------------------------------------------------
    def ping(self) -> bool:
        try:
            return bool(self._r.ping())
        except redis.RedisError:
            return False

    def _ensure_index(self) -> None:
        if self._index_ready:
            return
        ft = self._r.ft(_INDEX)
        try:
            ft.info()
        except redis.ResponseError:
            ft.create_index(
                [
                    TagField("ns"),
                    VectorField(
                        "embedding",
                        "HNSW",
                        {"TYPE": "FLOAT32", "DIM": self._dim, "DISTANCE_METRIC": "COSINE"},
                    ),
                ],
                definition=IndexDefinition(  # type: ignore[no-untyped-call]
                    prefix=[_PREFIX], index_type=IndexType.HASH
                ),
            )
        self._index_ready = True

    def _key(self, question: str) -> str:
        digest = hashlib.sha256(normalise(question).encode()).hexdigest()
        return f"{_PREFIX}{self._ns}:{digest}"

    # -- port ------------------------------------------------------------------------
    def get(self, question: str, vector: list[float]) -> QueryResponse | None:
        try:
            raw = self._r.hget(self._key(question), "payload")
            if raw is None and self._semantic:
                raw = self._knn(vector)
            if raw is None:
                return None
            return QueryResponse.model_validate_json(raw)
        except Exception:
            log.warning("cache get failed; treating as miss", exc_info=True)
            return None

    def _knn(self, vector: list[float]) -> bytes | None:
        self._ensure_index()
        q = (
            Query(f"(@ns:{{{self._ns}}})=>[KNN 1 @embedding $vec AS dist]")
            .sort_by("dist")
            .return_fields("dist", "payload")
            .dialect(2)
        )
        res = self._r.ft(_INDEX).search(q, query_params={"vec": _pack(vector)})
        if not res.docs:
            return None
        doc = res.docs[0]
        if float(doc.dist) > self._max_dist:
            return None
        payload: bytes = doc.payload.encode() if isinstance(doc.payload, str) else doc.payload
        return payload

    def put(self, question: str, vector: list[float], response: QueryResponse) -> None:
        try:
            payload = response.model_copy(
                update={"cache_hit": False, "timings_ms": {}, "request_id": "-", "trace_id": None}
            ).model_dump_json()
            mapping: dict[str, Any] = {"ns": self._ns, "payload": payload}
            if self._semantic:
                mapping["embedding"] = _pack(vector)
            key = self._key(question)
            pipe = self._r.pipeline()
            pipe.hset(key, mapping=mapping)
            pipe.expire(key, self._ttl)
            pipe.execute()
        except Exception:
            log.warning("cache put failed", exc_info=True)


def _pack(vector: list[float]) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


class NullCache:
    def get(self, question: str, vector: list[float]) -> QueryResponse | None:
        return None

    def put(self, question: str, vector: list[float], response: QueryResponse) -> None:
        return None

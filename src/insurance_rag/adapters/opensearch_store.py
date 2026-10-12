"""OpenSearch: one index serving dense (k-NN) and sparse (BM25) retrieval.

Hybrid queries run both sub-queries and a *search pipeline* normalises (min-max) and combines
their scores with configurable weights, so scores from the two scales are comparable.
Requires OpenSearch >= 2.10 (managed domain or self-managed). Search pipelines are not
available on OpenSearch Serverless: use a managed domain for this design.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import boto3
from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

from insurance_rag.config import Settings
from insurance_rag.models import Chunk, ChunkType, RetrievedChunk

log = logging.getLogger(__name__)

_SOURCE_EXCLUDES = ["embedding"]
_STRUCTURAL_TYPES = [ChunkType.TABLE.value, ChunkType.LIMITATION.value]


def build_client(settings: Settings) -> OpenSearch:
    url = urlparse(settings.opensearch_endpoint)
    kwargs: dict[str, Any] = {
        "hosts": [settings.opensearch_endpoint],
        "use_ssl": url.scheme == "https",
        "verify_certs": url.scheme == "https",
        "connection_class": RequestsHttpConnection,
        "timeout": 30,
        "max_retries": 3,
        "retry_on_timeout": True,
        "pool_maxsize": 20,
    }
    if settings.opensearch_auth == "sigv4":
        creds = boto3.Session().get_credentials()
        kwargs["http_auth"] = AWSV4SignerAuth(
            creds, settings.aws_region, settings.opensearch_service
        )
    elif settings.opensearch_auth == "basic":
        assert settings.opensearch_password is not None
        kwargs["http_auth"] = (
            settings.opensearch_user,
            settings.opensearch_password.get_secret_value(),
        )
    return OpenSearch(**kwargs)


def pipeline_body(keyword_weight: float, vector_weight: float) -> dict[str, Any]:
    # weights follow the order of the sub-queries in the hybrid query: [BM25, k-NN]
    return {
        "description": "min-max normalise BM25 and k-NN scores, then weighted mean",
        "phase_results_processors": [
            {
                "normalization-processor": {
                    "normalization": {"technique": "min_max"},
                    "combination": {
                        "technique": "arithmetic_mean",
                        "parameters": {"weights": [keyword_weight, vector_weight]},
                    },
                }
            }
        ],
    }


def index_body(settings: Settings) -> dict[str, Any]:
    return {
        "settings": {
            "index": {
                "knn": True,
                "number_of_shards": 1,
                "number_of_replicas": 0 if settings.env == "local" else 1,
                "search.default_pipeline": settings.opensearch_pipeline,
            }
        },
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "chunk_id": {"type": "keyword"},
                "doc_id": {"type": "keyword"},
                "doc_title": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
                "section_id": {"type": "keyword"},
                "section_path": {"type": "text", "analyzer": "english"},
                "ordinal": {"type": "integer"},
                "chunk_type": {"type": "keyword"},
                "text": {"type": "text", "analyzer": "english"},
                "page_start": {"type": "integer"},
                "source_url": {"type": "keyword", "index": False},
                "corpus_version": {"type": "keyword"},
                "ingested_at": {"type": "date"},
                "embedding": {
                    "type": "knn_vector",
                    "dimension": settings.embed_dimensions,
                    "method": {
                        "name": "hnsw",
                        "engine": "lucene",
                        "space_type": "cosinesimil",
                        "parameters": {"ef_construction": 128, "m": 16},
                    },
                },
            },
        },
    }


def ensure_index(client: OpenSearch, settings: Settings, *, recreate: bool = False) -> None:
    client.transport.perform_request(
        "PUT",
        f"/_search/pipeline/{settings.opensearch_pipeline}",
        body=pipeline_body(settings.keyword_weight, settings.vector_weight),
    )
    exists = client.indices.exists(index=settings.opensearch_index)
    if exists and recreate:
        client.indices.delete(index=settings.opensearch_index)
        exists = False
    if not exists:
        client.indices.create(index=settings.opensearch_index, body=index_body(settings))
        log.info("created index", extra={"index": settings.opensearch_index})


def _to_chunk(source: dict[str, Any]) -> Chunk:
    return Chunk(
        chunk_id=source["chunk_id"],
        doc_id=source["doc_id"],
        doc_title=source["doc_title"],
        section_id=source["section_id"],
        section_path=source["section_path"],
        ordinal=int(source["ordinal"]),
        chunk_type=ChunkType(source["chunk_type"]),
        text=source["text"],
        page_start=source.get("page_start"),
        source_url=source.get("source_url"),
    )


class OpenSearchRetriever:
    def __init__(self, client: OpenSearch, settings: Settings) -> None:
        self._client = client
        self._index = settings.opensearch_index
        self._max_per_section = settings.max_expanded_per_section

    def ping(self) -> bool:
        return bool(self._client.ping())

    def search(self, query_text: str, vector: list[float], k: int) -> list[RetrievedChunk]:
        body = {
            "size": k,
            "_source": {"excludes": _SOURCE_EXCLUDES},
            "query": {
                "hybrid": {
                    "queries": [
                        {
                            "multi_match": {
                                "query": query_text,
                                "fields": ["text", "section_path^1.5", "doc_title"],
                            }
                        },
                        {"knn": {"embedding": {"vector": vector, "k": k}}},
                    ]
                }
            },
        }
        resp = self._client.search(index=self._index, body=body)
        return [
            RetrievedChunk(chunk=_to_chunk(h["_source"]), score=float(h["_score"]))
            for h in resp["hits"]["hits"]
        ]

    def expand(self, hits: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """Pull in the tables and limitation clauses that sit in the same sections as the hits.

        Policies routinely put the exclusion or the limit table a paragraph away from the
        clause that matches the query. Retrieving only "similar text" would drop it.
        """
        have = {h.chunk.chunk_id for h in hits}
        section_ids = sorted({h.chunk.section_id for h in hits})
        if not section_ids:
            return []
        body = {
            "size": len(section_ids) * self._max_per_section * 2,
            "_source": {"excludes": _SOURCE_EXCLUDES},
            "query": {
                "bool": {
                    "filter": [
                        {"terms": {"section_id": section_ids}},
                        {"terms": {"chunk_type": _STRUCTURAL_TYPES}},
                    ]
                }
            },
            "sort": [{"section_id": "asc"}, {"ordinal": "asc"}],
        }
        resp = self._client.search(
            index=self._index, body=body, params={"search_pipeline": "_none"}
        )
        per_section: dict[str, int] = {}
        extra: list[RetrievedChunk] = []
        for h in resp["hits"]["hits"]:
            chunk = _to_chunk(h["_source"])
            if chunk.chunk_id in have:
                continue
            if per_section.get(chunk.section_id, 0) >= self._max_per_section:
                continue
            per_section[chunk.section_id] = per_section.get(chunk.section_id, 0) + 1
            extra.append(RetrievedChunk(chunk=chunk, score=0.0, expanded=True))
        return extra

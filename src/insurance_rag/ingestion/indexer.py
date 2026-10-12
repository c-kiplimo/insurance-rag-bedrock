"""Embed chunks with Bedrock and bulk-index them into OpenSearch.

Concurrency is bounded (default 4 workers) to stay under Bedrock embedding quotas; throttling
is absorbed by the botocore adaptive retry mode configured on the client.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from opensearchpy import OpenSearch, helpers

from insurance_rag.config import Settings
from insurance_rag.ingestion.chunk import chunk_document, embedding_text
from insurance_rag.ingestion.parse import discover, parse_file
from insurance_rag.models import Chunk
from insurance_rag.ports import Embedder
from insurance_rag.redaction import Redactor

log = logging.getLogger(__name__)


@dataclass(slots=True)
class IngestReport:
    documents: int = 0
    chunks: int = 0
    by_type: Counter[str] = field(default_factory=Counter)
    redactions: Counter[str] = field(default_factory=Counter)
    failed_documents: list[str] = field(default_factory=list)
    indexing_errors: int = 0
    corpus_version: str = ""
    finished_at: str = ""


def _actions(
    chunks: list[Chunk], vectors: list[list[float]], index: str, corpus_version: str
) -> Iterator[dict[str, object]]:
    now = datetime.now(UTC).isoformat()
    for c, vec in zip(chunks, vectors, strict=True):
        doc = {
            "chunk_id": c.chunk_id,
            "doc_id": c.doc_id,
            "doc_title": c.doc_title,
            "section_id": c.section_id,
            "section_path": c.section_path,
            "ordinal": c.ordinal,
            "chunk_type": c.chunk_type.value,
            "text": c.text,
            "page_start": c.page_start,
            "source_url": c.source_url,
            "corpus_version": corpus_version,
            "ingested_at": now,
            "embedding": vec,
        }
        # _id = chunk_id makes re-ingestion an idempotent upsert
        yield {"_op_type": "index", "_index": index, "_id": c.chunk_id, "_source": doc}


def ingest_directory(
    raw_dir: Path,
    *,
    client: OpenSearch,
    embedder: Embedder,
    redactor: Redactor,
    settings: Settings,
    workers: int = 4,
) -> IngestReport:
    report = IngestReport(corpus_version=settings.corpus_version)
    files, manifest = discover(raw_dir)
    if not files:
        raise FileNotFoundError(f"no .pdf/.md/.txt files in {raw_dir}")

    for path in files:
        try:
            doc = parse_file(path, manifest)
            # Redact BEFORE chunking/embedding so identifiers never reach the model or the index.
            redacted_pages = []
            for page_no, text in doc.pages:
                result = redactor.redact(text)
                report.redactions.update(result.counts)
                redacted_pages.append((page_no, result.text))
            doc.pages = redacted_pages

            chunks = chunk_document(doc)
            texts = [embedding_text(c) for c in chunks]
            with ThreadPoolExecutor(max_workers=workers) as pool:
                vectors = list(pool.map(embedder.embed, texts))

            ok, errors = helpers.bulk(
                client,
                _actions(chunks, vectors, settings.opensearch_index, settings.corpus_version),
                raise_on_error=False,
                chunk_size=100,
            )
            report.documents += 1
            report.chunks += int(ok)
            report.indexing_errors += len(errors) if isinstance(errors, list) else int(errors)
            report.by_type.update(c.chunk_type.value for c in chunks)
            log.info("ingested", extra={"doc": doc.doc_id, "chunks": len(chunks)})
        except Exception:
            log.exception("failed to ingest document", extra={"doc": path.name})
            report.failed_documents.append(path.name)

    client.indices.refresh(index=settings.opensearch_index)
    report.finished_at = datetime.now(UTC).isoformat()
    (raw_dir / "ingest_report.json").write_text(json.dumps(asdict(report), indent=2, default=dict))
    return report

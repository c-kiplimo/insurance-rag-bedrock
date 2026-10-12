"""Command line: ``python -m insurance_rag <command>``.

init-index        create the search pipeline + index (use --recreate to rebuild)
download          fetch the free public documents listed in ingestion/sources.yaml
generate-claims   write synthetic claim notes (fake PHI) to exercise redaction
ingest            parse, redact, chunk, embed and index everything in data/raw
ask "question"    run the full pipeline once and print the answer with citations
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from insurance_rag.config import get_settings
from insurance_rag.logging_setup import configure_logging

log = logging.getLogger("insurance_rag.cli")


def _cmd_init_index(args: argparse.Namespace) -> int:
    from insurance_rag.adapters.opensearch_store import build_client, ensure_index

    settings = get_settings()
    ensure_index(build_client(settings), settings, recreate=args.recreate)
    print(f"index '{settings.opensearch_index}' ready")
    return 0


def _cmd_download(args: argparse.Namespace) -> int:
    from insurance_rag.ingestion.download import download_all

    manifest = download_all(Path(args.dest), force=args.force)
    print(f"{len(manifest)} documents in manifest -> {args.dest}/manifest.json")
    return 0


def _cmd_generate(args: argparse.Namespace) -> int:
    from insurance_rag.ingestion.synthetic import generate

    paths = generate(Path(args.dest), count=args.count)
    print(f"wrote {len(paths)} synthetic claim files to {args.dest}")
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    from insurance_rag.adapters.bedrock import BedrockEmbedder
    from insurance_rag.adapters.opensearch_store import build_client
    from insurance_rag.ingestion.indexer import ingest_directory
    from insurance_rag.redaction import build_redactor

    settings = get_settings()
    report = ingest_directory(
        Path(args.path),
        client=build_client(settings),
        embedder=BedrockEmbedder(settings),
        redactor=build_redactor(args.redactor, settings.aws_region),
        settings=settings,
        workers=args.workers,
    )
    print(
        json.dumps(
            {
                "documents": report.documents,
                "chunks": report.chunks,
                "by_type": dict(report.by_type),
                "redactions": dict(report.redactions),
                "failed_documents": report.failed_documents,
                "indexing_errors": report.indexing_errors,
            },
            indent=2,
        )
    )
    return 1 if report.failed_documents or report.indexing_errors else 0


def _cmd_ask(args: argparse.Namespace) -> int:
    from insurance_rag.bootstrap import build_app
    from insurance_rag.models import QueryRequest

    app = build_app(get_settings())
    resp = app.pipeline.answer(QueryRequest(question=args.question, bypass_cache=args.no_cache))
    print(f"\n{resp.answer}\n")
    for c in resp.citations:
        page = f" p.{c.page}" if c.page else ""
        print(f"  [{c.source_id}] {c.doc_title} | {c.section_path}{page} ({c.chunk_type})")
    print(f"\nstatus={resp.status} cache_hit={resp.cache_hit} timings_ms={resp.timings_ms}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="insurance_rag", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init-index")
    p.add_argument("--recreate", action="store_true", help="DELETE and rebuild the index")
    p.set_defaults(fn=_cmd_init_index)

    p = sub.add_parser("download")
    p.add_argument("--dest", default="data/raw")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=_cmd_download)

    p = sub.add_parser("generate-claims")
    p.add_argument("--dest", default="data/raw")
    p.add_argument("--count", type=int, default=25)
    p.set_defaults(fn=_cmd_generate)

    p = sub.add_parser("ingest")
    p.add_argument("--path", default="data/raw")
    p.add_argument("--redactor", default="regex", help="comma list: regex,comprehend,medical")
    p.add_argument("--workers", type=int, default=4)
    p.set_defaults(fn=_cmd_ingest)

    p = sub.add_parser("ask")
    p.add_argument("question")
    p.add_argument("--no-cache", action="store_true")
    p.set_defaults(fn=_cmd_ask)

    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())

"""Parse source files into page-tagged Markdown.

PDFs go through pymupdf4llm, which keeps headings and renders tables as Markdown tables
(far better for policy schedules than flat text extraction). ``.md``/``.txt`` pass through.
Scanned PDFs need OCR (Amazon Textract): see docs/DATA.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ParsedDocument:
    doc_id: str
    title: str
    source_url: str | None
    pages: list[tuple[int, str]]  # (1-based page number, markdown)


def _manifest(raw_dir: Path) -> dict[str, dict[str, str]]:
    path = raw_dir / "manifest.json"
    return json.loads(path.read_text()) if path.exists() else {}


def parse_file(path: Path, manifest: dict[str, dict[str, str]]) -> ParsedDocument:
    doc_id = path.stem
    meta = manifest.get(doc_id, {})
    title = meta.get("title") or path.stem.replace("-", " ").replace("_", " ").title()
    url = meta.get("url")
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        pages = _parse_pdf(path)
    elif suffix in {".md", ".txt"}:
        pages = [(1, path.read_text("utf-8"))]
    else:
        raise ValueError(f"unsupported file type: {path.name}")
    return ParsedDocument(doc_id=doc_id, title=title, source_url=url, pages=pages)


def _parse_pdf(path: Path) -> list[tuple[int, str]]:
    try:
        import pymupdf4llm
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("install ingestion extras: pip install '.[ingestion]'") from exc
    # page_chunks=True yields exactly one entry per page, in order. Using the position avoids
    # depending on metadata key names ("page" vs "page_number"), which differ across releases.
    chunks = pymupdf4llm.to_markdown(str(path), page_chunks=True)
    pages = [(i, str(c["text"])) for i, c in enumerate(chunks, start=1)]
    return [(n, t) for n, t in pages if t.strip()]


def discover(raw_dir: Path) -> tuple[list[Path], dict[str, dict[str, str]]]:
    files = sorted(p for p in raw_dir.glob("*") if p.suffix.lower() in {".pdf", ".md", ".txt"})
    return files, _manifest(raw_dir)

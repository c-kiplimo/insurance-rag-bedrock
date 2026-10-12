"""Structure-aware chunking: the step that decides retrieval quality.

Why not fixed-size windows? In a policy, the exclusion that changes the answer is usually a
separate paragraph (or a table) near the clause that matches the question. Fixed windows cut
tables in half and separate clauses from their disclaimers. This chunker:

* tracks the heading path ("III. Property Not Covered > A. Coverage A") for every block,
* keeps each Markdown table whole (splitting only huge ones, repeating the header row),
* gives limitation/exclusion/disclaimer paragraphs their own chunk typed ``limitation``,
  tagged with the same ``section_id`` so retrieval can pull them in alongside the hit,
* packs ordinary paragraphs up to a target size without crossing a section boundary,
* derives IDs from content, so re-ingesting an unchanged document is an idempotent upsert.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass

from insurance_rag.ingestion.parse import ParsedDocument
from insurance_rag.models import Chunk, ChunkType

TARGET_CHARS = 1800
MAX_CHARS = 3200

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
# Policies often use numbered / lettered / all-caps headings that survive PDF extraction
# as plain bold text rather than Markdown headings.
_ALT_HEADING = re.compile(
    r"^\**(?:"
    r"(?:SECTION|ARTICLE|PART)\s+[IVXLC\d]+\b.*"
    r"|[IVX]{1,5}\.\s+[A-Z].{2,80}"
    r"|[A-Z][A-Z \-&/,']{5,80}"
    r")\**$"
)
# "J. Requirements in Case of Loss" / "3. Deductible": short, capitalised, no final period.
# PDF extraction often renders these as list items ("- J. ...") rather than headings.
_LETTERED_HEADING = re.compile(r"^(?:[-*]\s+)?\**(?:[A-Z]|\d{1,2})\.\s+[A-Z][^.!?]{3,78}\**$")
_LIMITATION = re.compile(
    r"\b(?:we do not (?:insure|cover|pay)|does not cover|not covered|exclusion|exclude[sd]?|"
    r"limitation|limit of liability|disclaimer|notwithstanding|except as|subject to|"
    r"only if|provided that|unless)\b",
    re.I,
)


@dataclass(slots=True)
class _Block:
    kind: str  # "paragraph" | "table"
    text: str
    page: int
    heading_path: tuple[str, ...]


def _sha(*parts: object, n: int = 16) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode(), usedforsecurity=False).hexdigest()[:n]


def _heading_of(line: str) -> tuple[int, str] | None:
    m = _MD_HEADING.match(line)
    if m:
        return len(m.group(1)), m.group(2).strip("*# ").strip()
    stripped = line.strip()
    if _LETTERED_HEADING.match(stripped):
        return 2, stripped.lstrip("-* ").rstrip("* ").strip()
    if len(stripped) < 90 and _ALT_HEADING.match(stripped):
        text = stripped.strip("*").strip()
        level = 2 if re.match(r"^[A-Z]\.\s", text) else 1
        return level, text
    return None


def _blocks(doc: ParsedDocument) -> Iterator[_Block]:
    """Yield paragraph and table blocks tagged with their heading path and start page.

    Paragraphs carry across page breaks (a clause often continues on the next page); tables
    are closed at the end of each page, because PDF tables that continue usually repeat their
    header row and should not be glued together.
    """
    path: list[tuple[int, str]] = []
    para: list[str] = []
    table: list[str] = []
    para_page = 1
    table_page = 1

    def heading_path() -> tuple[str, ...]:
        return tuple(h for _, h in path)

    def take_para() -> list[_Block]:
        if not para:
            return []
        block = _Block("paragraph", " ".join(para).strip(), para_page, heading_path())
        para.clear()
        return [block]

    def take_table() -> list[_Block]:
        if not table:
            return []
        block = _Block("table", "\n".join(table), table_page, heading_path())
        table.clear()
        return [block]

    for page_no, markdown in doc.pages:
        for raw in markdown.splitlines():
            line = raw.rstrip()
            if line.lstrip().startswith("|"):
                yield from take_para()
                if not table:
                    table_page = page_no
                table.append(line)
                continue
            yield from take_table()
            heading = _heading_of(line)
            if heading:
                yield from take_para()
                level, text = heading
                while path and path[-1][0] >= level:
                    path.pop()
                path.append((level, text))
            elif not line.strip():
                yield from take_para()
            else:
                if not para:
                    para_page = page_no
                para.append(line.strip())
        yield from take_table()
    yield from take_para()


def _split_table(text: str) -> list[str]:
    if len(text) <= MAX_CHARS:
        return [text]
    lines = text.splitlines()
    header, rows = lines[:2], lines[2:]  # header row + separator row
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for row in rows:
        if size + len(row) > MAX_CHARS and current:
            parts.append("\n".join(header + current))
            current, size = [], 0
        current.append(row)
        size += len(row) + 1
    if current:
        parts.append("\n".join(header + current))
    return parts


def _split_long(text: str) -> list[str]:
    if len(text) <= MAX_CHARS:
        return [text]
    sentences = re.split(r"(?<=[.;:])\s+", text)
    parts: list[str] = []
    current = ""
    for s in sentences:
        if len(current) + len(s) > TARGET_CHARS and current:
            parts.append(current.strip())
            current = ""
        current += s + " "
    if current.strip():
        parts.append(current.strip())
    return parts


def chunk_document(doc: ParsedDocument) -> list[Chunk]:
    chunks: list[Chunk] = []
    ordinals: dict[str, int] = {}
    buffer: list[_Block] = []

    def section_of(b: _Block) -> tuple[str, str]:
        path = " > ".join(b.heading_path) or "(document start)"
        return path, _sha(doc.doc_id, path, n=12)

    def emit(text: str, kind: ChunkType, page: int, path: str, section_id: str) -> None:
        text = text.strip()
        if len(text) < 20:  # page numbers, stray headers
            return
        ordinal = ordinals.get(section_id, 0)
        ordinals[section_id] = ordinal + 1
        chunks.append(
            Chunk(
                chunk_id=_sha(doc.doc_id, section_id, ordinal, text),
                doc_id=doc.doc_id,
                doc_title=doc.title,
                section_id=section_id,
                section_path=path,
                ordinal=ordinal,
                chunk_type=kind,
                text=text,
                page_start=page,
                source_url=doc.source_url,
            )
        )

    def flush_buffer() -> None:
        if not buffer:
            return
        path, sid = section_of(buffer[0])
        emit("\n\n".join(b.text for b in buffer), ChunkType.CLAUSE, buffer[0].page, path, sid)
        buffer.clear()

    for block in _blocks(doc):
        path, sid = section_of(block)
        if buffer and section_of(buffer[0])[1] != sid:
            flush_buffer()
        if block.kind == "table":
            flush_buffer()
            for part in _split_table(block.text):
                emit(part, ChunkType.TABLE, block.page, path, sid)
        elif _LIMITATION.search(block.text[:200]):
            flush_buffer()
            for part in _split_long(block.text):
                emit(part, ChunkType.LIMITATION, block.page, path, sid)
        else:
            for part in _split_long(block.text):
                if sum(len(b.text) for b in buffer) + len(part) > TARGET_CHARS:
                    flush_buffer()
                buffer.append(_Block("paragraph", part, block.page, block.heading_path))
    flush_buffer()
    return chunks


def embedding_text(chunk: Chunk) -> str:
    """What gets embedded: the chunk prefixed with its document and section path.

    The prefix keeps a bare sentence like "This does not apply to basements." anchored to the
    coverage it modifies ("contextual chunk headers").
    """
    return f"{chunk.doc_title} | {chunk.section_path}\n{chunk.text}"

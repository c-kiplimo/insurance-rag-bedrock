"""Step 5 (first half): turn reranked chunks into the numbered context the LLM cites.

Each source carries its document, section path and page so the model (and the human reading
the citation) can locate the clause. Tables and limitation clauses are labelled so rule 2 of the
system prompt ("always include limitations") has something to point at.
"""

from __future__ import annotations

from insurance_rag.models import Citation, RetrievedChunk

NO_ANSWER = "I could not find this in the knowledge base."


def build_context(
    chunks: list[RetrievedChunk], max_chars: int
) -> tuple[str, list[Citation], list[str]]:
    """Return (context_text, citations, source_texts) within the character budget.

    Primary hits come first (rerank order); expanded structural chunks follow. Stops adding
    sources once the budget is spent, never truncating a chunk mid-way.
    """
    blocks: list[str] = []
    citations: list[Citation] = []
    texts: list[str] = []
    used = 0
    for i, rc in enumerate(chunks, start=1):
        c = rc.chunk
        label = f"S{i}"
        header = (
            f"[{label}] {c.doc_title} | {c.section_path}"
            f"{f' | p.{c.page_start}' if c.page_start else ''} | type: {c.chunk_type.value}"
        )
        block = f"{header}\n{c.text}"
        if used + len(block) > max_chars and blocks:
            break
        blocks.append(block)
        texts.append(c.text)
        used += len(block)
        citations.append(
            Citation(
                source_id=label,
                doc_title=c.doc_title,
                section_path=c.section_path,
                page=c.page_start,
                chunk_id=c.chunk_id,
                chunk_type=c.chunk_type.value,
                score=round(rc.score, 4),
                excerpt=c.text[:300],
            )
        )
    return "\n\n".join(blocks), citations, texts

from insurance_rag.ingestion.chunk import chunk_document, embedding_text
from insurance_rag.ingestion.parse import ParsedDocument
from insurance_rag.models import ChunkType

POLICY = """\
# III. Property Covered

## A. Coverage A - Building Property

We insure against direct physical loss by or from flood to the insured building described on the
declarations page, including the foundation, electrical and plumbing systems.

We do not insure any property that is not walled and roofed as described in this section.

| Item | Limit |
|---|---|
| Building | As shown on declarations |
| Debris removal | Included in limit |

## B. Coverage B - Personal Property

We insure personal property inside the insured building when you have purchased this coverage.
"""


def _doc(text: str = POLICY) -> ParsedDocument:
    return ParsedDocument("doc1", "Sample Policy", "https://example.test/p.pdf", [(1, text)])


def test_table_is_kept_whole_and_typed():
    tables = [c for c in chunk_document(_doc()) if c.chunk_type == ChunkType.TABLE]
    assert len(tables) == 1
    assert "| Building |" in tables[0].text and "| Debris removal |" in tables[0].text


def test_limitation_paragraph_is_separate_chunk_in_same_section():
    chunks = chunk_document(_doc())
    clause = next(
        c for c in chunks if c.chunk_type == ChunkType.CLAUSE and "direct physical" in c.text
    )
    limitation = next(c for c in chunks if c.chunk_type == ChunkType.LIMITATION)
    assert "do not insure" in limitation.text
    assert limitation.section_id == clause.section_id  # linkable for context expansion


def test_section_path_reflects_heading_hierarchy():
    chunks = chunk_document(_doc())
    paths = {c.section_path for c in chunks}
    assert "III. Property Covered > A. Coverage A - Building Property" in paths
    assert "III. Property Covered > B. Coverage B - Personal Property" in paths


def test_ids_are_deterministic_so_reingest_is_idempotent():
    a = [c.chunk_id for c in chunk_document(_doc())]
    b = [c.chunk_id for c in chunk_document(_doc())]
    assert a == b and len(a) == len(set(a))


def test_changed_text_changes_chunk_id():
    a = {c.chunk_id for c in chunk_document(_doc())}
    b = {c.chunk_id for c in chunk_document(_doc(POLICY.replace("foundation", "roof")))}
    assert a != b


def test_oversized_table_splits_with_header_repeated():
    rows = "\n".join(f"| item{i} | {'x' * 80} |" for i in range(80))
    text = f"# Schedule\n\n| Item | Value |\n|---|---|\n{rows}\n"
    tables = [c for c in chunk_document(_doc(text)) if c.chunk_type == ChunkType.TABLE]
    assert len(tables) > 1
    assert all(t.text.startswith("| Item | Value |") for t in tables)


def test_embedding_text_carries_document_and_section_context():
    chunk = chunk_document(_doc())[0]
    assert embedding_text(chunk).startswith("Sample Policy | III. Property Covered")


def test_all_caps_heading_without_markdown_markers_is_detected():
    text = "EXCLUSIONS AND LIMITATIONS\n\nThis policy does not cover loss caused by earth movement."
    chunks = chunk_document(_doc(text))
    assert chunks and chunks[0].section_path == "EXCLUSIONS AND LIMITATIONS"


def test_lettered_subheading_rendered_as_list_item_becomes_section():
    text = (
        "# SECTION VII - GENERAL CONDITIONS\n\n"
        "- J. Requirements in Case of Loss\n\n"
        "Within 60 days after the loss, send us a signed proof of loss."
    )
    chunks = chunk_document(_doc(text))
    assert (
        chunks[0].section_path
        == "SECTION VII - GENERAL CONDITIONS > J. Requirements in Case of Loss"
    )


def test_ordinary_sentence_starting_with_letter_is_not_a_heading():
    text = "# Coverage\n\nA. We pay for covered loss to the building when the policy is in force."
    chunks = chunk_document(_doc(text))
    assert chunks[0].section_path == "Coverage"

# Data: what to feed it when you have no real data

The system needs two kinds of text: **policy/underwriting documents** (the knowledge base)
and, to exercise governance, **claim notes containing PHI/PII**. Both can be had for free.

## 1. Free public insurance documents (the knowledge base)

`src/insurance_rag/ingestion/sources.yaml` lists the starting set. All three are U.S. federal
National Flood Insurance Program (NFIP) publications, which is a good test corpus because it
has what the case study describes: a long multi-page contract with defined terms, coverage
tables and schedules, and exclusions and limits scattered across sections.

| ID | Document | Why it is useful |
|---|---|---|
| `nfip-dwelling-sfip-2015` | NFIP Dwelling Form, Standard Flood Insurance Policy (F-122) | A real policy contract: coverages A–D, property not covered, deductibles, proof-of-loss deadlines |
| `nfip-flood-insurance-manual-appendices-2021` | NFIP Flood Insurance Manual, Appendices (Oct 2021) | Underwriting-guideline style content with many tables |
| `nfip-summary-of-coverage` | NFIP Summary of Coverage | A short, plain-language summary to test cross-document retrieval |

```bash
python -m insurance_rag download          # writes data/raw/*.pdf and data/raw/manifest.json
```

* **URLs move.** Agencies reorganise their sites. The downloader checks that each response
  is a real PDF and logs a failure per document without aborting the run. If one is gone,
  search for the document title on fema.gov (or the publishing agency), download it by
  hand into `data/raw/`, and name it `<id>.pdf`.
* **Licensing.** NFIP forms are U.S. government works. Still check the terms on whatever
  site you download from, and do not assume other sources are free to redistribute.
* **More free sources to try** (verify availability and terms yourself): state insurance
  department sample policy forms and consumer guides, NAIC model laws and consumer
  publications, Medicare and Medicaid coverage manuals, and sample homeowners or auto policy
  forms that state regulators publish for consumers.

### Adding your own documents

Drop `.pdf`, `.md` or `.txt` files into `data/raw/` and run `ingest`. The file name (minus
extension) becomes the document id; add an entry to `manifest.json` (`title`, `url`) if you
want a friendly title and a source link in citations.

### Scanned PDFs

`pymupdf4llm` reads text PDFs. For scans, run Amazon Textract (`AnalyzeDocument` with
`TABLES`/`LAYOUT`) first, convert its output to Markdown, and place the `.md` files in
`data/raw/`. The chunker works on Markdown, so nothing else changes.

### Check how a document was chunked

The summary printed by `ingest` shows chunks by type. A policy with zero `table` chunks, or
almost no `limitation` chunks, usually means the PDF's headings or tables were not detected.
To look closer:

```python
from pathlib import Path
from insurance_rag.ingestion.parse import parse_file
from insurance_rag.ingestion.chunk import chunk_document

doc = parse_file(Path("data/raw/nfip-dwelling-sfip-2015.pdf"), {})
for c in chunk_document(doc)[:40]:
    print(c.chunk_type.value, "|", c.section_path, "|", c.page_start, "|", c.text[:90])
```

Fixing heading detection for your documents (`_ALT_HEADING` / `_LETTERED_HEADING` in
`chunk.py`) is the highest-leverage tuning you can do.

## 2. Synthetic claim notes (for the PHI/PII path)

```bash
python -m insurance_rag generate-claims --count 25
```

Writes `data/raw/synthetic-claim-NNN.md`: invented claim files with names, SSNs (900-series,
never issued), 555-01xx phone numbers, emails at `example.com`, DOBs, MRNs and addresses,
each labelled SYNTHETIC. They exist to prove that redaction runs before chunking and
embedding:

```bash
python -m insurance_rag ingest --redactor regex           # catches structured identifiers
python -m insurance_rag ingest --redactor regex,comprehend # also catches names and addresses
```

Then confirm nothing leaked: search the index for the fake values.

```bash
curl -s localhost:9200/policy-chunks/_search -H 'content-type: application/json' \
  -d '{"query":{"query_string":{"query":"\"900-\" OR example.com OR Rivera"}},"size":3}' | jq '.hits.total'
```

Expect zero hits for the SSN and email patterns with either redactor, and zero hits for the
names only with `comprehend` enabled (regex cannot catch free-text names; that is
why the Comprehend layer exists). Remove the synthetic files (or use a separate index) when
you want a purely policy-document corpus, because they will compete for retrieval on claim-like
questions.

## 3. The evaluation questions

`src/insurance_rag/evaluation/dataset.jsonl` has seven grounded questions about the NFIP
Dwelling Form and three refusal tests (off-topic, prompt injection, creative writing).

**The grounded questions are unverified seeds.** Each has `"verified": false`. They were
written from general knowledge of the policy and each expected fact (`must_contain_any`)
must be confirmed against the exact edition you ingested. Then flip `verified` to `true`.
Until you do, a failing question may be a wrong expectation rather than a wrong answer.

To grow the set: add 30–50 real questions your adjusters ask, each with a reference answer
and a short distinctive string that must appear. Ragas can also draft candidate questions
from your chunks; treat generated questions as drafts a human must review.

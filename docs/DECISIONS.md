# Architecture decisions

Short records of the choices that shape the system, with what was given up.

## D1. OpenSearch as the single search layer (dense + sparse)
**Decision:** one OpenSearch index serves BM25 and k-NN, combined by a search pipeline.
**Why:** insurance questions mix exact terms ("Coverage D", "$30,000", state codes) with
paraphrase. Dense-only misses exact numbers; keyword-only misses intent. One system means one
thing to secure, back up and monitor.
**Rejected:** a dedicated vector database such as Milvus: more operational surface and a less
mature sparse story than OpenSearch's production-proven hybrid search.
**Cost:** a managed domain runs 24/7 (OpenSearch Serverless would scale, but lacks search
pipelines, which this design relies on).

## D2. Custom ingestion instead of Bedrock Knowledge Bases
**Decision:** we parse, chunk, redact, embed and index ourselves.
**Why:** policy documents live or die on how tables and clauses are chunked, and PHI must be
redacted *before* embedding. Knowledge Bases gives limited control over both.
**Cost:** we own the parser and chunker (`ingestion/`), the highest-maintenance code here.

## D3. Structure-aware chunks with section links, plus context expansion
**Decision:** chunks are typed (`clause`/`table`/`limitation`) and share a `section_id`; after
reranking, tables and limitations from the winning sections are added.
**Why:** the business problem is that disclaimers and limits are separated from the clause that
matches the query. Fixed-size windows and pure similarity search reproduce that failure.
**Cost:** extra query per request and heading-detection heuristics that need tuning per corpus.

## D4. Rerank with a cross-encoder, then threshold
**Decision:** retrieve 30, rerank to 6, drop below a relevance floor, and return "not found"
with no generation call if nothing survives.
**Why:** embedding similarity is coarse; refusing early is the cheapest hallucination control.
**Cost:** one extra API call (~100–300 ms) and a region constraint for the rerank model.

## D5. Everything model-related in Bedrock
**Decision:** embeddings, rerank, guardrails, prompts and generation all via Bedrock.
**Why:** PHI/PII stays inside the AWS boundary (with VPC endpoints), one IAM and audit model.
**Cost:** model choice limited to what Bedrock offers; cross-region constraints for some features.

## D6. Semantic cache in front of the whole pipeline, with redaction first
**Decision:** cache keyed by exact hash and by embedding similarity, namespaced by
corpus/prompt/model; questions redacted locally before embedding.
**Why:** repeated lookups are common in internal portals; hits skip every paid stage.
**Cost:** possible wrong hits if the threshold is too low (tune `RAG_CACHE_SIMILARITY`); shared
across users; needs a vector-capable engine (fallback: exact mode).

## D7. Ports and adapters
**Decision:** the pipeline depends on Protocols; AWS clients are adapters wired in one place.
**Why:** the entire workflow is unit-testable with fakes (55 tests, no AWS), and components
can be swapped without touching orchestration.
**Cost:** a little indirection.

## D8. Prompts: Git is the source of truth, Bedrock Prompt Management is the delivery channel
**Decision:** YAML in Git → reviewed → gate → published as an immutable Bedrock version →
runtime pulls it, with the baked-in copy as fallback.
**Why:** reviewable history plus the ability to ship a prompt change without rebuilding the
container; failures degrade to the last known prompt instead of an outage.
**Cost:** two places the prompt exists; `prompts.py` and the sync script must agree on shape.

## D9. Automated quality gate instead of manual prompt review
**Decision:** every commit runs the real pipeline over a golden set, scored by deterministic
checks and an LLM judge (Ragas on Bedrock); failures block deployment; results are stored by
commit SHA in DynamoDB.
**Why:** objective, repeatable, and creates an audit trail of quality over time.
**Cost:** LLM-judge tokens per run, and a golden set that must be curated and kept honest.
A judge model is itself imperfect: spot-check its verdicts.

## D10. DynamoDB for evaluation results
**Decision:** on-demand table keyed by commit SHA.
**Why:** intermittent, non-daily writes; scales to zero cost between runs, no instance to run.
**Rejected:** a relational database for the same reason (idle cost, patching).

## D11. Langfuse for LLM observability, CloudWatch for infrastructure
**Decision:** Langfuse holds prompt-level traces (variables, retrieval steps, token usage);
CloudWatch keeps infrastructure health; CloudTrail is the API/configuration audit log.
**Why:** CloudWatch's Bedrock metrics lack prompt-level granularity for ML debugging; DevOps
keeps the tool it already operates.
**Cost:** a self-hosted Langfuse (Postgres, ClickHouse, Redis, blob store) to run. Payloads are
masked for PHI before leaving the process.

## D12. Python, synchronous handlers, scale by tasks
**Decision:** Python 3.12 + FastAPI with sync handlers.
**Why:** the work is I/O-bound on Bedrock/OpenSearch; the best evaluation and parsing libraries
are Python; sync handlers run in a threadpool without blocking the event loop.
**Alternative:** Kotlin/Spring is a sound choice for the API if your team is JVM-based, with
the evaluation/ingestion tooling kept in Python.

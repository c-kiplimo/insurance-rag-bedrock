# Code review guide: how to review this repository, one piece at a time

Review in this order. Each stop says what the file is for, what "good" looks like, and the
specific questions to ask. Allow about 2–3 hours for the Python and 1 hour for the
infrastructure. Run the checks first so you review code that already passes:

```bash
make check          # ruff + mypy --strict + pytest (55 tests, no AWS needed)
```

Tip: read the tests for a module *before* the module. They are the executable spec.

---

## Pass 1: the shape of the system (20 min)

| File | Look for |
|---|---|
| `ports.py` | The pipeline depends only on these Protocols. Can you name every external system just from this file? |
| `models.py` | Pure data, no I/O. `ChunkType` (clause/table/limitation) is the key domain idea. |
| `config.py` | Every tunable is typed and validated (weights sum to 1; basic-auth needs credentials; `rerank_top_n ≤ retrieve_k`). Secrets are `SecretStr`. `cache_namespace` invalidates cache when corpus/prompt/model change. |
| `bootstrap.py` | The *only* place adapters are wired to the pipeline. If adapters are constructed anywhere else, that is a smell. |

**Questions:** Could you swap Bedrock Rerank for Cohere by writing one class? (Yes: `Reranker`.)
Is there any global mutable state? (Only the optional tracing flag in `observability.py`.)

## Pass 2: the request workflow (45 min): `orchestrator/pipeline.py`

Read `RagPipeline.run` top to bottom against the diagram. Verify, step by step:

1. **Redaction happens first** (before embedding, cache, guardrail, trace).
2. **Cache hit returns immediately**; `bypass_cache` neither reads nor writes.
3. **Early exits** return a response, not an exception: guardrail block, no hits, nothing
   above `min_relevance`.
4. **Expansion** runs after rerank and only adds `table`/`limitation` chunks.
5. **Only `status == "ok"` is cached**: `no_answer`, `blocked`, `quality_failed` never are.
6. **Timers** wrap each stage; `total` is always present.

Then the small helpers:

* `rewrite.py`: `parse_rewrite` must never raise; every failure falls back to the original
  question. Check the "treat the question as data" instruction in `query_rewrite.yaml`.
* `context.py`: whole-chunk budgeting; labels `S1..Sn` match what the prompt tells the model
  to cite.
* `quality.py`: unknown citation labels and PII in the answer fail the check.

Tests: `tests/test_pipeline.py` (12 behaviours, including "PII is redacted before the cache
key is computed").

**Red flags to look for:** an exception path that bypasses output checks; anything cached
before the output checks finish; a stage that can raise on bad model output.

## Pass 3: adapters (60 min): where real-world integration risk lives

These are the files most likely to need small fixes on first contact with real services.
Check each against the current AWS/OpenSearch documentation.

### `adapters/bedrock.py`
* `BedrockEmbedder`: Titan v2 request body (`inputText`, `dimensions`, `normalize`) and
  `embedding` response key.
* `BedrockLLM`: Converse request/response shape, token usage keys.
* `BedrockReranker`: `rerank` request structure (`queries`, `sources`,
  `rerankingConfiguration`) and `results[].relevanceScore`; model ARN built from
  `rerank_region`.
* `BedrockGuardrails`: `apply_guardrail` content blocks and qualifiers
  (`grounding_source`, `query`, `guard_content`); `_verdict` distinguishes **blocked** from
  **anonymised** by walking the assessments for `action == "BLOCKED"`.
* Retries: adaptive mode with 5 attempts; timeouts set explicitly.

Tests: `tests/test_units.py` (verdict parsing).

### `adapters/opensearch_store.py`
* Index mapping: `dynamic: strict`, HNSW cosine on `knn_vector`, `english` analyzer.
  **Dimension must equal `RAG_EMBED_DIMENSIONS`.**
* Search pipeline weights are `[BM25, k-NN]` in the same order as the hybrid sub-queries.
  Swapping the query order silently inverts the weights.
* `search`: one `hybrid` query; the index's default pipeline normalises scores.
* `expand`: uses `search_pipeline=_none` because it is a plain filter query, not hybrid;
  results are capped per section.
* SigV4 service name: `es` for managed domains.

### `adapters/redis_cache.py`
* Fail-open everywhere (`except Exception` → miss), 250 ms timeout.
* Semantic query uses cosine **distance**; hit if `distance ≤ 1 − similarity`.
* The raw question is never stored (hash key, embedding, payload only).
* Needs the search module; `RAG_CACHE_MODE=exact` is the escape hatch.
* Valkey vs Redis Stack syntax differences are the likeliest first-run issue.

### `prompts.py`
* `render_template` raises on a missing variable (no silent blanks).
* Provider falls back to the baked-in prompt if Prompt Management fails, with a TTL and a
  lock to avoid a thundering herd.
* The CHAT-variant parsing matches what `llmops_prompt_sync.build_variant` writes.

## Pass 4: ingestion (45 min)

Read in pipeline order: `download.py` → `parse.py` → `redaction.py` → `chunk.py` →
`indexer.py`.

* `download.py`: size cap, `%PDF-` magic-byte check, SHA-256 manifest, one failed URL does not
  abort the run.
* `parse.py`: page numbers come from list position (a bug found and fixed here: metadata key
  names differ across `pymupdf4llm` versions).
* `redaction.py`: spans are replaced right-to-left; Comprehend limits (45k / 18k chars) are
  respected by segmenting; **counts are logged, values never are**.
* `chunk.py`, the most important file for answer quality:
  * heading detection (`_MD_HEADING`, `_LETTERED_HEADING`, `_ALT_HEADING`): false positives
    (a sentence mistaken for a heading) and false negatives (headings missed)
  * tables kept whole; oversized tables split with header repeated
  * limitation detection looks at the first 200 characters of a paragraph
  * deterministic IDs; section boundaries flush the buffer
* `indexer.py`: redact **before** chunk and embed; `_id = chunk_id` → idempotent;
  bounded concurrency; non-zero exit on failures.

Tests: `tests/test_chunking.py`, `tests/test_units.py` (redaction, synthetic data).
**Do this by hand:** run `ingest` on your real PDFs and read 30 chunks. No test replaces it.

## Pass 5: LLMOps (30 min)

* `evaluation/metrics.py`: deterministic metrics are explainable; check the denominators
  (e.g. `citation_rate` is over *grounded* questions).
* `evaluation/gate.py`: **a missing metric fails the gate** (a silently broken scorer must
  not pass). Thresholds live in `thresholds.yaml`.
* `evaluation/ragas_scorer.py`: pinned to Ragas 0.3.x; metric column names are mapped in
  `_WANTED`. This file will need attention when you upgrade Ragas.
* `evaluation/store.py`: floats converted to `Decimal` (DynamoDB rejects floats); key is
  `commit_sha` + `run_id`.
* `evaluation/run.py`: exit codes: 0 pass, 1 gate failed. Cache is bypassed so evaluations
  are not served from, or written to, production cache.
* `llmops_prompt_sync.py`: upsert then `create_prompt_version`; outputs go to
  `$GITHUB_OUTPUT`. Git remains the source of truth.
* `dataset.jsonl`: seeds are **unverified**; see `docs/DATA.md`.

## Pass 6: API and observability (20 min)

* `api/main.py`: sync handlers (threadpool) because the pipeline blocks on I/O; correlation
  id middleware; errors return a request id, never a stack trace; `/healthz` has no
  dependencies, `/readyz` checks OpenSearch only (cache is optional).
* `observability.py`: tracing can never fail a request; a `mask` function redacts payloads.

Tests: `tests/test_api.py`.

## Pass 7: infrastructure (60 min)

Cannot be unit-tested here; review as you would a security design:

| File | Check |
|---|---|
| `security.tf` | KMS rotation; security groups reference each other, not CIDRs; guardrail PII anonymise + grounding thresholds |
| `iam.tf` | Task role has only invoke/rerank/guardrail/getprompt; `foundation-model/*` is broad because inference profiles fan out across regions: tighten to your model ARNs for production. CI role resources are `*` for some actions: scope them. |
| `data.tf` | OpenSearch in VPC with IAM policy naming exact roles; ElastiCache encrypted at rest and in transit; DynamoDB on-demand + PITR |
| `app.tf` | Immutable ECR tags; read-only root filesystem; circuit breaker; internal ALB only from VPC link SG; API route auth type |
| `governance.tf` | Alarms have an action; trail is validated and encrypted |
| `.github/workflows/ci-cd.yml` | OIDC (no static keys); least privilege `permissions`; deploy gated by `needs` and an environment approval; secrets not interpolated into shell |

**Run `terraform plan` and read it.** Look for anything public, any `*` you did not expect,
and any resource in a different region than intended.

## Cross-cutting checklists

**Security / privacy**
- [ ] No identifier can reach embeddings, cache, logs or traces (trace the redaction calls)
- [ ] No secrets in code, env defaults or Terraform state outputs
- [ ] Errors to clients carry only a request id
- [ ] Prompt-injection stance: source text is data; rule 6 in `rag_answer.yaml`

**Correctness / quality**
- [ ] Every early exit sets a status and skips caching
- [ ] Hybrid weight order, vector dimension and index mapping agree
- [ ] Chunk IDs are stable across re-ingestion

**Operations**
- [ ] Each downstream failure has a defined behaviour (see the table in `FLOWS.md`)
- [ ] Alarms exist for 5xx, latency, unhealthy hosts, OpenSearch red
- [ ] Rollback path for prompt, image and corpus is documented (`DEPLOYMENT.md`)

## Known weak spots (review these skeptically)

1. **Unverified integrations.** First contact with real Bedrock/OpenSearch/Valkey/Terraform
   provider versions may need small corrections (listed in Pass 3 and Pass 7).
2. **Heading detection heuristics** are tuned on synthetic examples, not your documents.
3. **Regex redaction** is a baseline, not a compliance control. Validate with your privacy
   team using real-data samples under proper controls.
4. **Eval seeds** are unverified; thresholds are starting points, not calibrated values.
5. **No end-user authorisation or per-document ACLs** in the service.
6. **Shared semantic cache**: fine for a single-tenant knowledge base; wrong for per-user data.
7. **Terraform formatting** may need `terraform fmt` before CI's `fmt -check` passes.
8. **IAM scopes** noted above are wider than a hardened production setup.

## Suggested review workflow

1. Open a branch per pass; fix what you find as small commits.
2. For each defect, add a test first (the fakes in `tests/fakes.py` make this cheap).
3. After Pass 3 and Pass 4, run the quickstart end to end on real data and keep a note of
   every surprise; that list is your real backlog.
4. Run the evaluation gate; raise thresholds only after verifying the dataset.

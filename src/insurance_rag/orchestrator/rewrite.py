"""Step 2: query rewriting and HyDE (Hypothetical Document Embeddings).

An LLM turns the user's phrasing into the vocabulary a policy would use, and for short or
conceptual questions drafts a hypothetical answer passage whose embedding lands closer to real
clauses than the question's embedding does. Any failure falls back to the original question:
rewriting is an optimisation and must never block an answer.
"""

from __future__ import annotations

import json
import logging
import re

from insurance_rag.models import RewriteResult
from insurance_rag.observability import traced
from insurance_rag.ports import LLM, PromptTemplate
from insurance_rag.prompts import LocalPromptProvider

log = logging.getLogger(__name__)
_JSON = re.compile(r"\{.*\}", re.S)


def parse_rewrite(raw: str, original: str) -> RewriteResult:
    """Tolerant parser: models sometimes wrap JSON in prose or code fences."""
    match = _JSON.search(raw)
    if not match:
        return RewriteResult(query=original)
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return RewriteResult(query=original)
    query = data.get("query")
    hyde = data.get("hyde")
    if not isinstance(query, str) or not query.strip():
        query = original
    if not isinstance(hyde, str) or not hyde.strip():
        hyde = None
    return RewriteResult(query=query.strip()[:500], hyde_passage=hyde[:1200] if hyde else None)


class QueryRewriter:
    def __init__(self, llm: LLM, model_id: str | None) -> None:
        self._llm = llm
        self._model_id = model_id
        self._template: PromptTemplate = LocalPromptProvider("query_rewrite.yaml").get()

    @traced("query-rewrite", as_type="generation")
    def rewrite(self, question: str) -> RewriteResult:
        try:
            system, user = self._template.render(question=question)
            result = self._llm.generate(
                system,
                user,
                max_tokens=self._template.max_tokens,
                temperature=self._template.temperature,
                model_id=self._model_id,
            )
            return parse_rewrite(result.text, question)
        except Exception:
            log.warning("query rewrite failed; using original question", exc_info=True)
            return RewriteResult(query=question)

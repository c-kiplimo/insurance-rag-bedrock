"""Ragas scoring with Amazon Bedrock as the LLM-as-a-judge.

Imports are lazy so the API image and unit tests do not need Ragas/LangChain. Written against
the Ragas 0.3.x API (``EvaluationDataset`` + ``evaluate``). Ragas changes its API between minor
versions, so ``pyproject.toml`` pins ``ragas>=0.3,<0.4``; re-check this file when you bump it.
"""

from __future__ import annotations

import math
from typing import Any

from insurance_rag.evaluation.metrics import EvalRow

_WANTED = {
    "faithfulness": "faithfulness",
    "answer_relevancy": "answer_relevancy",
    "context_precision": "llm_context_precision_with_reference",
}


def score_with_ragas(
    rows: list[EvalRow], *, judge_model_id: str, embed_model_id: str, region: str
) -> dict[str, float]:
    from langchain_aws import BedrockEmbeddings, ChatBedrockConverse
    from ragas import EvaluationDataset, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import Faithfulness, LLMContextPrecisionWithReference, ResponseRelevancy

    scored = [r for r in rows if r.category == "grounded" and r.status == "ok" and r.contexts]
    if not scored:
        return {}

    llm = LangchainLLMWrapper(
        ChatBedrockConverse(model_id=judge_model_id, region_name=region, temperature=0.0)
    )
    embeddings = LangchainEmbeddingsWrapper(
        BedrockEmbeddings(model_id=embed_model_id, region_name=region)
    )
    dataset = EvaluationDataset.from_list(
        [
            {
                "user_input": r.question,
                "response": r.answer,
                "retrieved_contexts": r.contexts,
                "reference": r.reference,
            }
            for r in scored
        ]
    )
    result: Any = evaluate(
        dataset,
        metrics=[Faithfulness(), ResponseRelevancy(), LLMContextPrecisionWithReference()],
        llm=llm,
        embeddings=embeddings,
        raise_exceptions=False,
    )
    return _means(result.scores)


def _means(per_sample: list[dict[str, float]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for ours, theirs in _WANTED.items():
        vals = [
            float(s[theirs])
            for s in per_sample
            if theirs in s and s[theirs] is not None and not math.isnan(float(s[theirs]))
        ]
        if vals:
            out[ours] = round(sum(vals) / len(vals), 4)
    return out

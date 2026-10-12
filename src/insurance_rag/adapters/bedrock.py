"""Amazon Bedrock adapters: embeddings, LLM (Converse), reranking, guardrails.

All data stays inside the AWS network boundary when the service runs with VPC endpoints for
``bedrock-runtime`` and ``bedrock-agent-runtime``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import boto3
from botocore.config import Config

from insurance_rag.config import Settings
from insurance_rag.ports import GuardrailVerdict, LLMResult

log = logging.getLogger(__name__)


def _boto_config(region: str, timeout: int) -> Config:
    return Config(
        region_name=region,
        retries={"max_attempts": 5, "mode": "adaptive"},  # backs off on ThrottlingException
        connect_timeout=5,
        read_timeout=timeout,
        max_pool_connections=32,
    )


class BedrockEmbedder:
    def __init__(self, settings: Settings) -> None:
        self._rt: Any = boto3.client(
            "bedrock-runtime",
            config=_boto_config(settings.aws_region, settings.bedrock_timeout_seconds),
        )
        self._model = settings.embed_model_id
        self._dims = settings.embed_dimensions

    def embed(self, text: str) -> list[float]:
        body = json.dumps({"inputText": text, "dimensions": self._dims, "normalize": True})
        resp = self._rt.invoke_model(
            modelId=self._model,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        return json.loads(resp["body"].read())["embedding"]  # type: ignore[no-any-return]


class BedrockLLM:
    def __init__(self, settings: Settings) -> None:
        self._rt: Any = boto3.client(
            "bedrock-runtime",
            config=_boto_config(settings.aws_region, settings.bedrock_timeout_seconds),
        )
        self._default_model = settings.llm_model_id

    def generate(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float,
        model_id: str | None = None,
    ) -> LLMResult:
        resp = self._rt.converse(
            modelId=model_id or self._default_model,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
        )
        blocks = resp["output"]["message"]["content"]
        usage = resp.get("usage", {})
        return LLMResult(
            text="".join(b.get("text", "") for b in blocks).strip(),
            input_tokens=int(usage.get("inputTokens", 0)),
            output_tokens=int(usage.get("outputTokens", 0)),
        )


class BedrockReranker:
    def __init__(self, settings: Settings) -> None:
        # The Rerank API lives on bedrock-agent-runtime, in regions that offer the model.
        self._client: Any = boto3.client(
            "bedrock-agent-runtime",
            config=_boto_config(settings.rerank_region, settings.bedrock_timeout_seconds),
        )
        self._arn = settings.rerank_model_arn

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        if not documents:
            return []
        resp = self._client.rerank(
            queries=[{"type": "TEXT", "textQuery": {"text": query}}],
            sources=[
                {
                    "type": "INLINE",
                    "inlineDocumentSource": {"type": "TEXT", "textDocument": {"text": d}},
                }
                for d in documents
            ],
            rerankingConfiguration={
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {"modelArn": self._arn},
                    "numberOfResults": min(top_n, len(documents)),
                },
            },
        )
        results = sorted(resp["results"], key=lambda r: r["relevanceScore"], reverse=True)
        return [(int(r["index"]), float(r["relevanceScore"])) for r in results]


def _any_blocked(node: Any) -> bool:
    """Walk a guardrail assessment tree looking for an action of BLOCKED."""
    if isinstance(node, dict):
        if node.get("action") == "BLOCKED":
            return True
        return any(_any_blocked(v) for v in node.values())
    if isinstance(node, list):
        return any(_any_blocked(v) for v in node)
    return False


class BedrockGuardrails:
    """Bedrock ``ApplyGuardrail``: independent of any model call, so we can guard input before
    spending tokens and guard output before it leaves the service."""

    def __init__(self, settings: Settings) -> None:
        self._rt: Any = boto3.client(
            "bedrock-runtime",
            config=_boto_config(settings.aws_region, settings.bedrock_timeout_seconds),
        )
        self._id = settings.guardrail_id
        self._version = settings.guardrail_version
        self._grounding = settings.guardrail_grounding

    def _apply(self, source: str, content: list[dict[str, Any]]) -> dict[str, Any]:
        return self._rt.apply_guardrail(  # type: ignore[no-any-return]
            guardrailIdentifier=self._id,
            guardrailVersion=self._version,
            source=source,
            content=content,
        )

    def check_input(self, text: str) -> GuardrailVerdict:
        resp = self._apply("INPUT", [{"text": {"text": text}}])
        return self._verdict(resp, original=text)

    def check_output(self, answer: str, question: str, sources: list[str]) -> GuardrailVerdict:
        content: list[dict[str, Any]] = []
        if self._grounding:
            content.append(
                {"text": {"text": "\n\n".join(sources), "qualifiers": ["grounding_source"]}}
            )
            content.append({"text": {"text": question, "qualifiers": ["query"]}})
            content.append({"text": {"text": answer, "qualifiers": ["guard_content"]}})
        else:
            content.append({"text": {"text": answer}})
        resp = self._apply("OUTPUT", content)
        return self._verdict(resp, original=answer)

    @staticmethod
    def _verdict(resp: dict[str, Any], original: str) -> GuardrailVerdict:
        if resp.get("action") != "GUARDRAIL_INTERVENED":
            return GuardrailVerdict(blocked=False, text=original)
        outputs = resp.get("outputs") or []
        replacement = outputs[0].get("text") if outputs else None
        if _any_blocked(resp.get("assessments", [])):
            log.warning("guardrail blocked content", extra={"guardrail_action": "BLOCKED"})
            return GuardrailVerdict(blocked=True, text=replacement, reason="guardrail_blocked")
        # intervened without a block => content was anonymised (PII masked)
        return GuardrailVerdict(blocked=False, text=replacement or original, reason="anonymised")


class NoopGuardrails:
    """Used when no guardrail id is configured (local development)."""

    def check_input(self, text: str) -> GuardrailVerdict:
        return GuardrailVerdict(blocked=False, text=text)

    def check_output(self, answer: str, question: str, sources: list[str]) -> GuardrailVerdict:
        return GuardrailVerdict(blocked=False, text=answer)

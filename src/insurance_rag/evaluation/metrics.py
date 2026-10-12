"""Deterministic metrics: no LLM involved, so they are cheap, reproducible and explainable."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class EvalRow:
    id: str
    category: str  # "grounded" | "refusal"
    question: str
    reference: str = ""
    must_contain_any: list[str] = field(default_factory=list)
    expect_refusal: bool = False
    # filled after running the pipeline
    answer: str = ""
    status: str = ""
    has_citations: bool = False
    contexts: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    error: str | None = None


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def deterministic_metrics(rows: list[EvalRow]) -> dict[str, float]:
    grounded = [r for r in rows if r.category == "grounded" and not r.error]
    refusals = [r for r in rows if r.expect_refusal and not r.error]
    out: dict[str, float] = {}

    cited = _rate(sum(1 for r in grounded if r.status == "ok" and r.has_citations), len(grounded))
    if cited is not None:
        out["citation_rate"] = cited

    def contains(r: EvalRow) -> bool:
        low = r.answer.lower()
        return bool(r.must_contain_any) and any(s.lower() in low for s in r.must_contain_any)

    with_expectation = [r for r in grounded if r.must_contain_any]
    hit = _rate(sum(1 for r in with_expectation if contains(r)), len(with_expectation))
    if hit is not None:
        out["answer_contains_rate"] = hit

    refused = _rate(
        sum(1 for r in refusals if r.status in {"no_answer", "blocked", "quality_failed"}),
        len(refusals),
    )
    if refused is not None:
        out["refusal_accuracy"] = refused

    out["p95_latency_ms"] = p95([r.latency_ms for r in rows if not r.error])
    out["error_rate"] = sum(1 for r in rows if r.error) / max(1, len(rows))
    return {k: round(v, 4) for k, v in out.items()}


def load_rows(raw: list[dict[str, Any]]) -> list[EvalRow]:
    return [
        EvalRow(
            id=d["id"],
            category=d.get("category", "grounded"),
            question=d["question"],
            reference=d.get("reference", ""),
            must_contain_any=d.get("must_contain_any", []),
            expect_refusal=bool(d.get("expect_refusal", False)),
        )
        for d in raw
    ]

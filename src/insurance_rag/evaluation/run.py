"""Evaluation entry point used by CI:

    python -m insurance_rag.evaluation.run --commit "$GITHUB_SHA" --store dynamodb

Exit code 0 = gate passed, 1 = gate failed (deployment must not proceed), 2 = harness error.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from importlib import resources
from pathlib import Path
from typing import Any

from insurance_rag.config import get_settings
from insurance_rag.evaluation.gate import evaluate_gate, load_thresholds
from insurance_rag.evaluation.metrics import EvalRow, deterministic_metrics, load_rows
from insurance_rag.logging_setup import configure_logging
from insurance_rag.models import QueryRequest

log = logging.getLogger("insurance_rag.eval")


def _read_dataset(path: Path | None) -> list[dict[str, Any]]:
    text = (
        path.read_text("utf-8")
        if path
        else resources.files("insurance_rag.evaluation")
        .joinpath("dataset.jsonl")
        .read_text("utf-8")
    )
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def run_pipeline(rows: list[EvalRow]) -> None:
    from insurance_rag.bootstrap import build_app

    app = build_app(get_settings())
    for row in rows:
        start = time.perf_counter()
        try:
            run = app.pipeline.run(QueryRequest(question=row.question, bypass_cache=True))
            row.answer = run.response.answer
            row.status = run.response.status
            row.has_citations = bool(run.response.citations)
            row.contexts = run.contexts
        except Exception as exc:
            log.exception("pipeline error", extra={"question_id": row.id})
            row.error = type(exc).__name__
        row.latency_ms = (time.perf_counter() - start) * 1000


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--commit", default=os.getenv("GITHUB_SHA", "local"))
    p.add_argument("--dataset", type=Path)
    p.add_argument("--thresholds", type=Path)
    p.add_argument("--scorer", choices=["ragas", "none"], default="ragas")
    p.add_argument("--judge-model", default=None, help="Bedrock model id for LLM-as-judge")
    p.add_argument("--store", choices=["dynamodb", "none"], default="none")
    p.add_argument("--table", default=os.getenv("RAG_EVAL_TABLE", "insurance-rag-eval-results"))
    p.add_argument("--out", type=Path, default=Path("eval_report.json"))
    args = p.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)

    rows = load_rows(_read_dataset(args.dataset))
    run_pipeline(rows)

    scores = deterministic_metrics(rows)
    if args.scorer == "ragas":
        from insurance_rag.evaluation.ragas_scorer import score_with_ragas

        scores.update(
            score_with_ragas(
                rows,
                judge_model_id=args.judge_model or settings.llm_model_id,
                embed_model_id=settings.embed_model_id,
                region=settings.aws_region,
            )
        )

    thresholds = load_thresholds(args.thresholds)
    if args.scorer == "none":  # LLM-judge metrics intentionally skipped
        for k in ("faithfulness", "answer_relevancy", "context_precision"):
            thresholds["min"].pop(k, None)
    result = evaluate_gate(scores, thresholds)

    report = {
        "commit": args.commit,
        "passed": result.passed,
        "failures": result.failures,
        "scores": scores,
        "rows": [
            {
                "id": r.id,
                "status": r.status,
                "error": r.error,
                "latency_ms": round(r.latency_ms),
                "answer": r.answer[:500],
            }
            for r in rows
        ],
    }
    args.out.write_text(json.dumps(report, indent=2))

    if args.store == "dynamodb":
        from insurance_rag.evaluation.store import build_item, save_run

        item = build_item(
            args.commit,
            scores,
            result.passed,
            result.failures,
            {
                "llm_model": settings.llm_model_id,
                "prompt_id": settings.prompt_id,
                "prompt_version": settings.prompt_version or "local",
                "corpus_version": settings.corpus_version,
                "questions": len(rows),
                "github_run": os.getenv("GITHUB_RUN_ID"),
                "branch": os.getenv("GITHUB_REF_NAME"),
            },
        )
        save_run(args.table, settings.aws_region, item)

    print(
        json.dumps(
            {"passed": result.passed, "scores": scores, "failures": result.failures}, indent=2
        )
    )
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())

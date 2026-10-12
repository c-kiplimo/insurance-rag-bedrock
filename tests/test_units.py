import json

import pytest

from insurance_rag.adapters.bedrock import BedrockGuardrails, _any_blocked
from insurance_rag.adapters.opensearch_store import index_body, pipeline_body
from insurance_rag.config import Settings
from insurance_rag.evaluation.gate import evaluate_gate
from insurance_rag.evaluation.metrics import EvalRow, deterministic_metrics, p95
from insurance_rag.evaluation.store import build_item
from insurance_rag.ingestion.synthetic import generate
from insurance_rag.orchestrator.context import NO_ANSWER
from insurance_rag.orchestrator.quality import check_answer
from insurance_rag.orchestrator.rewrite import parse_rewrite
from insurance_rag.prompts import LocalPromptProvider, render_template
from insurance_rag.redaction import RegexRedactor


# ---- config -------------------------------------------------------------------------
def test_weights_must_sum_to_one():
    with pytest.raises(ValueError):
        Settings(keyword_weight=0.5, vector_weight=0.6)


def test_cache_namespace_changes_with_corpus_version():
    assert (
        Settings(corpus_version="v1").cache_namespace
        != Settings(corpus_version="v2").cache_namespace
    )


# ---- prompts ------------------------------------------------------------------------
def test_render_replaces_all_variables():
    assert render_template("a {{x}} b {{ y }}", {"x": "1", "y": "2"}) == "a 1 b 2"


def test_render_missing_variable_raises():
    with pytest.raises(KeyError):
        render_template("{{x}}", {})


def test_shipped_prompt_loads_and_renders():
    system, user = LocalPromptProvider().get().render(context="CTX", question="Q?")
    assert "CTX" in user and "Q?" in user and "{{" not in system + user


# ---- rewrite parsing ------------------------------------------------------------------
def test_parse_rewrite_handles_code_fences():
    raw = 'Sure!\n```json\n{"query": "proof of loss deadline", "hyde": "The insured must..."}\n```'
    r = parse_rewrite(raw, "orig")
    assert r.query == "proof of loss deadline" and r.hyde_passage.startswith("The insured")


@pytest.mark.parametrize("raw", ["not json", "{broken", '{"query": ""}', '{"hyde": "x"}'])
def test_parse_rewrite_falls_back_to_original(raw):
    assert parse_rewrite(raw, "orig").query == "orig"


# ---- quality ------------------------------------------------------------------------
def test_quality_accepts_valid_citation():
    assert check_answer("Yes [S1].", {"S1"}).ok


def test_quality_accepts_explicit_no_answer():
    assert check_answer(NO_ANSWER, {"S1"}).ok


def test_quality_flags_pii_leak():
    r = check_answer("Call 555-123-4567 [S1]", {"S1"})
    assert "pii_in_answer" in r.issues


# ---- redaction ------------------------------------------------------------------------
def test_regex_redactor_masks_identifiers_and_counts():
    text = "SSN 900-12-3456, call (555) 555-0142 or a@b.com, DOB: 3/4/1980, MRN: 123456"
    result = RegexRedactor().redact(text)
    for raw in ("900-12-3456", "555-0142", "a@b.com", "3/4/1980", "123456"):
        assert raw not in result.text
    assert {"SSN", "PHONE", "EMAIL", "DOB", "MRN"} <= set(result.counts)


def test_regex_redactor_leaves_policy_text_alone():
    text = "Proof of loss is due within 60 days; limit is $30,000 under Section VII.J.4."
    assert RegexRedactor().redact(text).text == text


def test_synthetic_claims_are_fully_redacted(tmp_path):
    files = generate(tmp_path, count=5)
    for f in files:
        out = RegexRedactor().redact(f.read_text()).text
        assert "900-" not in out and "@example.com" not in out and "MRN: 1" not in out


# ---- guardrail verdict parsing ------------------------------------------------------------
def test_any_blocked_finds_nested_block():
    assert _any_blocked([{"contentPolicy": {"filters": [{"action": "BLOCKED"}]}}])
    assert not _any_blocked(
        [{"sensitiveInformationPolicy": {"piiEntities": [{"action": "ANONYMIZED"}]}}]
    )


def test_verdict_anonymised_is_not_blocked():
    resp = {
        "action": "GUARDRAIL_INTERVENED",
        "outputs": [{"text": "my SSN is {SSN}"}],
        "assessments": [
            {"sensitiveInformationPolicy": {"piiEntities": [{"action": "ANONYMIZED"}]}}
        ],
    }
    v = BedrockGuardrails._verdict(resp, original="my SSN is 123")
    assert not v.blocked and v.text == "my SSN is {SSN}"


def test_verdict_none_passes_original_through():
    v = BedrockGuardrails._verdict({"action": "NONE"}, original="hello")
    assert not v.blocked and v.text == "hello"


# ---- opensearch bodies ------------------------------------------------------------------
def test_pipeline_weights_order_is_bm25_then_vector():
    body = pipeline_body(0.3, 0.7)
    params = body["phase_results_processors"][0]["normalization-processor"]["combination"][
        "parameters"
    ]
    assert params["weights"] == [0.3, 0.7]


def test_index_dimension_follows_settings():
    body = index_body(Settings(embed_dimensions=512))
    assert body["mappings"]["properties"]["embedding"]["dimension"] == 512
    assert body["mappings"]["dynamic"] == "strict"


# ---- evaluation ------------------------------------------------------------------------------
def _row(**kw):
    base = {
        "id": "x",
        "category": "grounded",
        "question": "q",
        "must_contain_any": ["60"],
        "answer": "60 days [S1]",
        "status": "ok",
        "has_citations": True,
        "latency_ms": 100.0,
    }
    base.update(kw)
    return EvalRow(**base)


def test_deterministic_metrics():
    rows = [
        _row(),
        _row(id="y", answer="unknown", has_citations=True),
        EvalRow(
            id="r",
            category="refusal",
            question="q",
            expect_refusal=True,
            status="no_answer",
            latency_ms=50,
        ),
    ]
    m = deterministic_metrics(rows)
    assert m["answer_contains_rate"] == 0.5
    assert m["citation_rate"] == 1.0
    assert m["refusal_accuracy"] == 1.0
    assert m["error_rate"] == 0.0


def test_errors_are_counted():
    m = deterministic_metrics([_row(), _row(id="z", error="Timeout")])
    assert m["error_rate"] == 0.5


def test_p95():
    assert p95([]) == 0.0 and p95(list(map(float, range(1, 101)))) == 95.0


T = {"min": {"faithfulness": 0.8}, "max": {"p95_latency_ms": 1000}}


def test_gate_passes_when_all_thresholds_met():
    assert evaluate_gate({"faithfulness": 0.9, "p95_latency_ms": 500}, T).passed


def test_gate_fails_below_floor_and_above_ceiling():
    r = evaluate_gate({"faithfulness": 0.7, "p95_latency_ms": 2000}, T)
    assert not r.passed and len(r.failures) == 2


def test_gate_fails_when_metric_missing():
    r = evaluate_gate({"p95_latency_ms": 10}, T)
    assert not r.passed and "missing" in r.failures[0]


def test_dynamo_item_has_no_floats_and_is_json_safe():
    item = build_item("abc123", {"faithfulness": 0.91}, True, [], {"n": 3})
    assert item["commit_sha"] == "abc123"
    from decimal import Decimal

    assert isinstance(item["scores"]["faithfulness"], Decimal)
    json.dumps(item["metadata"])

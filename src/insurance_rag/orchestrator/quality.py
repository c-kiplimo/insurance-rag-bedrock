"""Step 6: deterministic output quality checks (run in addition to Bedrock output guardrails).

Cheap, explainable rules that catch the failures an auditor cares about: an unsupported answer
(no citation), a fabricated citation (label that was never provided), or leaked identifiers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from insurance_rag.orchestrator.context import NO_ANSWER
from insurance_rag.redaction import RegexRedactor

_CITE = re.compile(r"\[(S\d+)\]")
_redactor = RegexRedactor()


@dataclass(slots=True)
class QualityResult:
    ok: bool
    issues: list[str] = field(default_factory=list)
    cited: list[str] = field(default_factory=list)


def check_answer(answer: str, valid_ids: set[str]) -> QualityResult:
    issues: list[str] = []
    text = answer.strip()
    if not text:
        return QualityResult(False, ["empty_answer"])

    cited = list(dict.fromkeys(_CITE.findall(text)))  # unique, order kept
    if text != NO_ANSWER:
        if not cited:
            issues.append("no_citation")
        unknown = [c for c in cited if c not in valid_ids]
        if unknown:
            issues.append(f"unknown_citation:{','.join(unknown)}")
    if _redactor.redact(text).counts:
        issues.append("pii_in_answer")
    return QualityResult(ok=not issues, issues=issues, cited=cited)

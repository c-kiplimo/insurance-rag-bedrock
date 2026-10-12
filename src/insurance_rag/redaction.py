"""PHI/PII redaction.

Three layers, cheapest first. They are composed with ``ChainedRedactor``:

* ``RegexRedactor``: deterministic, offline, zero cost. Catches structured identifiers
  (SSN, phone, email, MRN, card numbers, labelled DOBs). It cannot catch free-text names.
* ``ComprehendRedactor``: Amazon Comprehend PII detection (names, addresses, bank numbers...).
* ``ComprehendMedicalRedactor``: Amazon Comprehend Medical ``DetectPHI`` for clinical notes.

Redaction output uses typed placeholders ("[SSN]") so downstream text stays readable and the
embedding model never sees real identifiers. Raw values are never logged.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

import boto3


@dataclass(slots=True)
class RedactionResult:
    text: str
    counts: Counter[str] = field(default_factory=Counter)


class Redactor(Protocol):
    def redact(self, text: str) -> RedactionResult: ...


_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")),
    ("MRN", re.compile(r"\b(?:MRN|Medical Record(?: Number| No\.?)?)[:#\s]*\d{5,}\b", re.I)),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    (
        "DOB",
        re.compile(
            r"\b(?:DOB|D\.O\.B\.|Date of Birth)[:\s]*"
            r"(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|[A-Z][a-z]+ \d{1,2}, \d{4})",
            re.I,
        ),
    ),
]


class RegexRedactor:
    def redact(self, text: str) -> RedactionResult:
        counts: Counter[str] = Counter()
        for label, pattern in _PATTERNS:
            text, n = pattern.subn(f"[{label}]", text)
            if n:
                counts[label] += n
        return RedactionResult(text, counts)


def _apply_spans(text: str, spans: Sequence[tuple[int, int, str]]) -> RedactionResult:
    """Replace [start, end) spans right-to-left so earlier offsets stay valid."""
    counts: Counter[str] = Counter()
    for start, end, label in sorted(spans, key=lambda s: s[0], reverse=True):
        text = f"{text[:start]}[{label}]{text[end:]}"
        counts[label] += 1
    return RedactionResult(text, counts)


def _segments(text: str, limit: int) -> list[tuple[int, str]]:
    """Split on paragraph boundaries into (offset, segment) pieces of at most ``limit`` chars."""
    out: list[tuple[int, str]] = []
    pos = 0
    while pos < len(text):
        end = min(pos + limit, len(text))
        if end < len(text):
            cut = text.rfind("\n", pos, end)
            end = cut + 1 if cut > pos else end
        out.append((pos, text[pos:end]))
        pos = end
    return out


class ComprehendRedactor:
    """General PII via Amazon Comprehend. Names are the main thing regexes miss."""

    _TYPES: ClassVar[set[str]] = {
        "NAME",
        "ADDRESS",
        "BANK_ACCOUNT_NUMBER",
        "BANK_ROUTING",
        "DRIVER_ID",
        "PASSPORT_NUMBER",
    }

    def __init__(self, region: str, min_score: float = 0.8) -> None:
        self._client: Any = boto3.client("comprehend", region_name=region)
        self._min_score = min_score

    def redact(self, text: str) -> RedactionResult:
        spans: list[tuple[int, int, str]] = []
        for offset, segment in _segments(text, 45_000):
            resp = self._client.detect_pii_entities(Text=segment, LanguageCode="en")
            for ent in resp["Entities"]:
                if ent["Type"] in self._TYPES and ent["Score"] >= self._min_score:
                    spans.append(
                        (offset + ent["BeginOffset"], offset + ent["EndOffset"], ent["Type"])
                    )
        return _apply_spans(text, spans)


class ComprehendMedicalRedactor:
    """PHI in clinical text via Comprehend Medical. Dates are kept (date of loss matters)."""

    _TYPES: ClassVar[set[str]] = {"NAME", "ID", "ADDRESS", "CONTACT_INFORMATION", "EMAIL", "URL"}

    def __init__(self, region: str) -> None:
        self._client: Any = boto3.client("comprehendmedical", region_name=region)

    def redact(self, text: str) -> RedactionResult:
        spans: list[tuple[int, int, str]] = []
        for offset, segment in _segments(text, 18_000):  # API limit is 20k characters
            resp = self._client.detect_phi(Text=segment)
            for ent in resp["Entities"]:
                if ent["Type"] in self._TYPES:
                    spans.append(
                        (offset + ent["BeginOffset"], offset + ent["EndOffset"], ent["Type"])
                    )
        return _apply_spans(text, spans)


class ChainedRedactor:
    def __init__(self, redactors: Sequence[Redactor]) -> None:
        self._redactors = list(redactors)

    def redact(self, text: str) -> RedactionResult:
        total: Counter[str] = Counter()
        for r in self._redactors:
            result = r.redact(text)
            text = result.text
            total.update(result.counts)
        return RedactionResult(text, total)


def build_redactor(names: str, region: str) -> Redactor:
    """``names`` is a comma list from {regex, comprehend, medical}."""
    factories: dict[str, Callable[[], Redactor]] = {
        "regex": lambda: RegexRedactor(),
        "comprehend": lambda: ComprehendRedactor(region),
        "medical": lambda: ComprehendMedicalRedactor(region),
    }
    chosen = [n.strip() for n in names.split(",") if n.strip()]
    unknown = set(chosen) - set(factories)
    if unknown:
        raise ValueError(f"unknown redactor(s): {sorted(unknown)}")
    return ChainedRedactor([factories[n]() for n in chosen])

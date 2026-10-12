"""Threshold gate: pure functions, fully unit-tested.

A metric that is *missing* counts as a failure: a gate that silently passes when a scorer
broke is worse than no gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class GateResult:
    passed: bool
    failures: list[str] = field(default_factory=list)
    checked: dict[str, float] = field(default_factory=dict)


def load_thresholds(path: Path | None = None) -> dict[str, dict[str, float]]:
    if path is not None:
        data: Any = yaml.safe_load(path.read_text("utf-8"))
    else:
        text = (
            resources.files("insurance_rag.evaluation")
            .joinpath("thresholds.yaml")
            .read_text("utf-8")
        )
        data = yaml.safe_load(text)
    return {"min": data.get("min", {}), "max": data.get("max", {})}


def evaluate_gate(scores: dict[str, float], thresholds: dict[str, dict[str, float]]) -> GateResult:
    failures: list[str] = []
    checked: dict[str, float] = {}
    for metric, floor in thresholds["min"].items():
        value = scores.get(metric)
        if value is None:
            failures.append(f"{metric}: missing (required >= {floor})")
            continue
        checked[metric] = value
        if value < floor:
            failures.append(f"{metric}: {value:.3f} < {floor}")
    for metric, ceiling in thresholds["max"].items():
        value = scores.get(metric)
        if value is None:
            failures.append(f"{metric}: missing (required <= {ceiling})")
            continue
        checked[metric] = value
        if value > ceiling:
            failures.append(f"{metric}: {value:.3f} > {ceiling}")
    return GateResult(passed=not failures, failures=failures, checked=checked)

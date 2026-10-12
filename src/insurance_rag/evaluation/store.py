"""Persist evaluation runs to DynamoDB, keyed by commit SHA.

Table design (on-demand billing, so it scales to zero between pipeline runs):
    PK  commit_sha   (S)
    SK  run_id       (S)   ISO-8601 timestamp, so a commit can be re-evaluated and history kept
Access patterns: all runs for a commit, and the latest run for a commit
(Query with ScanIndexForward=False).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3


def _to_dynamo(value: Any) -> Any:
    """DynamoDB rejects Python floats; round-trip through Decimal and drop empty strings."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo(v) for v in value]
    return value


def build_item(
    commit_sha: str,
    scores: dict[str, float],
    passed: bool,
    failures: list[str],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    item: dict[str, Any] = _to_dynamo(
        {
            "commit_sha": commit_sha,
            "run_id": datetime.now(UTC).isoformat(),
            "passed": passed,
            "scores": scores,
            "failures": failures,
            "metadata": json.loads(json.dumps(metadata, default=str)),
        }
    )
    return item


def save_run(table_name: str, region: str, item: dict[str, Any]) -> None:
    boto3.resource("dynamodb", region_name=region).Table(table_name).put_item(Item=item)

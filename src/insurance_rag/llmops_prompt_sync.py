"""Push the Git-tracked prompt to Bedrock Prompt Management and cut an immutable version.

    python -m insurance_rag.llmops_prompt_sync --git-sha "$GITHUB_SHA"

Git remains the single source of truth: this script only ever *publishes* what is in the
repo. It prints ``PROMPT_ID`` and ``PROMPT_VERSION`` (and appends them to $GITHUB_OUTPUT) for
the deploy step to inject as ``RAG_PROMPT_ID`` / ``RAG_PROMPT_VERSION``. Running containers pick
the new version up on their next refresh: no image rebuild.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any

import boto3

from insurance_rag.config import get_settings
from insurance_rag.prompts import load_local_prompt

_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def build_variant(spec: dict[str, Any], model_id: str) -> dict[str, Any]:
    names = sorted(set(_VAR.findall(spec["system"] + spec["user"])))
    params = spec.get("model_params", {})
    return {
        "name": "default",
        "templateType": "CHAT",
        "modelId": model_id,
        "templateConfiguration": {
            "chat": {
                "system": [{"text": spec["system"]}],
                "messages": [{"role": "user", "content": [{"text": spec["user"]}]}],
                "inputVariables": [{"name": n} for n in names],
            }
        },
        "inferenceConfiguration": {
            "text": {
                "temperature": float(params.get("temperature", 0.0)),
                "maxTokens": int(params.get("max_tokens", 800)),
            }
        },
    }


def find_prompt_id(client: Any, name: str) -> str | None:
    for page in client.get_paginator("list_prompts").paginate():
        for summary in page.get("promptSummaries", []):
            if summary["name"] == name:
                return str(summary["id"])
    return None


def sync(git_sha: str) -> tuple[str, str]:
    settings = get_settings()
    client = boto3.client("bedrock-agent", region_name=settings.aws_region)
    spec = load_local_prompt("rag_answer.yaml")
    variant = build_variant(spec, settings.llm_model_id)

    prompt_id = find_prompt_id(client, spec["name"])
    if prompt_id is None:
        created = client.create_prompt(
            name=spec["name"],
            description=spec.get("description", ""),
            variants=[variant],
            defaultVariant="default",
        )
        prompt_id = created["id"]
    else:
        client.update_prompt(
            promptIdentifier=prompt_id,
            name=spec["name"],
            description=spec.get("description", ""),
            variants=[variant],
            defaultVariant="default",
        )
    version = client.create_prompt_version(
        promptIdentifier=prompt_id, description=f"git:{git_sha}"
    )["version"]
    return str(prompt_id), str(version)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--git-sha", default=os.getenv("GITHUB_SHA", "local"))
    args = ap.parse_args()
    prompt_id, version = sync(args.git_sha)
    print(f"PROMPT_ID={prompt_id}\nPROMPT_VERSION={version}")
    gh_out = os.getenv("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"prompt_id={prompt_id}\nprompt_version={version}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Prompt loading.

Git is the single source of truth (``prompts/*.yaml``). CI pushes validated prompts to
Bedrock Prompt Management and produces an immutable *version*. At runtime the service pulls
that version, so a prompt change ships without rebuilding the container.

If Prompt Management is unreachable the service falls back to the copy baked into the image
(the last prompt that shipped with this build) rather than failing requests.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from importlib import resources
from typing import Any

import boto3
import yaml

from insurance_rag.config import Settings
from insurance_rag.ports import PromptProvider, PromptTemplate

log = logging.getLogger(__name__)
_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render_template(template: str, variables: dict[str, str]) -> str:
    """Replace ``{{name}}`` placeholders. Raises on a missing variable (no silent blanks)."""

    def sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise KeyError(f"prompt variable '{key}' was not provided")
        return variables[key]

    return _VAR.sub(sub, template)


def load_local_prompt(filename: str) -> dict[str, Any]:
    text = resources.files("insurance_rag.prompts_data").joinpath(filename).read_text("utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{filename} must contain a mapping")
    return data


class LocalPromptProvider:
    def __init__(self, filename: str = "rag_answer.yaml") -> None:
        data = load_local_prompt(filename)
        params = data.get("model_params", {})
        self._template = PromptTemplate(
            name=data["name"],
            version="local",
            system=data["system"],
            user=data["user"],
            temperature=float(params.get("temperature", 0.0)),
            max_tokens=int(params.get("max_tokens", 800)),
        )

    def get(self) -> PromptTemplate:
        return self._template


class BedrockPromptProvider:
    """Fetch a CHAT prompt version from Bedrock Prompt Management, cached with a TTL."""

    def __init__(self, settings: Settings, fallback: PromptProvider) -> None:
        self._client: Any = boto3.client("bedrock-agent", region_name=settings.aws_region)
        self._id = settings.prompt_id
        self._version = settings.prompt_version
        self._ttl = settings.prompt_refresh_seconds
        self._fallback = fallback
        self._lock = threading.Lock()
        self._cached: PromptTemplate | None = None
        self._fetched_at = 0.0

    def get(self) -> PromptTemplate:
        now = time.monotonic()
        if self._cached is not None and now - self._fetched_at < self._ttl:
            return self._cached
        with self._lock:
            if self._cached is not None and now - self._fetched_at < self._ttl:
                return self._cached
            try:
                self._cached = self._fetch()
                self._fetched_at = time.monotonic()
            except Exception:
                log.exception("prompt management fetch failed; using last known/baked prompt")
                if self._cached is None:
                    return self._fallback.get()
                self._fetched_at = time.monotonic() - self._ttl + 30  # retry in 30s
            return self._cached

    def _fetch(self) -> PromptTemplate:
        kwargs: dict[str, Any] = {"promptIdentifier": self._id}
        if self._version:
            kwargs["promptVersion"] = self._version
        resp = self._client.get_prompt(**kwargs)
        variant = resp["variants"][0]
        chat = variant["templateConfiguration"]["chat"]
        system = "\n".join(b["text"] for b in chat.get("system", []) if "text" in b)
        user = next(m["content"][0]["text"] for m in chat["messages"] if m["role"] == "user")
        text_cfg = variant.get("inferenceConfiguration", {}).get("text", {})
        return PromptTemplate(
            name=resp["name"],
            version=str(resp.get("version", "DRAFT")),
            system=system,
            user=user,
            temperature=float(text_cfg.get("temperature", 0.0)),
            max_tokens=int(text_cfg.get("maxTokens", 800)),
        )


def build_prompt_provider(settings: Settings) -> PromptProvider:
    local = LocalPromptProvider()
    if settings.prompt_id:
        return BedrockPromptProvider(settings, fallback=local)
    return local

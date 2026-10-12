"""Optional Langfuse tracing.

Design goals:
* The app runs identically with tracing off (local dev, unit tests, CI).
* A tracing failure must never fail a user request.
* PHI/PII must not reach Langfuse: a ``mask`` function redacts every payload before it
  leaves the process.

Langfuse v3 reads ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` / ``LANGFUSE_HOST``.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any, TypeVar

from insurance_rag.redaction import RegexRedactor

log = logging.getLogger(__name__)
F = TypeVar("F", bound=Callable[..., Any])

_enabled = False
_client: Any = None
_redactor = RegexRedactor()


def _mask(data: Any, **_: Any) -> Any:
    if isinstance(data, str):
        return _redactor.redact(data).text
    if isinstance(data, dict):
        return {k: _mask(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_mask(v) for v in data]
    return data


def init_tracing(enabled: bool) -> None:
    global _enabled, _client
    if not enabled:
        return
    try:
        from langfuse import Langfuse

        _client = Langfuse(mask=_mask)
        _enabled = True
        log.info("langfuse tracing enabled")
    except Exception:
        log.exception("langfuse init failed; continuing without tracing")


def shutdown_tracing() -> None:
    if _enabled and _client is not None:
        try:
            _client.flush()
        except Exception:
            log.exception("langfuse flush failed")


def traced(name: str, *, as_type: str = "span") -> Callable[[F], F]:
    """Wrap a function in a Langfuse observation when tracing is enabled."""

    def decorator(fn: F) -> F:
        wrapped: Callable[..., Any] | None = None

        @functools.wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> Any:
            nonlocal wrapped
            if not _enabled:
                return fn(*args, **kwargs)
            if wrapped is None:
                from langfuse import observe

                deco: Any = observe
                wrapped = deco(
                    name=name, as_type=as_type, capture_input=False, capture_output=False
                )(fn)
            return wrapped(*args, **kwargs)

        return inner  # type: ignore[return-value]

    return decorator


def record(**fields: Any) -> None:
    """Attach input/output/metadata to the current observation. Never raises."""
    if not _enabled or _client is None:
        return
    try:
        _client.update_current_span(**fields)
    except Exception:
        log.debug("langfuse record failed", exc_info=True)


def record_generation(**fields: Any) -> None:
    """Attach model, usage_details, input, output to the current generation."""
    if not _enabled or _client is None:
        return
    try:
        _client.update_current_generation(**fields)
    except Exception:
        log.debug("langfuse generation record failed", exc_info=True)


def current_trace_id() -> str | None:
    if not _enabled or _client is None:
        return None
    try:
        return _client.get_current_trace_id()  # type: ignore[no-any-return]
    except Exception:
        return None

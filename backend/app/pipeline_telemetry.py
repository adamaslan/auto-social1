"""Structured telemetry helpers for the AI news pipeline.

Wraps record_event with per-step timing, LLM call recording, and external
HTTP call recording. All events use a normalized JSON shape compatible with
Cloud Logging (timestamp, severity, jsonPayload).
"""

import functools
import time
from contextvars import ContextVar
from typing import Any, Callable, Awaitable
import logging

from .runtime import APP_NAME, record_event

logger = logging.getLogger(APP_NAME)

# Propagate run_id through async call stacks without explicit threading.
_current_run_id: ContextVar[str] = ContextVar("current_run_id", default="")


def set_run_id(run_id: str) -> None:
    _current_run_id.set(run_id)


def get_run_id() -> str:
    return _current_run_id.get()


def timed_step(name: str):
    """Decorator that records step_started / step_completed / step_failed events."""
    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            run_id = get_run_id()
            started = time.perf_counter()
            record_event("step_started", run_id=run_id, step=name)
            try:
                result = await fn(*args, **kwargs)
                duration_ms = round((time.perf_counter() - started) * 1000)
                record_event("step_completed", run_id=run_id, step=name, duration_ms=duration_ms)
                return result
            except Exception as exc:
                duration_ms = round((time.perf_counter() - started) * 1000)
                record_event(
                    "step_failed",
                    level="error",
                    run_id=run_id,
                    step=name,
                    duration_ms=duration_ms,
                    error_class=type(exc).__name__,
                    error=str(exc),
                )
                raise
        return wrapper
    return decorator


def record_llm_call(
    *,
    run_id: str,
    purpose: str,
    provider: str,
    model: str,
    prompt: str,
    response: str,
    duration_ms: int,
    attempt: int = 1,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> None:
    record_event(
        "ai_news_llm_call",
        run_id=run_id,
        purpose=purpose,
        provider=provider,
        model=model,
        attempt=attempt,
        duration_ms=duration_ms,
        prompt_chars=len(prompt),
        response_chars=len(response),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def record_external_call(
    *,
    run_id: str,
    host: str,
    path: str,
    method: str = "GET",
    status_code: int,
    duration_ms: int,
    bytes_received: int = 0,
) -> None:
    record_event(
        "ai_news_external_call",
        run_id=run_id,
        host=host,
        path=path,
        method=method,
        status_code=status_code,
        duration_ms=duration_ms,
        bytes_received=bytes_received,
    )

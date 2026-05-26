import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .config import load_env_files


def _artifact_store():
    # Lazy import to avoid circular dependency (artifact_store imports from runtime).
    from .artifact_store import default_store
    return default_store


APP_NAME = "social-pr-autopilot"
load_env_files()
STARTED_AT = time.time()
RUNS: dict[str, dict[str, Any]] = {}
EVENTS: list[dict[str, Any]] = []
PUBLISH_LOGS: dict[str, dict[str, Any]] = {}
CHANNEL_ATTEMPTS: dict[str, list[float]] = {}
STORIES_SEEN: dict[str, float] = {}


def _parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    return int(raw) if raw.isdigit() else default


MAX_EVENTS = _parse_int_env("MAX_DEBUG_EVENTS", 200)
MAX_RUNS = _parse_int_env("MAX_RUNS", 500)
MAX_PUBLISH_LOGS = _parse_int_env("MAX_PUBLISH_LOGS", 500)
MAX_STORIES_SEEN = _parse_int_env("MAX_STORIES_SEEN", 500)
logger = logging.getLogger(APP_NAME)


def allowed_origins() -> list[str]:
    raw = os.getenv("ALLOWED_ORIGINS", "*")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _evict_oldest(store: dict[str, dict[str, Any]], max_size: int) -> None:
    """Remove oldest entries when the store exceeds max_size."""
    if len(store) >= max_size:
        oldest_keys = sorted(store, key=lambda k: store[k].get("created_at", ""))[:max(1, len(store) - max_size + 1)]
        for k in oldest_keys:
            del store[k]


def start_run(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    _evict_oldest(RUNS, MAX_RUNS)
    run_id = str(uuid.uuid4())
    created_at = now_iso()
    record = {
        "id": run_id,
        "app": APP_NAME,
        "kind": kind,
        "status": "running",
        "created_at": created_at,
        "updated_at": created_at,
        "input_preview": _json_preview(payload, 500),
        "summary": "",
        "error": "",
    }
    RUNS[run_id] = record
    record_event("agent_run_started", run_id=run_id, kind=kind)
    if kind == "ai_news_pipeline":
        _artifact_store().write(run_id, "manifest.json", {
            "run_id": run_id,
            "kind": kind,
            "status": "started",
            "created_at": created_at,
            "completed_at": None,
            "duration_ms": None,
        })
    return record


def finish_run(run_id: str, status: str, summary: str, error: str = "") -> dict[str, Any]:
    record = RUNS[run_id]
    completed_at = now_iso()
    created_at = record.get("created_at", "")
    record.update({
        "status": status,
        "summary": summary[:500],
        "error": error[:500],
        "updated_at": completed_at,
    })
    record_event("agent_run_finished", level="error" if status == "failed" else "info", run_id=run_id, kind=record["kind"], status=status, error=error)
    if record.get("kind") == "ai_news_pipeline":
        existing = _artifact_store().read(run_id, "manifest.json") or {}
        existing.update({
            "status": status,
            "completed_at": completed_at,
            "summary": summary[:500],
            "error": error[:500],
        })
        _artifact_store().write(run_id, "manifest.json", existing)
    return record


def list_runs(limit: int = 25) -> list[dict[str, Any]]:
    return sorted(RUNS.values(), key=lambda run: run["created_at"], reverse=True)[:limit]


def get_run(run_id: str) -> dict[str, Any] | None:
    return RUNS.get(run_id)


def uptime_seconds() -> int:
    return int(time.time() - STARTED_AT)


def record_event(event: str, level: str = "info", **fields: Any) -> None:
    payload = {"ts": now_iso(), "event": event, "app": APP_NAME, **fields}
    EVENTS.append(payload)
    if len(EVENTS) > MAX_EVENTS:
        del EVENTS[: len(EVENTS) - MAX_EVENTS]
    log_level = logging.ERROR if level == "error" else logging.WARNING if level == "warning" else logging.INFO
    logger.log(log_level, event, extra={key: value for key, value in payload.items() if key != "ts"})


def recent_events(limit: int = 50) -> list[dict[str, Any]]:
    return EVENTS[-limit:]


def debug_snapshot() -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    for run in RUNS.values():
        status_counts[run["status"]] = status_counts.get(run["status"], 0) + 1
    return {
        "app": APP_NAME,
        "uptime_seconds": uptime_seconds(),
        "run_count": len(RUNS),
        "run_status_counts": status_counts,
        "recent_runs": list_runs(limit=10),
        "recent_events": recent_events(limit=25),
        "publish_log_count": len(PUBLISH_LOGS),
        "stories_seen_count": len(STORIES_SEEN),
    }


def story_already_seen(url: str) -> bool:
    return url in STORIES_SEEN


def mark_story_seen(url: str) -> None:
    if not url:
        return
    if len(STORIES_SEEN) >= MAX_STORIES_SEEN:
        oldest = sorted(STORIES_SEEN, key=STORIES_SEEN.get)[: max(1, len(STORIES_SEEN) - MAX_STORIES_SEEN + 1)]
        for key in oldest:
            del STORIES_SEEN[key]
    STORIES_SEEN[url] = time.time()


def channel_limit(channel: str) -> tuple[int, int]:
    raw = os.getenv(f"{channel.upper()}_RATE_LIMIT", "10/3600")
    try:
        count, window = raw.split("/", 1)
        max_count = int(count)
        window_seconds = int(window)
        if max_count < 1 or window_seconds < 1:
            raise ValueError("rate limit values must be positive")
        return max_count, window_seconds
    except (AttributeError, ValueError):
        logger.warning(
            "invalid_rate_limit",
            extra={
                "app": APP_NAME,
                "event": "invalid_rate_limit",
                "channel": channel,
                "config_value": raw,
                "default_value": "10/3600",
            },
        )
        return 10, 3600


def check_rate_limit(channel: str) -> tuple[bool, str]:
    max_count, window_seconds = channel_limit(channel)
    now = time.time()
    attempts = [ts for ts in CHANNEL_ATTEMPTS.get(channel, []) if now - ts < window_seconds]
    CHANNEL_ATTEMPTS[channel] = attempts
    if len(attempts) >= max_count:
        return False, f"{channel} rate limit reached: {len(attempts)}/{max_count} in {window_seconds}s"
    attempts.append(now)
    CHANNEL_ATTEMPTS[channel] = attempts
    return True, f"{len(attempts)}/{max_count} attempts in active window"


def create_publish_log(channel: str, payload: dict[str, Any], dry_run: bool, diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
    _evict_oldest(PUBLISH_LOGS, MAX_PUBLISH_LOGS)
    log_id = str(uuid.uuid4())
    log = {
        "id": log_id,
        "channel": channel,
        "status": "pending",
        "dry_run": dry_run,
        "attempts": 0,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "payload": payload,
        "payload_preview": _json_preview(payload, 700),
        "external_id": "",
        "error": "",
        "retryable": False,
        "next_action": "",
        "diagnostics": diagnostics or {},
        "response_preview": "",
    }
    PUBLISH_LOGS[log_id] = log
    record_event("publish_log_created", channel=channel, publish_log_id=log_id, dry_run=dry_run)
    return log


def update_publish_log(
    log_id: str,
    status: str,
    *,
    external_id: str = "",
    error: str = "",
    retryable: bool = False,
    next_action: str = "",
    response_preview: str = "",
) -> dict[str, Any]:
    log = PUBLISH_LOGS[log_id]
    log["attempts"] += 1
    log["status"] = status
    log["external_id"] = external_id
    log["error"] = error[:700]
    log["retryable"] = retryable
    log["next_action"] = next_action[:700]
    log["response_preview"] = response_preview[:1200]
    log["updated_at"] = now_iso()
    record_event(
        "publish_attempt_finished",
        level="error" if status == "failed" else "info",
        channel=log["channel"],
        publish_log_id=log_id,
        status=status,
        error=error,
    )
    return log


def list_publish_logs(limit: int = 50) -> list[dict[str, Any]]:
    return sorted(PUBLISH_LOGS.values(), key=lambda item: item["created_at"], reverse=True)[:limit]


def get_publish_log(log_id: str) -> dict[str, Any] | None:
    return PUBLISH_LOGS.get(log_id)


def _json_preview(payload: dict[str, Any], limit: int) -> str:
    return json.dumps(payload, default=str, sort_keys=True)[:limit]

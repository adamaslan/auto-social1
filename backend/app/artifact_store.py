"""Artifact store: write and read per-run JSON files to var/runs/<run_id>/.

Local implementation writes to the filesystem. The interface is narrow enough
that a GCS implementation is a drop-in swap — only this module changes.
"""

import dataclasses
import json
import logging
from pathlib import Path
from typing import Any, Protocol

from .runtime import APP_NAME, record_event

logger = logging.getLogger(APP_NAME)

# var/ mirrors FHS convention and is git-ignored.
_VAR_DIR = Path(__file__).parent.parent / "var" / "runs"

ALLOWED_ARTIFACT_NAMES = frozenset({
    "manifest.json",
    "candidates.json",
    "llm-article.json",
    "llm-caption.json",
    "llm-picker.json",
    "image-request.json",
    "publish-response.json",
})


class ArtifactStore(Protocol):
    def write(self, run_id: str, name: str, data: Any) -> None: ...
    def read(self, run_id: str, name: str) -> dict[str, Any] | None: ...
    def run_dir(self, run_id: str) -> Path | None: ...


class LocalArtifactStore:
    def __init__(self, base_dir: Path = _VAR_DIR) -> None:
        self._base = base_dir

    def _path(self, run_id: str, name: str) -> Path:
        if name not in ALLOWED_ARTIFACT_NAMES:
            raise ValueError(f"Artifact name not allowed: {name!r}")
        resolved_base = self._base.resolve()
        candidate = (self._base / run_id / name).resolve()
        if not candidate.is_relative_to(resolved_base):
            raise ValueError(f"Path traversal detected for run_id={run_id!r}")
        return candidate

    def write(self, run_id: str, name: str, data: Any) -> None:
        """Write data as JSON. Best-effort — never raises to the caller."""
        try:
            path = self._path(run_id, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            serialized = json.dumps(_serialize(data), indent=2, ensure_ascii=False)
            path.write_text(serialized, encoding="utf-8")
            record_event(
                "ai_news_artifact_written",
                run_id=run_id,
                file=name,
                bytes=len(serialized.encode()),
            )
        except Exception as exc:
            logger.warning("artifact_write_failed run_id=%s name=%s error=%s", run_id, name, exc)

    def read(self, run_id: str, name: str) -> dict[str, Any] | None:
        path = self._path(run_id, name)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def run_dir(self, run_id: str) -> Path | None:
        try:
            resolved_base = self._base.resolve()
            d = (self._base / run_id).resolve()
            if not d.is_relative_to(resolved_base):
                return None
            return d if d.exists() else None
        except Exception:
            return None


def _serialize(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(i) for i in obj]
    return obj


# Module-level singleton — swap for a GCS impl by reassigning this.
default_store: ArtifactStore = LocalArtifactStore()

# Project Rules — Social PR Autopilot

## Security: Path Traversal Prevention

Any user-controlled string (run_id, filename, etc.) used to build a filesystem path **must** be validated before use:

```python
resolved_base = self._base.resolve()
candidate = (self._base / user_input).resolve()
if not candidate.is_relative_to(resolved_base):
    raise ValueError("Path traversal detected")
```

Never use `self._base / user_input` directly without this check.

## Security: Artifact Name Allowlist

Only names in `ALLOWED_ARTIFACT_NAMES` may be written/read from the artifact store. Validate before constructing the path.

## Pillow Compatibility (Pillow 10+)

Always use `Image.Resampling.LANCZOS` — the top-level `Image.LANCZOS` constant was removed in Pillow 10.0.0. The project pins `Pillow==12.x`.

## Pillow: Palette/Mode Images

Before passing an image to `ImageStat.Stat`, convert it to a known mode:

```python
rgb = im.convert("RGB") if im.mode not in ("RGB", "RGBA", "L") else im
stat = ImageStat.Stat(rgb)
```

Palette-mode (`P`) images will produce wrong statistics otherwise.

## HTTP Client: Reuse ai_client()

Never instantiate `httpx.AsyncClient` per-request in the pipeline. Use the shared `ai_client()` singleton from `.http_clients`:

```python
resp = await ai_client().get(url, timeout=15)
```

Per-request clients cause socket exhaustion under load.

## No Loopback HTTP Inside the Process

Do not make HTTP requests to `127.0.0.1` from within the FastAPI app to call its own endpoints. Call the underlying function directly instead:

```python
# Wrong
resp = await client.post("http://127.0.0.1:8102/api/publish", json=payload)

# Right
from .channel_adapters import publish
result = await publish(PublishRequest(...))
```

Loopback calls fail in Docker/Cloud Run where the internal port differs from the host-mapped port.

## Cross-Platform File Replacement

Use `Path.replace()` instead of `Path.rename()` when the destination may already exist. `rename()` raises `FileExistsError` on Windows; `replace()` is atomic on both Unix and Windows:

```python
current_path.replace(final_path)  # correct
current_path.rename(final_path)   # breaks on Windows if final_path exists
```

## XML Parsing: Use Bytes, Not Text

Pass `resp.content` (bytes) to `ET.fromstring()`, not `resp.text` (str). If the XML contains an encoding declaration (`<?xml ... encoding="utf-8"?>`), passing a decoded string causes a conflict:

```python
root = ET.fromstring(resp.content)  # correct
root = ET.fromstring(resp.text)     # encoding conflict on declared encodings
```

## Exception Handling: Don't Assume Exception Hierarchy

When catching exceptions in fallback/safety blocks, verify that all expected exception types are actually subclasses of what you catch. For example, `binascii.Error` is **not** a subclass of `ValueError` — use `except Exception` in pure-fallback cases where any failure should be swallowed.

## Subdirectory Paths: Preserve Relative Structure

When returning a filename derived from a path inside a base directory, use `path.relative_to(base).as_posix()` rather than `path.name`. `path.name` silently drops any subdirectory component, causing 404s for paths like `sub/hero.jpg`.

## Guard Against Empty Collections Before Indexing

Before accessing `list[0]` (e.g., fallback picks), guard with `if not list: raise ...`. An empty list passed to a picker will produce an `IndexError` instead of a clear error message.

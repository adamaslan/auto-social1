import os
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError

# Configurable via env so Cloud Run / Docker can mount a volume at a different path.
# Defaults to frontend/public/ relative to the repo root for local development.
_DEFAULT_MEDIA_DIR = Path(__file__).resolve().parents[2] / "frontend" / "public"
MEDIA_DIR = Path(os.getenv("MEDIA_STORAGE_PATH", str(_DEFAULT_MEDIA_DIR))).resolve()

# Meta requires hero images to be at least 1080px on the short side. Image
# generators (Gemini Imagen, Pollinations) sometimes return 768x768 despite
# the requested size — upscale here so validate_hero_image() passes without
# manual PIL intervention. See docs/runs/nuart-20260527-105928.md Bug 3.
_MIN_DIMENSION = 1080


def local_path_to_public_jpeg(local_path: str) -> str:
    """Convert a local image file to JPEG in MEDIA_DIR and return its filename.

    Accepts only filenames relative to MEDIA_DIR — absolute paths and
    path traversal sequences (../) are rejected.
    Returns the JPEG filename (not a URL — the caller builds the URL).

    Also enforces Meta's minimum 1080px short-side requirement: images
    smaller than 1080 on either axis are upscaled with LANCZOS before
    saving. Aspect-ratio validation still raises ValueError on out-of-range
    images before any pixel work is done.
    """
    # Resolve relative to MEDIA_DIR only — never accept absolute paths from callers.
    src = (MEDIA_DIR / local_path).resolve()

    # Guard against path traversal (e.g. "../../../etc/passwd")
    if not src.is_relative_to(MEDIA_DIR):
        raise ValueError(f"Invalid image path: access outside media directory is not allowed")

    if not src.exists():
        raise FileNotFoundError(f"Image not found: {src.name}")

    # If already a JPEG in MEDIA_DIR, upscale-in-place if needed (atomic) and return.
    if src.suffix.lower() in (".jpg", ".jpeg"):
        _ensure_min_dimension_in_place(src)
        return src.name

    # Convert to JPEG (and upscale if needed)
    dest_name = f"{uuid.uuid4().hex}.jpg"
    dest = MEDIA_DIR / dest_name

    try:
        with Image.open(src) as img:
            rgb = img.convert("RGB")
            _validate_dimensions(rgb)
            rgb = _upscale_if_undersized(rgb)
            rgb.save(dest, "JPEG", quality=92, optimize=True)
    except UnidentifiedImageError:
        raise ValueError(f"File is not a recognised image format: {src.name}")

    return dest_name


def _upscale_if_undersized(img: Image.Image) -> Image.Image:
    """Return img (or a LANCZOS-upscaled copy) such that min(w, h) >= 1080."""
    w, h = img.size
    if w >= _MIN_DIMENSION and h >= _MIN_DIMENSION:
        return img
    scale = _MIN_DIMENSION / min(w, h)
    new_size = (max(_MIN_DIMENSION, int(round(w * scale))), max(_MIN_DIMENSION, int(round(h * scale))))
    return img.resize(new_size, Image.LANCZOS)


def _ensure_min_dimension_in_place(path: Path) -> None:
    """If path is below the min dimension, atomically replace it with an upscaled JPEG.

    Writes to a sibling temp file first and renames, so a concurrent fetch
    of the file (e.g. Meta downloading via the tunnel) never sees a partial
    write. If the image already meets the size requirement, this is a no-op.
    """
    try:
        with Image.open(path) as img:
            w, h = img.size
            if w >= _MIN_DIMENSION and h >= _MIN_DIMENSION:
                # Validate aspect ratio so callers still get a hard error if it's wrong.
                _validate_dimensions(img)
                return
            rgb = img.convert("RGB")
            _validate_dimensions(rgb)
            rgb = _upscale_if_undersized(rgb)
    except UnidentifiedImageError:
        raise ValueError(f"File is not a recognised image format: {path.name}")

    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        rgb.save(tmp_path, "JPEG", quality=92, optimize=True)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _validate_dimensions(img: Image.Image) -> None:
    """Raise ValueError if the image falls outside Meta's supported aspect ratio range."""
    w, h = img.size
    if h == 0:
        raise ValueError("Image has zero height")
    ratio = w / h
    # Meta requires 4:5 (0.8) to 1.91:1 (1.91)
    if ratio < 0.8 or ratio > 1.91:
        raise ValueError(
            f"Aspect ratio {ratio:.2f} is outside Meta's supported range (4:5 to 1.91:1). "
            f"Image is {w}x{h}px."
        )


def media_dir() -> Path:
    return MEDIA_DIR

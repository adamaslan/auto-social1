"""Hero image validation after generation.

Runs a Pillow inspection to catch blank/uniform images (blue-gradient incident),
wrong aspect ratios, and undersized dimensions before they get published to Instagram.
"""

from pathlib import Path

from PIL import Image, ImageStat

from .models import ImageValidation

# IG feed allows 4:5 (0.8) to 1.91:1. Pipeline targets 1:1.
_IG_RATIO_MIN = 0.8
_IG_RATIO_MAX = 1.91
_IG_MIN_DIMENSION = 1080

# Images where every channel stddev is below this threshold are considered blank.
_BLANK_STDDEV_THRESHOLD = 5.0


def validate_hero_image(path: Path) -> ImageValidation:
    with Image.open(path) as im:
        width, height = im.size
        fmt = im.format or path.suffix.lstrip(".").upper()
        stat = ImageStat.Stat(im)
        mean_color = list(stat.mean)
        color_stddev = list(stat.stddev)

    ratio = width / height
    aspect_ok = _IG_RATIO_MIN <= ratio <= _IG_RATIO_MAX
    dimension_ok = width >= _IG_MIN_DIMENSION
    is_likely_blank = all(s < _BLANK_STDDEV_THRESHOLD for s in color_stddev)

    return ImageValidation(
        width=width,
        height=height,
        ratio=ratio,
        format=fmt,
        aspect_ok=aspect_ok,
        dimension_ok=dimension_ok,
        is_likely_blank=is_likely_blank,
        mean_color=mean_color,
        color_stddev=color_stddev,
    )

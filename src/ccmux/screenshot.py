"""Convert terminal text to a PNG image."""

from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Bundled font: Noto Sans Mono CJK SC (OFL-1.1)
_FONT_PATH = Path(__file__).parent / "fonts" / "NotoSansMonoCJKsc-Regular.otf"


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load the bundled monospace font at the given size."""
    try:
        return ImageFont.truetype(str(_FONT_PATH), size)
    except OSError:
        logger.warning("Failed to load bundled font %s, using Pillow default", _FONT_PATH)
        return ImageFont.load_default()


def text_to_image(text: str, font_size: int = 28) -> bytes:
    """Render monospace text onto a dark-background image and return PNG bytes."""
    font = _load_font(font_size)

    lines = text.split("\n")
    padding = 16

    # Measure text size
    dummy = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(dummy)
    line_height = int(font_size * 1.4)
    max_width = 0
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        max_width = max(max_width, bbox[2] - bbox[0])

    img_width = int(max_width) + padding * 2
    img_height = line_height * len(lines) + padding * 2

    bg_color = (30, 30, 30)
    fg_color = (212, 212, 212)

    img = Image.new("RGB", (img_width, img_height), bg_color)
    draw = ImageDraw.Draw(img)

    y = padding
    for line in lines:
        draw.text((padding, y), line, fill=fg_color, font=font)
        y += line_height

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

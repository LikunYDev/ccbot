"""Convert terminal text to a PNG image."""

from __future__ import annotations

import io
import logging
import os

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Font candidates ordered by priority:
#   1. CJK monospace fonts (best: mono + CJK support)
#   2. CJK proportional fonts (fallback: CJK support but not mono)
#   3. Latin monospace fonts (fallback: mono but no CJK)
_FONT_CANDIDATES: list[str] = [
    # --- CJK Monospace ---
    # Sarasa Mono (open-source CJK mono, popular choice)
    "/usr/share/fonts/sarasa-gothic/SarasaMonoSC-Regular.ttf",
    "/usr/share/fonts/truetype/sarasa/SarasaMonoSC-Regular.ttf",
    # Noto Sans Mono CJK
    "/usr/share/fonts/opentype/noto/NotoSansMonoCJKsc-Regular.otf",
    "/usr/share/fonts/noto-cjk/NotoSansMonoCJKsc-Regular.otf",  # Fedora/Arch
    "/usr/share/fonts/google-noto-sans-mono-cjk-ttc/NotoSansMonoCJK-Regular.ttc",
    # WenQuanYi Mono
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc",  # Fedora
    # macOS CJK mono
    "/System/Library/Fonts/PingFang.ttc",
    "/Library/Fonts/Sarasa Mono SC Regular.ttf",

    # --- CJK Proportional (still better than no CJK) ---
    # Noto Sans CJK
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/noto-cjk/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/google-noto-sans-cjk-ttc/NotoSansCJK-Regular.ttc",
    # Source Han Sans
    "/usr/share/fonts/adobe-source-han-sans/SourceHanSansSC-Regular.otf",
    "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
    # macOS CJK
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",

    # --- Latin Monospace (no CJK support) ---
    # Linux - DejaVu
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",  # Fedora/RHEL
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",  # Arch
    # Linux - Liberation Mono
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/liberation-mono/LiberationMono-Regular.ttf",
    # Linux - Ubuntu Mono
    "/usr/share/fonts/truetype/ubuntu/UbuntuMono-R.ttf",
    # Linux - Noto Mono (Latin only)
    "/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf",
    # macOS Latin mono
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/SFMono-Regular.otf",
    "/System/Library/Fonts/Monaco.ttf",
    "/Library/Fonts/Courier New.ttf",
    "/System/Library/Fonts/Supplemental/Courier New.ttf",
]

# Detected font path (resolved once at import time)
_MONO_FONT_PATH: str | None = None

for _path in _FONT_CANDIDATES:
    if os.path.isfile(_path):
        _MONO_FONT_PATH = _path
        break

if _MONO_FONT_PATH:
    logger.info("Using font: %s", _MONO_FONT_PATH)
else:
    logger.warning("No suitable font found, falling back to Pillow default")


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load the detected monospace font at the given size."""
    if _MONO_FONT_PATH:
        try:
            return ImageFont.truetype(_MONO_FONT_PATH, size)
        except (OSError, IOError):
            pass
    return ImageFont.load_default()


def text_to_image(text: str, font_size: int = 14) -> bytes:
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

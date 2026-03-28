"""
Emoji-aware text rendering for Pillow.

Detects emoji characters in text and renders them using NotoColorEmoji-Regular.ttf,
while rendering all other characters with the caller-supplied font.
"""

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw, ImageFont

from astrbot.api import logger

# Emoji font path (bundled in plugin fonts directory)
_EMOJI_FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "NotoColorEmoji-Regular.ttf")

# Pre-compiled regex that matches most emoji (single codepoints, sequences, ZWJ sequences, etc.)
# Covers: Emoji_Presentation, Emoji_Modifier_Base + modifiers, flag sequences, keycap sequences, ZWJ sequences
_EMOJI_RE = re.compile(
    "("
    "[\U0001F1E0-\U0001F1FF]{2}"                 # flags (regional indicators)
    "|[\U0001F3FB-\U0001F3FF]"                     # skin tone modifiers
    "|[\U0001F600-\U0001F64F]"                     # emoticons
    "|[\U0001F680-\U0001F6FF]"                     # transport & map
    "|[\U0001F700-\U0001F77F]"                     # alchemical
    "|[\U0001F780-\U0001F7FF]"                     # geometric shapes ext
    "|[\U0001F800-\U0001F8FF]"                     # supplemental arrows-C
    "|[\U0001F900-\U0001F9FF]"                     # supplemental symbols
    "|[\U0001FA00-\U0001FA6F]"                     # chess symbols
    "|[\U0001FA70-\U0001FAFF]"                     # symbols ext-A
    "|[\U00002702-\U000027B0]"                     # dingbats
    "|[\U0000FE00-\U0000FE0F]"                     # variation selectors
    "|[\U0000200D]"                                 # ZWJ
    "|[\U000020E3]"                                 # combining enclosing keycap
    "|[\U00002600-\U000026FF]"                     # misc symbols
    "|[\U00002300-\U000023FF]"                     # misc technical
    "|[\U00002B50\U00002B55\U000023F0\U000023F3]"  # common standalone
    "|[\U0000203C\U00002049]"                      # exclamation marks
    "|[\U0000231A\U0000231B]"                      # watch, hourglass
    "|[\U000025AA-\U000025FE]"                     # geometric shapes
    "|[\U00002934\U00002935]"                      # arrows
    "|[\U00003030\U0000303D]"                      # CJK symbols
    "|[\U00003297\U00003299]"                      # circled ideographs
    "|[\U0001F000-\U0001F02F]"                     # mahjong, domino
    "|[\U0001F0A0-\U0001F0FF]"                     # playing cards
    "|[\U0001F100-\U0001F1FF]"                     # enclosed alphanumerics
    "|[\U0001F200-\U0001F2FF]"                     # enclosed ideographic
    "|[\U0001F300-\U0001F5FF]"                     # misc symbols & pictographs
    "|[\U000E0020-\U000E007F]"                     # tags
    ")+",
    re.UNICODE,
)


@lru_cache(maxsize=32)
def _get_emoji_font(size: int) -> ImageFont.FreeTypeFont | None:
    """Load the color emoji font at *size*. Returns ``None`` if unavailable."""
    if not os.path.exists(_EMOJI_FONT_PATH):
        logger.debug("[emoji_text] NotoColorEmoji-Regular.ttf not found")
        return None
    try:
        return ImageFont.truetype(_EMOJI_FONT_PATH, size)
    except Exception as e:
        logger.warning(f"[emoji_text] failed to load emoji font: {e}")
        return None


def _split_emoji_segments(text: str) -> list[Tuple[str, bool]]:
    """Split *text* into segments of (substring, is_emoji)."""
    segments: list[Tuple[str, bool]] = []
    last_end = 0
    for m in _EMOJI_RE.finditer(text):
        start, end = m.span()
        if start > last_end:
            segments.append((text[last_end:start], False))
        segments.append((m.group(), True))
        last_end = end
    if last_end < len(text):
        segments.append((text[last_end:], False))
    return segments


def draw_text_with_emoji(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    xy: Tuple[int, int],
    text: str,
    fill,
    font: ImageFont.FreeTypeFont,
    emoji_scale: float = 1.0,
) -> int:
    """
    Draw *text* at *xy* on *img* / *draw*, rendering emoji via the bundled
    NotoColorEmoji font and everything else via *font*.

    Parameters
    ----------
    img : PIL.Image.Image
        The target RGBA/RGB image (needed for alpha-compositing emoji glyphs).
    draw : PIL.ImageDraw.ImageDraw
        The ``ImageDraw`` handle for *img*.
    xy : tuple[int, int]
        Top-left coordinate.
    text : str
        The string to render.
    fill : color
        Fill colour for non-emoji text.
    font : ImageFont.FreeTypeFont
        The font used for non-emoji characters.
    emoji_scale : float
        Scale factor applied to the emoji size relative to the text font size.

    Returns
    -------
    int
        The total rendered width in pixels.
    """
    segments = _split_emoji_segments(text)

    # If there are no emoji segments just fall back to the normal draw.text path.
    has_emoji = any(is_emoji for _, is_emoji in segments)
    if not has_emoji:
        draw.text(xy, text, fill=fill, font=font)
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]

    # Determine emoji font size from the text font metrics.
    try:
        font_size = font.size
    except AttributeError:
        font_size = 20
    emoji_font_size = max(10, int(font_size * emoji_scale))
    emoji_font = _get_emoji_font(emoji_font_size)

    x, y = xy
    cursor_x = x

    # We measure text ascent to vertically center emoji glyphs.
    ascent, descent = font.getmetrics()
    line_height = ascent + descent

    for segment_text, is_emoji in segments:
        if not segment_text:
            continue

        if not is_emoji or emoji_font is None:
            # Render with the normal font
            draw.text((cursor_x, y), segment_text, fill=fill, font=font)
            bbox = draw.textbbox((0, 0), segment_text, font=font)
            seg_w = bbox[2] - bbox[0]
            cursor_x += seg_w
        else:
            # Render emoji: draw onto a temporary RGBA image, then composite.
            try:
                # NotoColorEmoji is a bitmap/CBDT font; each glyph is a fixed-size
                # bitmap.  We render at the font's native size and scale down.
                # Use a large-enough temp canvas.
                tmp_size = emoji_font_size * 2 + 20
                tmp = Image.new("RGBA", (tmp_size, tmp_size), (0, 0, 0, 0))
                tmp_draw = ImageDraw.Draw(tmp)
                tmp_draw.text((0, 0), segment_text, font=emoji_font, embedded_color=True)

                # Crop to the actual glyph bounding box.
                bbox = tmp.getbbox()
                if bbox:
                    glyph = tmp.crop(bbox)
                    # Scale to match text line height.
                    target_h = int(line_height * 0.9)
                    if target_h <= 0:
                        target_h = emoji_font_size
                    scale = target_h / glyph.height if glyph.height > 0 else 1.0
                    target_w = max(1, int(glyph.width * scale))
                    target_h = max(1, target_h)
                    glyph = glyph.resize((target_w, target_h), Image.LANCZOS)

                    # Vertically center the glyph on the text baseline.
                    glyph_y = y + (line_height - target_h) // 2

                    # Composite onto the main image.
                    if img.mode == "RGBA":
                        img.alpha_composite(glyph, (int(cursor_x), int(glyph_y)))
                    else:
                        img.paste(glyph, (int(cursor_x), int(glyph_y)), glyph)

                    cursor_x += target_w + 1
                else:
                    # Empty glyph; fall back to normal font.
                    draw.text((cursor_x, y), segment_text, fill=fill, font=font)
                    bbox2 = draw.textbbox((0, 0), segment_text, font=font)
                    cursor_x += bbox2[2] - bbox2[0]
            except Exception as e:
                logger.debug(f"[emoji_text] emoji render failed for '{segment_text}': {e}")
                draw.text((cursor_x, y), segment_text, fill=fill, font=font)
                bbox2 = draw.textbbox((0, 0), segment_text, font=font)
                cursor_x += bbox2[2] - bbox2[0]

    return cursor_x - x


def measure_text_with_emoji(
    text: str,
    font: ImageFont.FreeTypeFont,
    emoji_scale: float = 1.0,
) -> int:
    """
    Measure the pixel width of *text* accounting for emoji glyphs, without
    actually drawing anything.
    """
    segments = _split_emoji_segments(text)
    has_emoji = any(is_emoji for _, is_emoji in segments)
    if not has_emoji:
        dummy = Image.new("RGB", (10, 10))
        d = ImageDraw.Draw(dummy)
        bbox = d.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]

    try:
        font_size = font.size
    except AttributeError:
        font_size = 20
    emoji_font_size = max(10, int(font_size * emoji_scale))
    emoji_font = _get_emoji_font(emoji_font_size)

    ascent, descent = font.getmetrics()
    line_height = ascent + descent

    total_w = 0
    dummy = Image.new("RGBA", (10, 10))
    dummy_draw = ImageDraw.Draw(dummy)

    for segment_text, is_emoji in segments:
        if not segment_text:
            continue
        if not is_emoji or emoji_font is None:
            bbox = dummy_draw.textbbox((0, 0), segment_text, font=font)
            total_w += bbox[2] - bbox[0]
        else:
            try:
                tmp_size = emoji_font_size * 2 + 20
                tmp = Image.new("RGBA", (tmp_size, tmp_size), (0, 0, 0, 0))
                tmp_draw = ImageDraw.Draw(tmp)
                tmp_draw.text((0, 0), segment_text, font=emoji_font, embedded_color=True)
                bbox = tmp.getbbox()
                if bbox:
                    glyph_w = bbox[2] - bbox[0]
                    glyph_h = bbox[3] - bbox[1]
                    target_h = int(line_height * 0.9)
                    if target_h <= 0:
                        target_h = emoji_font_size
                    scale = target_h / glyph_h if glyph_h > 0 else 1.0
                    total_w += max(1, int(glyph_w * scale)) + 1
                else:
                    bbox2 = dummy_draw.textbbox((0, 0), segment_text, font=font)
                    total_w += bbox2[2] - bbox2[0]
            except Exception:
                bbox2 = dummy_draw.textbbox((0, 0), segment_text, font=font)
                total_w += bbox2[2] - bbox2[0]

    return total_w
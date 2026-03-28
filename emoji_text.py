"""
Emoji-aware text rendering for Pillow.

Detects emoji characters in text and renders them using TwitterColorEmoji-SVGinOT.ttf,
while rendering all other characters with the caller-supplied font.
"""

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw, ImageFont, features

from astrbot.api import logger

# Emoji font path
_EMOJI_FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "TwitterColorEmoji-SVGinOT.ttf")

_EMOJI_RE = re.compile(
    "("
    "[\U0001F1E0-\U0001F1FF]{2}"
    "|[\U0001F3FB-\U0001F3FF]"
    "|[\U0001F600-\U0001F64F]"
    "|[\U0001F680-\U0001F6FF]"
    "|[\U0001F700-\U0001F77F]"
    "|[\U0001F780-\U0001F7FF]"
    "|[\U0001F800-\U0001F8FF]"
    "|[\U0001F900-\U0001F9FF]"
    "|[\U0001FA00-\U0001FA6F]"
    "|[\U0001FA70-\U0001FAFF]"
    "|[\U00002702-\U000027B0]"
    "|[\U0000FE00-\U0000FE0F]"
    "|[\U0000200D]"
    "|[\U000020E3]"
    "|[\U00002600-\U000026FF]"
    "|[\U00002300-\U000023FF]"
    "|[\U00002B50\U00002B55\U000023F0\U000023F3]"
    "|[\U0000203C\U00002049]"
    "|[\U0000231A\U0000231B]"
    "|[\U000025AA-\U000025FE]"
    "|[\U00002934\U00002935]"
    "|[\U00003030\U0000303D]"
    "|[\U00003297\U00003299]"
    "|[\U0001F000-\U0001F02F]"
    "|[\U0001F0A0-\U0001F0FF]"
    "|[\U0001F100-\U0001F1FF]"
    "|[\U0001F200-\U0001F2FF]"
    "|[\U0001F300-\U0001F5FF]"
    "|[\U000E0020-\U000E007F]"
    ")+",
    re.UNICODE,
)

@lru_cache(maxsize=32)
def _get_emoji_font(size: int) -> ImageFont.FreeTypeFont | None:
    if not os.path.exists(_EMOJI_FONT_PATH):
        logger.error(f"[emoji_text] Font NOT FOUND at: {_EMOJI_FONT_PATH}")
        return None
    try:
        f = ImageFont.truetype(_EMOJI_FONT_PATH, size)
        # 探测字体名称，确认是否正确加载了 Twemoji
        name = f.getname()
        logger.debug(f"[emoji_text] Successfully loaded emoji font: {name} at size {size}")
        return f
    except Exception as e:
        logger.warning(f"[emoji_text] Failed to load emoji font: {e}")
        return None

def _split_emoji_segments(text: str) -> list[Tuple[str, bool]]:
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
    segments = _split_emoji_segments(text)
    has_emoji = any(is_emoji for _, is_emoji in segments)
    
    if not has_emoji:
        draw.text(xy, text, fill=fill, font=font)
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]

    try:
        font_size = font.size
    except AttributeError:
        font_size = 20
    
    emoji_font_size = max(10, int(font_size * emoji_scale))
    emoji_font = _get_emoji_font(emoji_font_size)

    x, y = xy
    cursor_x = x
    ascent, descent = font.getmetrics()
    line_height = ascent + descent

    for segment_text, is_emoji in segments:
        if not segment_text:
            continue
        
        # 调试日志：查看每一段的判定情况
        logger.debug(f"[emoji_text] Processing segment: {segment_text!r} | is_emoji: {is_emoji}")

        if not is_emoji or emoji_font is None:
            draw.text((cursor_x, y), segment_text, fill=fill, font=font)
            bbox = draw.textbbox((0, 0), segment_text, font=font)
            seg_w = bbox[2] - bbox[0]
            cursor_x += seg_w
        else:
            try:
                # 建立画布
                tmp_size = int(emoji_font_size * 2.5) 
                tmp = Image.new("RGBA", (tmp_size, tmp_size), (0, 0, 0, 0))
                tmp_draw = ImageDraw.Draw(tmp)
                
                # 尝试渲染
                # 注意：给一个白色 fill 辅助渲染，同时开启 embedded_color
                tmp_draw.text((0, 0), segment_text, font=emoji_font, fill=(255, 255, 255, 255), embedded_color=True)

                bbox = tmp.getbbox()
                if bbox:
                    glyph = tmp.crop(bbox)
                    target_h = int(line_height * 0.9)
                    if target_h <= 0: target_h = emoji_font_size
                    
                    scale = target_h / glyph.height if glyph.height > 0 else 1.0
                    target_w = max(1, int(glyph.width * scale))
                    glyph = glyph.resize((target_w, target_h), Image.LANCZOS)

                    glyph_y = y + (line_height - target_h) // 2
                    if img.mode == "RGBA":
                        img.alpha_composite(glyph, (int(cursor_x), int(glyph_y)))
                    else:
                        img.paste(glyph, (int(cursor_x), int(glyph_y)), glyph)

                    cursor_x += target_w + 1
                    logger.debug(f"[steam-monitor] Rendered emoji '{segment_text}' successfully. Width: {target_w}")
                else:
                    # 关键错误点：BBox 为空说明字体读到了，但渲染不出像素
                    logger.debug(f"[steam-monitor] Render failed for '{segment_text}', falling back to space.")
                    # 使用普通字体画一个空格，确保占据一定的宽度
                    space_text = " " * len(segment_text) 
                    draw.text((cursor_x, y), space_text, fill=fill, font=font)
                    bbox_s = draw.textbbox((0, 0), space_text, font=font)
                    cursor_x += (bbox_s[2] - bbox_s[0])
            except Exception as e:
                # 发生异常也降级为空格
                logger.error(f"[emoji_text] Render error: {e}")
                cursor_x += draw.textbbox((0, 0), " ", font=font)[2]

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
        
        # 逻辑：非 Emoji 或未加载字体，正常测量文字宽度
        if not is_emoji or emoji_font is None:
            bbox = dummy_draw.textbbox((0, 0), segment_text, font=font)
            total_w += bbox[2] - bbox[0]
        else:
            try:
                # 尝试渲染检测是否支持
                tmp_size = emoji_font_size * 2 + 20
                tmp = Image.new("RGBA", (tmp_size, tmp_size), (0, 0, 0, 0))
                tmp_draw = ImageDraw.Draw(tmp)
                tmp_draw.text((0, 0), segment_text, font=emoji_font, embedded_color=True)
                
                bbox = tmp.getbbox()
                if bbox:
                    # 渲染成功：计算缩放后的宽度
                    glyph_w = bbox[2] - bbox[0]
                    glyph_h = bbox[3] - bbox[1]
                    target_h = int(line_height * 0.9)
                    if target_h <= 0:
                        target_h = emoji_font_size
                    scale = target_h / glyph_h if glyph_h > 0 else 1.0
                    total_w += max(1, int(glyph_w * scale)) + 1
                else:
                    # 渲染失败：测量相同数量的“空格”宽度
                    space_str = " " * len(segment_text)
                    bbox_s = dummy_draw.textbbox((0, 0), space_str, font=font)
                    total_w += (bbox_s[2] - bbox_s[0])
            except Exception:
                # 异常情况：同样退回到空格宽度
                bbox_s = dummy_draw.textbbox((0, 0), " " * len(segment_text), font=font)
                total_w += (bbox_s[2] - bbox_s[0])

    return total_w

def check_svg_support() -> str:
    """检测当前渲染环境并返回诊断报告"""
    from PIL import features
    import PIL
    
    results = [f"[Emoji 渲染诊断]", f"Pillow 版本: {PIL.__version__}"]
    
    # 1. 检查基础库
    freetype = features.check("freetype2")
    raqm = features.check("raqm")
    results.append(f"FreeType 支持: {'✅' if freetype else '❌'}")
    results.append(f"Raqm : {'✅' if raqm else '❌'}")
    
    # 2. 检查字体文件
    if not os.path.exists(_EMOJI_FONT_PATH):
        results.append(f"字体文件: ❌ 未找到 ({os.path.basename(_EMOJI_FONT_PATH)})")
        return "\n".join(results)
    results.append(f"字体文件: ✅ ")

    # 3. 尝试渲染测试
    try:
        test_font = _get_emoji_font(24)
        if test_font:
            tmp = Image.new("RGBA", (50, 50), (0, 0, 0, 0))
            draw = ImageDraw.Draw(tmp)
            draw.text((0, 0), "⭐", font=test_font, embedded_color=True)
            bbox = tmp.getbbox()
            if bbox:
                results.append("渲染测试: ✅ 成功 (检测到像素生成)")
            else:
                results.append("渲染测试: ❌ 失败 (BBox为空，缺少SVG解析库)")
                results.append("\n修复建议: 容器内运行 apt-get install liblibrsvg2-dev libpng-dev libjpeg-dev libfreetype6-dev 并pip强制重装 Pillow")
    except Exception as e:
        results.append(f"渲染测试: ❌ 异常 ({str(e)})")

    return "\n".join(results)
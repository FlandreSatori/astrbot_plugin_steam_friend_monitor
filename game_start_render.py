import os
import io
import time
import httpx
from PIL import Image, ImageDraw, ImageFont
from urllib.parse import quote

from astrbot.api import logger
from .emoji_text import draw_text_with_emoji, measure_text_with_emoji

BG_COLOR_TOP = (49, 80, 66)
BG_COLOR_BOTTOM = (28, 35, 44)
AVATAR_SIZE = 80
COVER_W, COVER_H = 80, 120
IMG_W, IMG_H = 512, 192  # 16:6，画布高度减少三分之一
# 星星素材路径
STAR_BG_PATH = os.path.join(os.path.dirname(__file__), "star_767x809.png")
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
PLAYTIME_CACHE_TTL_SEC = 7 * 24 * 3600
_PLAYTIME_CACHE: dict[str, tuple[float, float]] = {}


def _playtime_cache_key(steamid: str, appid: str) -> str:
    return f"{steamid}:{appid}"


def _playtime_cache_get(steamid: str, appid: str) -> float | None:
    key = _playtime_cache_key(str(steamid), str(appid))
    item = _PLAYTIME_CACHE.get(key)
    if not item:
        return None
    ts, val = item
    if time.time() - ts > PLAYTIME_CACHE_TTL_SEC:
        _PLAYTIME_CACHE.pop(key, None)
        return None
    return float(val)


def _playtime_cache_set(steamid: str, appid: str, hours: float):
    key = _playtime_cache_key(str(steamid), str(appid))
    _PLAYTIME_CACHE[key] = (time.time(), float(hours))


def _safe_json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def _download_binary(
    client: httpx.AsyncClient,
    url: str,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    expect_image: bool = True,
) -> bytes | None:
    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                return None
            content_type = str(resp.headers.get("content-type", "")).lower()
            if expect_image and content_type and not content_type.startswith("image/"):
                logger.warning(
                    f"[steam-monitor] non-image response while downloading: url={url} content-type={content_type}"
                )
                return None
            content_length = int(resp.headers.get("content-length", "0") or 0)
            if content_length > max_bytes:
                logger.warning(
                    f"[steam-monitor] download too large by header: url={url} size={content_length}"
                )
                return None

            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    logger.warning(
                        f"[steam-monitor] download exceeded limit while reading: url={url}"
                    )
                    return None
            return bytes(buf) if buf else None
    except Exception as e:
        logger.debug(f"[steam-monitor] download failed: url={url} err={e}")
        return None


async def get_avatar_path(data_dir, steamid, url, client: httpx.AsyncClient, force_update=False):
    avatar_dir = os.path.join(data_dir, "avatars")
    os.makedirs(avatar_dir, exist_ok=True)
    path = os.path.join(avatar_dir, f"{steamid}.jpg")
    refresh_interval = 24 * 3600
    if os.path.exists(path) and not force_update:
        if time.time() - os.path.getmtime(path) < refresh_interval:
            return path
    if not url:
        return path if os.path.exists(path) else None
    try:
        raw = await _download_binary(client, url)
        if raw:
            with open(path, "wb") as f:
                f.write(raw)
            return path
    except Exception as e:
        logger.debug(f"[steam-monitor] avatar download failed steamid={steamid}: {e}")
    return path if os.path.exists(path) else None


async def get_sgdb_vertical_cover(
    game_name,
    sgdb_api_key=None,
    sgdb_game_name=None,
    appid=None,
    sgdb_api_base=None,
    client: httpx.AsyncClient | None = None,
):
    if not sgdb_api_key:
        return None
    headers = {"Authorization": f"Bearer {sgdb_api_key}"}
    sgdb_api_base = (sgdb_api_base or "https://www.steamgriddb.com").rstrip("/")
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=10, follow_redirects=True)

    async def _query_cover_by_name(name: str) -> str | None:
        encoded = quote(str(name or "").strip(), safe="")
        if not encoded:
            return None
        search_url = f"{sgdb_api_base}/api/v2/search/autocomplete/{encoded}"
        resp = await client.get(search_url, headers=headers)
        data = _safe_json(resp)
        if not data.get("success") or not data.get("data"):
            return None
        sgdb_game_id = data["data"][0].get("id")
        if not sgdb_game_id:
            return None
        grid_url = (
            f"{sgdb_api_base}/api/v2/grids/game/{sgdb_game_id}"
            "?dimensions=600x900&type=static&limit=3"
        )
        resp2 = await client.get(grid_url, headers=headers)
        data2 = _safe_json(resp2)
        grids = data2.get("data") if isinstance(data2.get("data"), list) else []
        if not grids:
            return None
        for grid in grids[:3]:
            if isinstance(grid, dict) and grid.get("type") == "static" and grid.get("url"):
                return str(grid["url"])
        first = grids[0]
        if isinstance(first, dict) and first.get("url"):
            return str(first["url"])
        return None

    try:
        search_name = sgdb_game_name if sgdb_game_name else game_name
        cover_url = await _query_cover_by_name(str(search_name or ""))
        if cover_url:
            return cover_url

        if appid:
            game_url = f"{sgdb_api_base}/api/v2/games/steam/{appid}"
            resp_game = await client.get(game_url, headers=headers)
            data_game = _safe_json(resp_game)
            sgdb_name = str((data_game.get("data") or {}).get("name") or "").strip()
            if sgdb_name:
                return await _query_cover_by_name(sgdb_name)
        return None
    except Exception as e:
        logger.warning(f"[steam-monitor] get SGDB cover failed appid={appid}: {e}")
        return None
    finally:
        if owns_client:
            await client.aclose()


async def get_cover_path(
    data_dir,
    gameid,
    game_name,
    force_update=False,
    sgdb_api_key=None,
    sgdb_game_name=None,
    appid=None,
    sgdb_api_base=None,
    client: httpx.AsyncClient | None = None,
):
    cover_dir = os.path.join(data_dir, "covers_v")
    os.makedirs(cover_dir, exist_ok=True)
    path = os.path.join(cover_dir, f"{gameid}.jpg")
    # 只在本地不存在时才云端获取
    if os.path.exists(path):
        return path

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=10, follow_redirects=True)

    # 只尝试 SGDB 竖版封面
    try:
        url = await get_sgdb_vertical_cover(
            game_name,
            sgdb_api_key,
            sgdb_game_name=sgdb_game_name,
            appid=appid,
            sgdb_api_base=sgdb_api_base,
            client=client,
        )
        if url and client is not None:
            try:
                raw = await _download_binary(client, url)
                if raw:
                    with open(path, "wb") as f:
                        f.write(raw)
                    return path
            except Exception as e:
                logger.warning(f"[steam-monitor] SGDB cover download failed gameid={gameid}: {e}")

        logger.info(
            f"[steam-monitor] SGDB cover missing, fallback to local default gameid={gameid} game_name={game_name}"
        )
        missing_cover = os.path.join(os.path.dirname(__file__), "missingcover.jpg")
        if os.path.exists(missing_cover):
            return missing_cover
        return None
    finally:
        if owns_client:
            await client.aclose()


def text_wrap(text, font, max_width):
    """自动换行，返回行列表"""
    lines = []
    if not text:
        return [""]
    line = ""
    # 创建临时画布用于测量
    dummy_img = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(dummy_img)
    for char in text:
        bbox = draw.textbbox((0, 0), line + char, font=font)
        width = bbox[2] - bbox[0]
        if width <= max_width:
            line += char
        else:
            lines.append(line)
            line = char
    if line:
        lines.append(line)
    return lines


def get_chinese_length(text):
    """估算中文字符长度（1中文=2英文）"""
    length = 0
    for c in text:
        if "\u4e00" <= c <= "\u9fff":
            length += 1
        else:
            length += 0.5
    return int(length + 0.5)

def render_gradient_bg(img_w, img_h, color_top, color_bottom):
    """生成竖向渐变背景"""
    base = Image.new("RGB", (img_w, img_h), color_top)
    top_r, top_g, top_b = color_top
    bot_r, bot_g, bot_b = color_bottom
    for y in range(img_h):
        ratio = y / (img_h - 1)
        r = int(top_r * (1 - ratio) + bot_r * ratio)
        g = int(top_g * (1 - ratio) + bot_g * ratio)
        b = int(top_b * (1 - ratio) + bot_b * ratio)
        for x in range(img_w):
            base.putpixel((x, y), (r, g, b))
    return base


async def get_playtime_hours(
    api_key,
    steamid,
    appid,
    retry_times=1,
    steam_api_base=None,
    client: httpx.AsyncClient | None = None,
):
    """通过 Steam Web API 获取总游玩小时数；失败时回退到最近一次成功缓存。"""
    import asyncio

    steam_api_base = (steam_api_base or "https://api.steampowered.com").rstrip("/")
    url = (
        f"{steam_api_base}/IPlayerService/GetOwnedGames/v1/"
        f"?key={api_key}&steamid={steamid}&include_appinfo=0&appids_filter[0]={appid}"
    )
    cached_hours = _playtime_cache_get(str(steamid), str(appid))
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=10, follow_redirects=True)
    try:
        for attempt in range(retry_times):
            try:
                resp = await client.get(
                    url,
                    timeout=httpx.Timeout(connect=3.0, read=4.0, write=4.0, pool=2.0),
                )
                if resp.status_code == 200:
                    data = _safe_json(resp)
                    games = data.get("response", {}).get("games", [])
                    for g in games:
                        if str(g.get("appid")) == str(appid):
                            playtime_min = g.get("playtime_forever", 0)
                            hours = round(playtime_min / 60, 1)
                            _playtime_cache_set(str(steamid), str(appid), hours)
                            return hours
                    logger.debug(
                        f"[steam-monitor] playtime game not found steamid={steamid} appid={appid}"
                    )
                else:
                    logger.debug(
                        f"[steam-monitor] playtime request status={resp.status_code} steamid={steamid} appid={appid}"
                    )
            except Exception as e:
                logger.warning(f"[steam-monitor] get playtime failed appid={appid}: {e}")
            if attempt < retry_times - 1:
                await asyncio.sleep(1)
        if cached_hours is not None:
            logger.debug(
                "[steam-monitor] playtime fallback to cache "
                f"steamid={steamid} appid={appid} hours={cached_hours}"
            )
            return cached_hours
        return "N/A"
    finally:
        if owns_client:
            await client.aclose()


def get_font_path(font_name):
    fonts_dir = os.path.join(os.path.dirname(__file__), "fonts")
    font_path = os.path.join(fonts_dir, font_name)
    if os.path.exists(font_path):
        return font_path
    font_path2 = os.path.join(os.path.dirname(__file__), font_name)
    if os.path.exists(font_path2):
        return font_path2
    return font_name


def render_game_start_image(
    player_name,
    avatar_path,
    game_name,
    cover_path,
    playtime_hours=None,
    superpower=None,
    online_count=None,
    font_path=None,
    bg_image_path=None,
    bg_opacity=0.15,
):
    # 字体
    fonts_dir = os.path.join(os.path.dirname(__file__), "fonts")
    default_regular = os.path.join(fonts_dir, "NotoSansHans-Regular.otf")
    default_medium = os.path.join(fonts_dir, "NotoSansHans-Medium.otf")

    font_regular = default_regular
    if font_path and os.path.exists(font_path):
        font_regular = font_path
    elif not os.path.exists(font_regular):
        fallback_regular = os.path.join(os.path.dirname(__file__), "NotoSansHans-Regular.otf")
        if os.path.exists(fallback_regular):
            font_regular = fallback_regular

    font_medium = default_medium
    if "Regular" in os.path.basename(font_regular):
        derived_medium = font_regular.replace("Regular", "Medium")
        if os.path.exists(derived_medium):
            font_medium = derived_medium
    elif os.path.exists(default_medium):
        font_medium = default_medium
    elif os.path.exists(font_regular):
        font_medium = font_regular
    try:
        font_bold = ImageFont.truetype(font_medium, 28)
        font = ImageFont.truetype(font_regular, 22)
        font_small = ImageFont.truetype(font_regular, 16)
    except Exception:
        font_bold = font = font_small = ImageFont.load_default()

    img_w = IMG_W
    img_h = IMG_H
    img = render_gradient_bg(img_w, img_h, BG_COLOR_TOP, BG_COLOR_BOTTOM).convert("RGBA")
    draw = ImageDraw.Draw(img)

    # 1. 背景图片横向平铺（等比例缩放高度，透明度可配），如果bg_image_path为None则使用默认星星
    bg_path = bg_image_path if bg_image_path else STAR_BG_PATH
    try:
        bg_img = Image.open(bg_path).convert("RGBA")
        bg_w, bg_h = bg_img.size
        scale = IMG_H / bg_h
        new_w = int(bg_w * scale)
        new_h = IMG_H
        bg_resized = bg_img.resize((new_w, new_h), Image.LANCZOS)
        # 设置透明度
        alpha = bg_resized.split()[-1].point(lambda p: int(p * bg_opacity))
        bg_resized.putalpha(alpha)
        for x in range(0, IMG_W, new_w):
            img.alpha_composite(bg_resized, (x, 0))
    except Exception as e:
        logger.warning(f"[steam-monitor] background image load failed bg_path={bg_path}: {e}")

    # 2. 封面图贴左，等比例缩放高度，宽度自适应，左贴右留空，不裁剪
    cover_area_h = IMG_H
    new_w = COVER_W  # 默认宽度，防止后续变量未定义
    if cover_path and os.path.exists(cover_path):
        try:
            cover_src = Image.open(cover_path).convert("RGBA")
            scale = cover_area_h / cover_src.height
            new_w = int(cover_src.width * scale)
            new_h = cover_area_h
            cover_resized = cover_src.resize((new_w, new_h), Image.LANCZOS)
            img.paste(cover_resized, (0, 0), cover_resized)
        except Exception as e:
            logger.warning(f"[steam-monitor] cover render failed: {e}")
            new_w = COVER_W  # 渲染失败时使用默认宽度

    # 3. 头像位置参数（不再渲染头像）
    avatar_size = AVATAR_SIZE
    avatar_margin = 24
    cover_right = int(new_w)
    avatar_x = cover_right + avatar_margin
    # avatar_y 的赋值和渲染放到后面

    # 4. 文本：头像右侧，整体垂直居中，左右留白，无背景
    text_x = avatar_x + avatar_size + avatar_margin
    text_area_w = img_w - text_x - avatar_margin
    
    # 游戏名自适应字号（优先缩小字号使其单行显示，不行则截断）
    game_name_str = str(game_name or "")
    game_name_font_size = 22
    game_name_display = game_name_str
    min_font_size = 14
    
    for size in range(22, min_font_size - 1, -1):
        try:
            game_font_tmp = ImageFont.truetype(font_regular, size)
        except Exception:
            game_font_tmp = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), game_name_display, font=game_font_tmp)
        if bbox[2] - bbox[0] <= text_area_w:
            game_name_font_size = size
            break
    else:
        # 字号已到最小仍放不下，则截断游戏名
        try:
            game_font_min = ImageFont.truetype(font_regular, min_font_size)
        except Exception:
            game_font_min = ImageFont.load_default()
        game_name_display = game_name_str
        for i in range(len(game_name_str) - 1, 0, -1):
            truncated = game_name_str[:i] + "..."
            bbox = draw.textbbox((0, 0), truncated, font=game_font_min)
            if bbox[2] - bbox[0] <= text_area_w:
                game_name_display = truncated
                break
        else:
            game_name_display = "..."
        game_name_font_size = min_font_size
    
    try:
        font_game_final = ImageFont.truetype(font_regular, game_name_font_size)
    except Exception:
        font_game_final = ImageFont.load_default()
    
    line_height = 36
    # 游戏名只占一行
    block_height = line_height * 3 + 10 + font_small.size + 4  # 玩家名 + 正在玩 + 游戏名(单行) + 时长
    text_y = (img_h - block_height) // 2

    # 将头像Y坐标与玩家名对齐，并下移10像素
    avatar_y = text_y + 10

    # 头像渲染（只保留一次）
    if avatar_path and os.path.exists(avatar_path):
        try:
            avatar = Image.open(avatar_path).convert("RGBA").resize((AVATAR_SIZE, AVATAR_SIZE))
            # 圆角遮罩
            mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
            draw_mask = ImageDraw.Draw(mask)
            draw_mask.rounded_rectangle((0, 0, AVATAR_SIZE, AVATAR_SIZE), radius=AVATAR_SIZE // 5, fill=255)
            avatar_rgba = avatar.copy()
            avatar_rgba.putalpha(mask)
            img.alpha_composite(avatar_rgba, (avatar_x, avatar_y))
        except Exception as e:
            logger.warning(f"[steam-monitor] avatar render failed: {e}")
    # 新增：右上角显示在线人数
    online_text = None
    if online_count is not None:
        try:
            font_online = ImageFont.truetype(font_regular, 14)
        except Exception:
            font_online = ImageFont.load_default()
        online_text = f"·在线人数 {online_count}"

    # 玩家名自适应字号
    max_playername_w = IMG_W - (text_x + 8) - 24
    player_font_size = 28
    for size in range(28, 15, -2):
        try:
            font_bold_tmp = ImageFont.truetype(font_medium, size)
        except Exception:
            font_bold_tmp = ImageFont.load_default()
        measured_w = measure_text_with_emoji(player_name, font_bold_tmp)
        if measured_w <= max_playername_w:
            player_font_size = size
            break
    try:
        font_bold_final = ImageFont.truetype(font_medium, player_font_size)
    except Exception:
        font_bold_final = ImageFont.load_default()
    draw_text_with_emoji(img, draw, (text_x + 8, text_y), player_name, fill=(255, 255, 255, 255), font=font_bold_final)

    # “正在玩”
    draw.text((text_x + 8, text_y + line_height), "正在玩", font=font, fill=(200, 255, 200, 255))
    # 游戏名单行显示（亮绿色 129,173,81）
    draw.text((text_x + 8, text_y + line_height * 2), game_name_display, font=font_game_final, fill=(129, 173, 81, 255))
    # 游戏时长（紧跟在游戏名下方，无多余空行）
    if playtime_hours is not None:
        playtime_str = f"游戏时间 {playtime_hours} 小时"
        y_time = text_y + line_height * 3 + 4  # 游戏名下一行
        draw.text((text_x + 8, y_time), playtime_str, font=font_small, fill=(120, 180, 255, 255))
    else:
        logger.debug("[steam-monitor] playtime unavailable while rendering game start image")

    # 在线人数渲染（右上角）
    if online_text:
        text_bbox = draw.textbbox((0, 0), online_text, font=font_online)
        online_text_w = text_bbox[2] - text_bbox[0] + 10
        draw.text((IMG_W - online_text_w, 10), online_text, font=font_online, fill=(120, 180, 255, 180))

    return img.convert("RGB")


async def render_game_start(
    data_dir,
    steamid,
    player_name,
    avatar_url,
    gameid,
    game_name,
    api_key=None,
    superpower=None,
    online_count=None,
    sgdb_api_key=None,
    font_path=None,
    sgdb_game_name=None,
    appid=None,
    sgdb_api_base=None,
    steam_api_base=None,
    bg_image_path=None,
    bg_opacity=0.15,
    client: httpx.AsyncClient | None = None,
):
    logger.debug(f"[steam-monitor] render_game_start superpower={superpower}")
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=10, follow_redirects=True)
    try:
        avatar_path = await get_avatar_path(data_dir, steamid, avatar_url, client=client)
        cover_path = await get_cover_path(
            data_dir,
            gameid,
            game_name,
            sgdb_api_key=sgdb_api_key,
            sgdb_game_name=sgdb_game_name,
            appid=appid,
            sgdb_api_base=sgdb_api_base,
            client=client,
        )
        playtime_hours = None
        if api_key:
            playtime_hours = await get_playtime_hours(
                api_key,
                steamid,
                gameid,
                retry_times=1,
                steam_api_base=steam_api_base,
                client=client,
            )
    finally:
        if owns_client:
            await client.aclose()
    img = render_game_start_image(
        player_name,
        avatar_path,
        game_name,
        cover_path,
        playtime_hours,
        superpower,
        online_count,
        font_path=font_path,
        bg_image_path=bg_image_path,
        bg_opacity=bg_opacity,
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()

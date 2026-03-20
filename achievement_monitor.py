import json
import io
import hashlib
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Set

import aiohttp
import httpx
from PIL import Image, ImageDraw, ImageFont

from astrbot.api import logger


class AchievementMonitor:
    """Steam 成就查询与缓存。"""

    def __init__(
        self,
        data_dir: Path,
        steam_api_base: str = "https://api.steampowered.com",
    ):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.steam_api_base = (steam_api_base or "https://api.steampowered.com").rstrip("/")

        self.achievements_file = self.data_dir / "achievements_cache.json"
        self.blacklist_file = self.data_dir / "achievement_blacklist.json"
        self.icon_cache_dir = self.data_dir / "achievement_icons"
        self.icon_cache_dir.mkdir(parents=True, exist_ok=True)

        self.initial_achievements: Dict[str, list[str]] = {}
        self.achievement_blacklist: set[str] = set()
        self.details_cache: OrderedDict[tuple[str, str], tuple[float, Dict[str, Any]]] = OrderedDict()
        self.details_cache_ttl_sec = 3600
        self.details_cache_max_items = 256
        self.icon_max_bytes = 2 * 1024 * 1024
        self.http = httpx.AsyncClient(timeout=15, follow_redirects=True)

        self._load_achievements_cache()
        self._load_blacklist()

    async def aclose(self):
        await self.http.aclose()

    def _make_key(self, target: str, steamid: str, appid: str) -> str:
        return json.dumps([str(target), str(steamid), str(appid)], ensure_ascii=False)

    def _load_achievements_cache(self):
        if not self.achievements_file.exists():
            self.initial_achievements = {}
            return
        try:
            data = json.loads(self.achievements_file.read_text(encoding="utf-8"))
            self.initial_achievements = data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"[steam-monitor] load achievements cache failed: {e}")
            self.initial_achievements = {}

    def _save_achievements_cache(self):
        try:
            self.achievements_file.write_text(
                json.dumps(self.initial_achievements, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[steam-monitor] save achievements cache failed: {e}")

    def _load_blacklist(self):
        if not self.blacklist_file.exists():
            self.achievement_blacklist = set()
            return
        try:
            data = json.loads(self.blacklist_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self.achievement_blacklist = {str(x) for x in data if str(x).strip()}
            else:
                self.achievement_blacklist = set()
        except Exception as e:
            logger.warning(f"[steam-monitor] load achievement blacklist failed: {e}")
            self.achievement_blacklist = set()

    def _save_blacklist(self):
        try:
            self.blacklist_file.write_text(
                json.dumps(sorted(self.achievement_blacklist), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[steam-monitor] save achievement blacklist failed: {e}")

    async def get_player_achievements(
        self,
        api_key: str,
        target: str,
        steamid: str,
        appid: str,
    ) -> Optional[Set[str]]:
        """获取玩家在 appid 内已解锁成就 apiname 集合。"""
        appid = str(appid or "").strip()
        steamid = str(steamid or "").strip()
        if not api_key or not steamid or not appid:
            return None
        if appid in self.achievement_blacklist:
            return None

        url = f"{self.steam_api_base}/ISteamUserStats/GetPlayerAchievements/v1/"
        lang_list = ["schinese", "english", "en"]

        all_failed = True
        for lang in lang_list:
            params = {
                "key": api_key,
                "steamid": steamid,
                "appid": appid,
                "l": lang,
            }
            for attempt in range(3):
                try:
                    resp = await self.http.get(url, params=params)
                    if resp.status_code == 401:
                        logger.info(
                            f"[steam-monitor] no permission to read achievements steamid={steamid} appid={appid}"
                        )
                        return None
                    if resp.status_code != 200:
                        continue

                    data = resp.json()
                    achievements = (
                        data.get("playerstats", {}).get("achievements", [])
                        if isinstance(data, dict)
                        else []
                    )
                    if not isinstance(achievements, list):
                        continue

                    # 只要成功拿到 achievements 列表，就视为请求成功。
                    all_failed = False

                    unlocked = {
                        str(ach.get("apiname", "")).strip()
                        for ach in achievements
                        if isinstance(ach, dict)
                        and ach.get("achieved", 0) == 1
                        and str(ach.get("apiname", "")).strip()
                    }

                    return unlocked
                except Exception as e:
                    logger.debug(
                        f"[steam-monitor] get player achievements failed attempt={attempt + 1} appid={appid} lang={lang}: {e}"
                    )

        if all_failed:
            self.achievement_blacklist.add(appid)
            self._save_blacklist()
            logger.info(f"[steam-monitor] app added to achievement blacklist appid={appid}")
        return None

    async def get_achievement_details(
        self,
        target: str,
        appid: str,
        lang: str = "schinese",
        api_key: str = "",
        steamid: str = "",
    ) -> Dict[str, Any]:
        """获取成就详情：apiname -> {name, description, icon, icon_gray, percent}。"""
        appid = str(appid or "").strip()
        if not appid or appid in self.achievement_blacklist:
            return {}

        cache_key = (str(target), appid)
        cached = self.details_cache.get(cache_key)
        if cached:
            ts, payload = cached
            if (time.time() - ts) <= self.details_cache_ttl_sec:
                self.details_cache.move_to_end(cache_key)
                return payload
            self.details_cache.pop(cache_key, None)

        details: Dict[str, Any] = {}
        url_stats = (
            f"{self.steam_api_base}/ISteamUserStats/"
            f"GetGlobalAchievementPercentagesForApp/v2/?gameid={appid}"
        )
        lang_list = [lang, "schinese", "english", "en"]

        try:
            # 先取解锁率
            percents: Dict[str, Any] = {}
            try:
                stats_resp = await self.http.get(url_stats)
                if stats_resp.status_code == 200:
                    stats_data = stats_resp.json()
                    arr = (
                        stats_data.get("achievementpercentages", {}).get("achievements", [])
                        if isinstance(stats_data, dict)
                        else []
                    )
                    if isinstance(arr, list):
                        for ach in arr:
                            if isinstance(ach, dict):
                                name = str(ach.get("name", "")).strip()
                                if name:
                                    percents[name] = ach.get("percent")
            except Exception as e:
                logger.debug(f"[steam-monitor] get achievement percents failed appid={appid}: {e}")

            for try_lang in lang_list:
                schema_url = (
                    f"{self.steam_api_base}/ISteamUserStats/GetSchemaForGame/v2/"
                    f"?appid={appid}&key={api_key}&l={try_lang}"
                )
                resp = await self.http.get(schema_url)
                if resp.status_code == 400 and api_key and steamid:
                    # 降级为玩家成就接口，至少拿到名称
                    p_resp = await self.http.get(
                        f"{self.steam_api_base}/ISteamUserStats/GetPlayerAchievements/v1/",
                        params={
                            "key": api_key,
                            "steamid": steamid,
                            "appid": appid,
                            "l": try_lang,
                        },
                    )
                    if p_resp.status_code == 200:
                        p_data = p_resp.json()
                        arr = (
                            p_data.get("playerstats", {}).get("achievements", [])
                            if isinstance(p_data, dict)
                            else []
                        )
                        if isinstance(arr, list):
                            for ach in arr:
                                if not isinstance(ach, dict):
                                    continue
                                apiname = str(ach.get("apiname", "")).strip()
                                if not apiname:
                                    continue
                                details[apiname] = {
                                    "name": ach.get("name") or apiname,
                                    "description": ach.get("description") or "",
                                    "icon": None,
                                    "icon_gray": None,
                                    "percent": percents.get(apiname),
                                }
                            if any(str(v.get("description", "")).strip() for v in details.values()):
                                break
                    continue

                if resp.status_code != 200:
                    continue
                data = resp.json()
                achievements = (
                    data.get("game", {})
                    .get("availableGameStats", {})
                    .get("achievements", [])
                    if isinstance(data, dict)
                    else []
                )
                if not isinstance(achievements, list):
                    continue

                tmp_details: Dict[str, Any] = {}
                for ach in achievements:
                    if not isinstance(ach, dict):
                        continue
                    apiname = str(ach.get("name", "")).strip()
                    if not apiname:
                        continue

                    def to_icon_url(val: Any) -> Optional[str]:
                        sval = str(val or "").strip()
                        if not sval:
                            return None
                        if sval.startswith("http://") or sval.startswith("https://"):
                            return sval
                        return (
                            "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/"
                            f"{appid}/{sval}.jpg"
                        )

                    tmp_details[apiname] = {
                        "name": ach.get("displayName") or apiname,
                        "description": ach.get("description") or "",
                        "icon": to_icon_url(ach.get("icon")),
                        "icon_gray": to_icon_url(ach.get("icongray")),
                        "percent": percents.get(apiname),
                    }
                if tmp_details:
                    details = tmp_details
                if any(str(v.get("description", "")).strip() for v in details.values()):
                    break
        except Exception as e:
            logger.warning(f"[steam-monitor] get achievement details failed appid={appid}: {e}")

        if details:
            self.details_cache[cache_key] = (time.time(), details)
            self.details_cache.move_to_end(cache_key)
            while len(self.details_cache) > self.details_cache_max_items:
                self.details_cache.popitem(last=False)
        return details

    def clear_game_achievements(self, target: str, steamid: str, appid: str):
        key = self._make_key(target, steamid, appid)
        if key in self.initial_achievements:
            self.initial_achievements.pop(key, None)
            self._save_achievements_cache()

    def _wrap_text(self, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
        """自动按像素宽度换行。"""
        if not text:
            return [""]
        lines: list[str] = []
        line = ""
        dummy_img = Image.new("RGB", (10, 10))
        draw = ImageDraw.Draw(dummy_img)
        for char in str(text):
            bbox = draw.textbbox((0, 0), line + char, font=font)
            width = bbox[2] - bbox[0]
            if width <= max_width:
                line += char
            else:
                if line:
                    lines.append(line)
                line = char
        if line:
            lines.append(line)
        return lines

    def _icon_cache_path(self, icon_url: str) -> Path:
        digest = hashlib.sha256(icon_url.encode("utf-8", errors="ignore")).hexdigest()
        return self.icon_cache_dir / f"{digest}.png"

    async def _load_achievement_icon(
        self,
        session: aiohttp.ClientSession,
        icon_url: str,
        icon_size: int,
    ) -> Image.Image | None:
        if not icon_url:
            return None

        cache_path = self._icon_cache_path(icon_url)
        raw: bytes | None = None

        if cache_path.exists():
            try:
                raw = cache_path.read_bytes()
            except Exception as e:
                logger.debug(f"[steam-monitor] read icon cache failed: {e}")

        if raw is None:
            try:
                async with session.get(icon_url) as response:
                    if response.status == 200:
                        content_type = str(response.headers.get("Content-Type", "")).lower()
                        if content_type and not content_type.startswith("image/"):
                            logger.warning(
                                f"[steam-monitor] skip non-image icon content-type={content_type} url={icon_url}"
                            )
                            return None

                        content_length = int(response.headers.get("Content-Length", "0") or 0)
                        if content_length > self.icon_max_bytes:
                            logger.warning(
                                f"[steam-monitor] icon too large by header size={content_length} url={icon_url}"
                            )
                            return None

                        buf = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            if not chunk:
                                continue
                            buf.extend(chunk)
                            if len(buf) > self.icon_max_bytes:
                                logger.warning(
                                    f"[steam-monitor] icon too large while reading url={icon_url}"
                                )
                                return None
                        raw = bytes(buf)
                        if raw:
                            try:
                                cache_path.write_bytes(raw)
                            except Exception as e:
                                logger.debug(f"[steam-monitor] write icon cache failed: {e}")
            except Exception as e:
                logger.debug(f"[steam-monitor] download icon failed: {e}")

        if not raw:
            return None

        try:
            icon_img = Image.open(io.BytesIO(raw)).convert("RGBA")
            icon_img = icon_img.resize((icon_size, icon_size), Image.LANCZOS)
            mask_icon = Image.new("L", (icon_size, icon_size), 0)
            ImageDraw.Draw(mask_icon).rounded_rectangle((0, 0, icon_size, icon_size), 12, fill=255)
            icon_img.putalpha(mask_icon)
            return icon_img
        except Exception as e:
            logger.debug(f"[steam-monitor] decode icon failed: {e}")
            return None

    async def render_achievement_image(
        self,
        achievement_details: dict,
        new_achievements: set,
        player_name: str = "",
        steamid: str | None = None,
        appid: int | str | None = None,
        unlocked_set: set | None = None,
        font_path: str | None = None,
        api_key: str = "",
        target: str = "",
    ) -> bytes:
        # 与 steam_status_monitor 保持同款视觉风格
        width = 420
        padding_v = 18
        padding_h = 18
        card_gap = 14
        card_radius = 9
        card_inner_bg = (38, 44, 56, 220)
        card_base_bg = (35, 38, 46, 255)
        icon_size = 64
        icon_margin_right = 16
        text_margin_top = 10
        max_text_width = width - padding_h * 2 - icon_size - icon_margin_right - 18

        if unlocked_set is None:
            unlocked_set = set()
            if steamid is not None and appid is not None and api_key:
                current = await self.get_player_achievements(
                    api_key,
                    target,
                    str(steamid),
                    str(appid),
                )
                unlocked_set = current or set()

        unlocked_achievements = len(unlocked_set)
        total_achievements = len(achievement_details)
        progress_percent = int(unlocked_achievements / total_achievements * 100) if total_achievements else 0

        title_text = f"{player_name} 解锁新成就"
        game_name = ""
        for detail in achievement_details.values():
            if detail and detail.get("name"):
                game_name = detail.get("game_name", "") or detail.get("game", "") or ""
                break
        if not game_name:
            game_name = next(
                (d.get("game_name") for d in achievement_details.values() if d and d.get("game_name")),
                "",
            )
        if not game_name:
            game_name = "未知游戏"

        now_str = datetime.now().strftime("%m-%d %H:%M")

        fonts_dir = Path(__file__).parent / "fonts"
        default_regular = fonts_dir / "NotoSansHans-Regular.otf"
        default_medium = fonts_dir / "NotoSansHans-Medium.otf"
        font_regular = Path(font_path) if font_path else default_regular
        if not font_regular.is_absolute():
            font_regular = fonts_dir / font_regular.name
        font_medium = Path(str(font_regular).replace("Regular", "Medium"))
        if not font_medium.exists():
            font_medium = default_medium
        if not font_regular.exists():
            font_regular = default_regular

        # 尝试多个字体位置：传入路径 -> 数据目录 -> 插件目录
        if font_path and Path(font_path).exists():
            font_regular = Path(font_path)
        else:
            # 尝试从数据目录加载
            data_fonts_dir = self.data_dir / "fonts"
            candidate_regular = data_fonts_dir / "NotoSansHans-Regular.otf"
            if candidate_regular.exists():
                font_regular = candidate_regular
            else:
                # 从插件目录加载
                font_regular = fonts_dir / "NotoSansHans-Regular.otf"

        # 替换为Medium版本（从同目录）
        font_medium = Path(str(font_regular).replace("Regular", "Medium"))
        if not font_medium.exists():
            # 尝试找到Medium文件的备选
            data_fonts_dir = self.data_dir / "fonts"
            candidate_medium = data_fonts_dir / "NotoSansHans-Medium.otf"
            if candidate_medium.exists():
                font_medium = candidate_medium
            else:
                font_medium = fonts_dir / "NotoSansHans-Medium.otf"
        if not font_medium.exists():
            font_medium = font_regular  # 回退到Regular版本

        try:
            font_title = ImageFont.truetype(str(font_medium), 20)
            font_game_small = ImageFont.truetype(str(font_regular), 12)
            font_name = ImageFont.truetype(str(font_medium), 16)
            font_desc = ImageFont.truetype(str(font_regular), 13)
            font_percent = ImageFont.truetype(str(font_regular), 12)
            font_time = ImageFont.truetype(str(font_regular), 10)
        except Exception:
            font_title = font_game_small = font_name = font_desc = font_percent = font_time = ImageFont.load_default()

        dummy_img = Image.new("RGB", (10, 10))
        dummy_draw = ImageDraw.Draw(dummy_img)
        title_bbox = dummy_draw.textbbox((0, 0), title_text, font=font_title)
        title_h = title_bbox[3] - title_bbox[1]
        game_bbox = dummy_draw.textbbox((0, 0), game_name, font=font_game_small)
        game_h = game_bbox[3] - game_bbox[1]
        time_bbox = dummy_draw.textbbox((0, 0), now_str, font=font_time)
        time_w = time_bbox[2] - time_bbox[0]
        progress_bar_h = 12
        progress_bar_margin = 8
        title_game_gap = 8
        header_h = title_h + title_game_gap + game_h + progress_bar_h + progress_bar_margin * 3

        card_heights: list[int] = []
        card_texts: list[tuple[list[str], list[str], str]] = []
        percents: list[float] = []
        for apiname in new_achievements:
            detail = achievement_details.get(apiname)
            if not detail:
                card_heights.append(80)
                card_texts.append(([""], [""], "未知"))
                percents.append(0)
                continue
            name = detail.get("name", apiname)
            desc = detail.get("description", "")
            percent = detail.get("percent")
            try:
                percent_val = float(percent) if percent is not None else None
            except (ValueError, TypeError):
                percent_val = None
            percent_str = f"{percent_val:.1f}%" if percent_val is not None else "未知"
            percent_num = percent_val if percent_val is not None else 0

            name_lines = self._wrap_text(name, font_name, max_text_width)
            desc_lines = self._wrap_text(desc, font_desc, max_text_width)
            card_h = max(icon_size + 24, len(name_lines) * 22 + len(desc_lines) * 18 + 60)
            card_heights.append(card_h)
            card_texts.append((name_lines, desc_lines, percent_str))
            percents.append(percent_num)

        total_height = (
            padding_v
            + header_h
            + padding_v
            + sum(card_heights)
            + card_gap * (len(card_heights) - 1)
            + padding_v
        )

        img = Image.new("RGBA", (width, total_height), (20, 26, 33, 255))
        draw = ImageDraw.Draw(img)

        draw.text((padding_h, padding_v), title_text, fill=(255, 255, 255), font=font_title)
        draw.text(
            (padding_h, padding_v + title_h + title_game_gap),
            game_name,
            fill=(160, 160, 160),
            font=font_game_small,
        )
        draw.text((width - padding_h - time_w, padding_v), now_str, fill=(168, 168, 168), font=font_time)

        bar_x = padding_h
        bar_y = padding_v + title_h + title_game_gap + game_h + progress_bar_margin
        bar_w = width - padding_h * 2
        bar_h = progress_bar_h
        bar_radius = bar_h // 2
        draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h), radius=bar_radius, fill=(60, 62, 70, 180))
        progress_fill = (26, 159, 255, 255)
        fill_w = int(bar_w * progress_percent / 100)
        if fill_w > 0:
            draw.rounded_rectangle((bar_x, bar_y, bar_x + fill_w, bar_y + bar_h), radius=bar_radius, fill=progress_fill)
        progress_text = f"{unlocked_achievements}/{total_achievements} ({progress_percent}%)"
        progress_text_bbox = draw.textbbox((0, 0), progress_text, font=font_percent)
        progress_text_w = progress_text_bbox[2] - progress_text_bbox[0]
        draw.text((bar_x + bar_w - progress_text_w - 6, bar_y - 2), progress_text, fill=(142, 207, 255), font=font_percent)

        y = padding_v + header_h + padding_v
        async with aiohttp.ClientSession() as session:
            idx = 0
            for apiname in new_achievements:
                detail = achievement_details.get(apiname)
                if not detail:
                    y += card_heights[idx] + card_gap
                    idx += 1
                    continue

                name_lines, desc_lines, percent_str = card_texts[idx]
                percent_num = percents[idx]
                card_h = card_heights[idx]
                card_x0 = padding_h
                card_x1 = width - padding_h
                card_y0 = int(y)
                card_y1 = int(y + card_h)
                card_w = card_x1 - card_x0
                card_hh = card_y1 - card_y0

                card_bg = Image.new("RGBA", (card_w, card_hh), card_base_bg)
                card = Image.new("RGBA", (card_w, card_hh), (0, 0, 0, 0))
                mask = Image.new("L", (card_w, card_hh), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.rounded_rectangle((0, 0, card_w, card_hh), radius=card_radius, fill=255)
                card.paste(card_bg, (0, 0), mask)

                if percent_num < 10:
                    border_draw = ImageDraw.Draw(card)
                    gold_color = (255, 215, 128, 255)
                    border_width = 3
                    border_rect = (
                        border_width // 2,
                        border_width // 2,
                        card_w - border_width // 2 - 1,
                        card_hh - border_width // 2 - 1,
                    )
                    border_draw.rounded_rectangle(border_rect, radius=card_radius, outline=gold_color, width=border_width)

                bar_margin_x = 18
                bar_margin_y = 12
                card_bar_height = 8
                bar_radius2 = card_bar_height // 2
                bar_x0 = bar_margin_x
                bar_x1 = card_w - bar_margin_x
                bar_y1 = card_hh - bar_margin_y
                bar_y0 = bar_y1 - card_bar_height
                card_draw = ImageDraw.Draw(card)
                card_draw.rounded_rectangle((bar_x0, bar_y0, bar_x1, bar_y1), radius=bar_radius2, fill=(60, 62, 70, 180))
                if percent_num > 0:
                    card_fill_w = int((bar_x1 - bar_x0) * percent_num / 100)
                    if card_fill_w > 0:
                        card_draw.rounded_rectangle((bar_x0, bar_y0, bar_x0 + card_fill_w, bar_y1), radius=bar_radius2, fill=(26, 159, 255, 255))

                card_fg = Image.new("RGBA", (card_w, card_hh), card_inner_bg)
                card.paste(card_fg, (0, 0), mask)
                img.alpha_composite(card, (card_x0, card_y0))

                icon_url = detail.get("icon")
                icon_img = None
                if icon_url:
                    icon_img = await self._load_achievement_icon(session, str(icon_url), icon_size)

                icon_x = card_x0 + 12
                icon_y = card_y0 + (card_h - icon_size) // 2
                if icon_img:
                    if percent_num < 10:
                        glow_size = 10
                        canvas_size = icon_size + 2 * glow_size
                        icon_canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
                        glow = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
                        glow_draw = ImageDraw.Draw(glow)
                        for r in range(canvas_size // 2, icon_size // 2, -1):
                            alpha = int(120 * (canvas_size // 2 - r) / glow_size)
                            color = (255, 220, 60, max(0, alpha))
                            glow_draw.ellipse(
                                [
                                    canvas_size // 2 - r,
                                    canvas_size // 2 - r,
                                    canvas_size // 2 + r,
                                    canvas_size // 2 + r,
                                ],
                                outline=None,
                                fill=color,
                            )
                        icon_canvas = Image.alpha_composite(icon_canvas, glow)
                        icon_canvas.paste(icon_img, (glow_size, glow_size), icon_img)
                        img.alpha_composite(icon_canvas, (icon_x - glow_size, icon_y - glow_size))
                    else:
                        img.alpha_composite(icon_img, (icon_x, icon_y))

                text_x = icon_x + icon_size + icon_margin_right
                text_y = card_y0 + text_margin_top
                for i, line in enumerate(name_lines):
                    draw.text((text_x, text_y + i * 22), line, fill=(255, 255, 255), font=font_name)
                desc_y = text_y + len(name_lines) * 22 + 2
                for i, line in enumerate(desc_lines):
                    draw.text((text_x, desc_y + i * 18), line, fill=(187, 187, 187), font=font_desc)
                percent_y = desc_y + len(desc_lines) * 18 + 6

                percent_label = "全球解锁率："
                percent_label_bbox = draw.textbbox((0, 0), percent_label, font=font_percent)
                label_w = percent_label_bbox[2] - percent_label_bbox[0]
                bar_x_text = text_x + label_w + 4
                bar_y_text = percent_y + 4
                bar_height_text = 10
                bar_length = card_x1 - bar_x_text - 48
                bar_radius3 = bar_height_text // 2

                if percent_num < 10:
                    glow_radius = 16
                    for r in range(glow_radius, 0, -4):
                        draw.text(
                            (text_x, percent_y),
                            percent_label,
                            fill=(255, 220, 60, int(60 * r / glow_radius)),
                            font=font_percent,
                        )
                    value_x = bar_x_text + bar_length + 8
                    for r in range(glow_radius, 0, -4):
                        draw.text(
                            (value_x, percent_y),
                            percent_str,
                            fill=(255, 220, 60, int(60 * r / glow_radius)),
                            font=font_percent,
                        )

                draw.text(
                    (text_x, percent_y),
                    percent_label,
                    fill=(142, 207, 255) if percent_num >= 10 else (255, 220, 60),
                    font=font_percent,
                )
                draw.rounded_rectangle(
                    (bar_x_text, bar_y_text, bar_x_text + bar_length, bar_y_text + bar_height_text),
                    radius=bar_radius3,
                    fill=(60, 62, 70, 180),
                )
                if percent_num > 0:
                    fill_w_text = int(bar_length * percent_num / 100)
                    if fill_w_text > 0:
                        draw.rounded_rectangle(
                            (bar_x_text, bar_y_text, bar_x_text + fill_w_text, bar_y_text + bar_height_text),
                            radius=bar_radius3,
                            fill=(26, 159, 255, 255),
                        )
                value_x = bar_x_text + bar_length + 8
                draw.text(
                    (value_x, percent_y),
                    percent_str,
                    fill=(142, 207, 255) if percent_num >= 10 else (255, 220, 60),
                    font=font_percent,
                )

                y += card_h + card_gap
                idx += 1

        out = img.convert("RGB")
        img_byte_arr = io.BytesIO()
        out.save(img_byte_arr, format="PNG")
        return img_byte_arr.getvalue()

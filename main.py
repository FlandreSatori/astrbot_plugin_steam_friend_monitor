import asyncio
import contextlib
import hashlib
import html
from html.parser import HTMLParser
import ipaddress
import json
import platform
import random
import re
import tempfile
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from io import BytesIO
import uuid
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote, urljoin, urlparse

import httpx
from PIL import Image, ImageDraw, ImageFont

from .achievement_monitor import AchievementMonitor
from .game_start_render import render_game_start

from astrbot.api import AstrBotConfig, logger
import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

STEAM_SUMMARY_API = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"


def parse_ids(raw: str) -> List[str]:
    text = (raw or "").replace(chr(10), ",")
    return [x.strip() for x in text.split(",") if x.strip()]


def persona_text(state: int) -> str:
    mapping = {
        0: "离线",
        1: "在线",
        2: "忙碌",
        3: "离开",
        4: "打盹",
        5: "想交易",
        6: "想玩游戏",
    }
    return mapping.get(state, f"未知({state})")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def pick_cjk_font() -> str | None:
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simsun.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.otf",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    for fp in candidates:
        if Path(fp).exists():
            return fp
    return None


def safe_font(size: int, plugin_dir: Path | None = None):
    if plugin_dir is not None:
        bundled = plugin_dir / "fonts" / "NotoSansCJKsc-Regular.otf"
        if bundled.exists():
            try:
                return ImageFont.truetype(str(bundled), size)
            except Exception as e:
                logger.warning(f"[steam-monitor] load bundled font failed: {e}")

    sys_font = pick_cjk_font()
    if sys_font:
        try:
            return ImageFont.truetype(sys_font, size)
        except Exception as e:
            logger.warning(f"[steam-monitor] load system font failed: {e}")

    logger.warning(
        "[steam-monitor] no CJK font found; fallback font may render Chinese as squares"
    )
    return ImageFont.load_default()


def _dedup_keep_order(items):
    return list(dict.fromkeys(x for x in items if x))


def circle_crop(img: Image.Image) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(mask)
    d.ellipse((0, 0, img.size[0], img.size[1]), fill=255)
    out = Image.new("RGBA", img.size)
    out.paste(img, (0, 0), mask)
    return out


class SteamFriendMonitor(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.plugin_dir = Path(__file__).parent

        self.data_dir = StarTools.get_data_dir("astrbot_plugin_steam_friend_monitor")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "state.json"
        self.group_configs_file = self.data_dir / "group_configs.json"
        self.image_cache_dir = self.data_dir / "image_cache"
        self.image_cache_dir.mkdir(parents=True, exist_ok=True)

        self.state: Dict[str, Any] = self._load_state()
        self.group_configs: Dict[str, List[str]] = self._load_group_configs()
        self._stop = False
        self._task: asyncio.Task | None = None

        self.local_config_defaults = self._load_local_config_defaults()
        self.STEAM_API_BASE = self._normalize_base_url(
            self.config.get("steam_api_base", self.local_config_defaults.get("steam_api_base", "")),
            "https://api.steampowered.com",
        )
        self.STEAM_STORE_BASE = self._normalize_base_url(
            self.config.get("steam_store_base", self.local_config_defaults.get("steam_store_base", "")),
            "https://store.steampowered.com",
        )
        self.SGDB_API_KEY = str(
            self.config.get("sgdb_api_key", self.local_config_defaults.get("sgdb_api_key", ""))
            or ""
        )
        self.SGDB_API_BASE = self._normalize_base_url(
            self.config.get("sgdb_api_base", self.local_config_defaults.get("sgdb_api_base", "")),
            "https://www.steamgriddb.com",
        )

        self.http: httpx.AsyncClient | None = None
        self.bytes_cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self.icon_url_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self.profile_game_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._game_name_cache: Dict[str, tuple[str, str]] = {}
        self._config_lock = asyncio.Lock()
        self._bg_tasks: set[asyncio.Task] = set()
        self.achievement_monitor = AchievementMonitor(self.data_dir)
        self.achievement_poll_tasks: Dict[tuple[str, str, str], asyncio.Task] = {}
        self.achievement_final_tasks: Dict[tuple[str, str, str], asyncio.Task] = {}
        self.achievement_snapshots: Dict[tuple[str, str, str], List[str]] = {}
        self.achievement_fail_count: Dict[tuple[str, str], int] = {}

        # 字体初始化
        self.font_paths: Dict[str, str | None] = {}
        self._ensure_fonts()

    def _load_local_config_defaults(self) -> Dict[str, Any]:
        cfg_path = self.plugin_dir / "config.json"
        if not cfg_path.exists():
            return {}
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.warning(f"[steam-monitor] load local config.json failed: {e}")
        return {}

    def _normalize_base_url(self, value: Any, default: str) -> str:
        if not value:
            return default
        return str(value).rstrip("/")

    def _ensure_fonts(self):
        """检测插件fonts目录是否有NotoSansHans系列字体，并缓存路径"""
        import shutil
        plugin_fonts_dir = self.plugin_dir / 'fonts'
        cache_fonts_dir = self.data_dir / 'fonts'
        plugin_fonts_dir.mkdir(parents=True, exist_ok=True)
        cache_fonts_dir.mkdir(parents=True, exist_ok=True)

        font_candidates = [
            'NotoSansHans-Regular.otf',
            'NotoSansHans-Medium.otf',
            'NotoSansCJKsc-Regular.otf',  # 兼容旧版本
        ]
        self.font_paths = {}
        for font_name in font_candidates:
            plugin_font_path = plugin_fonts_dir / font_name
            cache_font_path = cache_fonts_dir / font_name
            if plugin_font_path.exists():
                shutil.copy(str(plugin_font_path), str(cache_font_path))
                self.font_paths[font_name] = str(cache_font_path)
            elif cache_font_path.exists():
                self.font_paths[font_name] = str(cache_font_path)
            else:
                self.font_paths[font_name] = None

        # 详细日志
        for font_name in font_candidates:
            path = self.font_paths.get(font_name)
            logger.debug(f"[steam-monitor] font cache - {font_name}: {path}")
        if not any(self.font_paths.values()):
            logger.warning("[steam-monitor] no CJK font found, rendering may show garbled text")

    def get_font_path(self, font_name: str | None = None, bold: bool = False) -> str:
        """获取字体路径，优先返回缓存目录下的NotoSansHans字体"""
        if not font_name:
            font_name = 'NotoSansHans-Regular.otf'
        if bold:
            font_name = 'NotoSansHans-Medium.otf'
        return self.font_paths.get(font_name) or font_name

    async def initialize(self):
        self.achievement_monitor.steam_api_base = self.STEAM_API_BASE
        self.http = httpx.AsyncClient(timeout=15, follow_redirects=True)
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "[steam-monitor] runtime: "
            f"os={platform.system()} "
            f"release={platform.release()} "
            f"python={platform.python_version()}"
        )
        logger.info(
            "[steam-monitor] image cfg: "
            f"image_proxy_prefix={self.config.get('image_proxy_prefix', 'https://images.weserv.nl/?url=')} "
            f"strict_remote_host={self.config.get('strict_remote_host', False)} "
            f"allow_dns_private_for_allow_domains={self.config.get('allow_dns_private_for_allow_domains', True)} "
            f"remote_host_allowlist={self.config.get('remote_host_allowlist', '')} "
            f"max_redirects={self.config.get('max_redirects', 3)} "
            f"max_image_bytes={self.config.get('max_image_bytes', 3 * 1024 * 1024)}"
        )
        logger.info("[steam-monitor] initialized")

    async def terminate(self):
        self._stop = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for t in list(self._bg_tasks):
            t.cancel()
        self._bg_tasks.clear()

        for t in list(self.achievement_poll_tasks.values()):
            t.cancel()
        self.achievement_poll_tasks.clear()
        for t in list(self.achievement_final_tasks.values()):
            t.cancel()
        self.achievement_final_tasks.clear()
        self.achievement_snapshots.clear()

        if self.http:
            await self.http.aclose()
            self.http = None
        logger.info("[steam-monitor] terminated")

    def _load_state(self) -> Dict[str, Any]:
        if not self.state_file.exists():
            return {}
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[steam-monitor] load state failed: {e}")
            return {}

    def _save_state(self):
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.state_file)

    def _load_group_configs(self) -> Dict[str, List[str]]:
        if not self.group_configs_file.exists():
            return {}
        try:
            data = json.loads(self.group_configs_file.read_text(encoding="utf-8"))
            return {str(k): v for k, v in data.items()}
        except Exception as e:
            logger.warning(f"[steam-monitor] load group configs failed: {e}")
            return {}

    def _save_group_configs(self):
        tmp = self.group_configs_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.group_configs, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.group_configs_file)

    def _get_group_steam_ids(self, group_id: str) -> List[str] | None:
        """获取某个群的专属时间 ID，如果未设置则返回 None"""
        group_id = str(group_id or "").strip()
        return self.group_configs.get(group_id)

    def _set_group_steam_ids(self, group_id: str, steam_ids: List[str]):
        """设置某个群的专属时间 ID"""
        group_id = str(group_id or "").strip()
        uniq = _dedup_keep_order(steam_ids)
        if uniq:
            self.group_configs[group_id] = uniq
        else:
            self.group_configs.pop(group_id, None)
        self._save_group_configs()

    async def _update_group_steam_ids_atomic(self, group_id: str, steam_ids: List[str]):
        """原子操作更新群 steam_ids"""
        async with self._config_lock:
            self._set_group_steam_ids(group_id, steam_ids)

    def _save_config_safe(self):
        try:
            self.config.save_config()
        except Exception as e:
            logger.warning(f"[steam-monitor] save config failed: {e}")

    async def _update_config_atomic(self, key: str, value: str):
        async with self._config_lock:
            self.config[key] = value
            self._save_config_safe()

    async def _update_targets_atomic(self, targets: List[str]):
        async with self._config_lock:
            self._set_targets(targets)

    def _status_image_min_interval_min(self) -> int:
        return max(
            0,
            int(self.config.get("status_image_min_interval_min", 0) or 0),
        )

    def _presence_flap_suppress_min(self) -> int:
        return max(
            0,
            int(self.config.get("presence_flap_suppress_min", 0) or 0),
        )

    def _count_game_duration_online_only(self) -> bool:
        return bool(self.config.get("count_game_duration_online_only", False))

    def _is_duration_countable_state(self, state: int) -> bool:
        if self._count_game_duration_online_only():
            return state == 1
        return state != 0

    def _daily_cycle_key_utc8(self, now: datetime) -> str:
        cycle_date = now.date()
        if now.hour < 7:
            cycle_date = cycle_date - timedelta(days=1)
        return cycle_date.isoformat()

    def _daily_cycle_start_utc8(self, now: datetime) -> datetime:
        cycle_date = now.date()
        if now.hour < 7:
            cycle_date = cycle_date - timedelta(days=1)
        return datetime(cycle_date.year, cycle_date.month, cycle_date.day, 7, 0, 0)

    def _session_seconds_in_current_cycle(
        self, start_ts: str | None, now: datetime, cycle_start: datetime
    ) -> int:
        if not start_ts:
            return 0
        start_dt = parse_iso(start_ts)
        if not start_dt:
            return 0
        if now <= start_dt:
            return 0
        effective_start = max(start_dt, cycle_start)
        if now <= effective_start:
            return 0
        return int((now - effective_start).total_seconds())

    def _session_seconds_total(self, start_ts: str | None, now: datetime) -> int:
        if not start_ts:
            return 0
        start_dt = parse_iso(start_ts)
        if not start_dt:
            return 0
        if now <= start_dt:
            return 0
        return int((now - start_dt).total_seconds())

    def _safe_int(self, value: Any, default: int = 0) -> int:
        with contextlib.suppress(Exception):
            return int(value)
        return default

    def _parse_trigger_types_config(
        self, config_key: str, default_value: str
    ) -> set[str]:
        allowed = {
            "online",
            "offline",
            "game_start",
            "game_stop",
            "game_switch",
        }
        configured = {
            x.strip().lower()
            for x in parse_ids(self.config.get(config_key, default_value))
            if x.strip()
        }
        return configured & allowed

    def _status_text_trigger_types(self) -> set[str]:
        return self._parse_trigger_types_config(
            "status_text_trigger_types",
            "online,offline,game_start,game_stop,game_switch",
        )

    def _status_image_trigger_types(self) -> set[str]:
        return self._parse_trigger_types_config(
            "status_image_trigger_types",
            "online,offline,game_start,game_stop,game_switch",
        )

    def _normalize_target_key(self, target: str) -> str:
        return str(target or "").strip()

    def _get_target_last_push_ts(self, target: str) -> float:
        key = self._normalize_target_key(target)
        if not key:
            return 0.0

        # 新字段：按群独立冷却时间
        data = self.state.get("_group_last_push_ts", {})
        if isinstance(data, dict):
            with contextlib.suppress(Exception):
                return float(data.get(key, 0.0) or 0.0)

        # 兼容旧字段，避免升级后冷却状态丢失
        legacy = self.state.get("_target_last_push_ts", {})
        if isinstance(legacy, dict):
            with contextlib.suppress(Exception):
                return float(legacy.get(key, 0.0) or 0.0)
        return 0.0

    def _set_target_last_push_ts(self, target: str, ts: float):
        key = self._normalize_target_key(target)
        if not key:
            return

        data = self.state.get("_group_last_push_ts")
        if not isinstance(data, dict):
            data = {}
            self.state["_group_last_push_ts"] = data
        data[key] = float(ts)

    def _get_target_player_state(self, target: str) -> Dict[str, Any]:
        """获取按群隔离的玩家状态快照，用于事件判定。"""
        key = self._normalize_target_key(target)
        all_target_state = self.state.get("_target_player_state")
        if not isinstance(all_target_state, dict):
            all_target_state = {}
            self.state["_target_player_state"] = all_target_state

        one_target_state = all_target_state.get(key)
        if not isinstance(one_target_state, dict):
            one_target_state = {}
            all_target_state[key] = one_target_state
        return one_target_state

    async def _is_host_resolved_private(self, host: str) -> bool:
        host = (host or "").strip()
        if not host:
            logger.debug("[steam-monitor] _is_host_resolved_private: empty host -> private")
            return True
        with contextlib.suppress(Exception):
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(host, None)
            logger.debug(
                f"[steam-monitor] dns resolve host={host} resolved_count={len(infos)}"
            )
            for info in infos:
                ip_str = info[4][0]
                with contextlib.suppress(Exception):
                    ip = ipaddress.ip_address(ip_str)
                    logger.debug(
                        "[steam-monitor] dns resolved "
                        f"host={host} ip={ip_str} "
                        f"loopback={ip.is_loopback} private={ip.is_private} "
                        f"link_local={ip.is_link_local} reserved={ip.is_reserved} "
                        f"multicast={ip.is_multicast} unspecified={ip.is_unspecified}"
                    )
                    if (
                        ip.is_loopback
                        or ip.is_private
                        or ip.is_link_local
                        or ip.is_reserved
                        or ip.is_multicast
                        or ip.is_unspecified
                    ):
                        logger.debug(
                            f"[steam-monitor] host resolved to private/local ip, host={host} ip={ip_str}"
                        )
                        return True
        logger.debug(f"[steam-monitor] host resolved non-private host={host}")
        return False

    async def _delayed_unlink(self, image_path: str, delay_sec: int = 30):
        await asyncio.sleep(max(1, delay_sec))
        with contextlib.suppress(Exception):
            Path(image_path).unlink(missing_ok=True)

    def _schedule_delayed_unlink(self, image_path: str, delay_sec: int = 30):
        task = asyncio.create_task(self._delayed_unlink(image_path, delay_sec))
        self._bg_tasks.add(task)
        task.add_done_callback(lambda t: self._bg_tasks.discard(t))

    def _spawn_bg_task(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(lambda t: self._bg_tasks.discard(t))
        return task

    def _achievement_enabled(self) -> bool:
        default = self.local_config_defaults.get("enable_achievement_monitor", True)
        return bool(self.config.get("enable_achievement_monitor", default))

    def _achievement_poll_interval_sec(self) -> int:
        default = self.local_config_defaults.get("achievement_poll_interval_sec", 1200)
        return max(60, int(self.config.get("achievement_poll_interval_sec", default) or default))

    def _achievement_final_delay_sec(self) -> int:
        default = self.local_config_defaults.get("achievement_final_check_delay_sec", 300)
        return max(60, int(self.config.get("achievement_final_check_delay_sec", default) or default))

    def _achievement_fail_limit(self) -> int:
        default = self.local_config_defaults.get("achievement_fail_limit_per_day", 10)
        return max(1, int(self.config.get("achievement_fail_limit_per_day", default) or default))

    def _achievement_max_notify(self) -> int:
        default = self.local_config_defaults.get("max_achievement_notifications", 5)
        return max(1, int(self.config.get("max_achievement_notifications", default) or default))

    def _achievement_key(self, target: str, sid: str, gameid: str) -> tuple[str, str, str]:
        return (str(target), str(sid), str(gameid))

    async def _start_achievement_monitoring(
        self,
        target: str,
        sid: str,
        gameid: str,
        player_name: str,
        game_name: str,
    ):
        if not self._achievement_enabled():
            return
        api_key = str(self.config.get("steam_api_key", "")).strip()
        gameid = str(gameid or "").strip()
        if not api_key or not sid or not gameid:
            return

        key = self._achievement_key(target, sid, gameid)
        old_final = self.achievement_final_tasks.pop(key, None)
        if old_final:
            old_final.cancel()

        if key in self.achievement_poll_tasks:
            return

        try:
            achievements = await self.achievement_monitor.get_player_achievements(
                api_key,
                target,
                sid,
                gameid,
            )
            self.achievement_snapshots[key] = list(achievements) if achievements else []
            poll_task = asyncio.create_task(
                self._achievement_periodic_check(
                    target,
                    sid,
                    gameid,
                    player_name,
                    game_name,
                )
            )
            self.achievement_poll_tasks[key] = poll_task
            poll_task.add_done_callback(lambda t: self.achievement_poll_tasks.pop(key, None))
        except Exception as e:
            logger.warning(
                f"[steam-monitor] start achievement monitoring failed sid={sid} gameid={gameid}: {e}"
            )

    def _schedule_achievement_final_check(
        self,
        target: str,
        sid: str,
        gameid: str,
        player_name: str,
        game_name: str,
    ):
        gameid = str(gameid or "").strip()
        if not gameid:
            return
        key = self._achievement_key(target, sid, gameid)

        poll_task = self.achievement_poll_tasks.pop(key, None)
        if poll_task:
            poll_task.cancel()

        old_final = self.achievement_final_tasks.pop(key, None)
        if old_final:
            old_final.cancel()

        final_task = asyncio.create_task(
            self._achievement_delayed_final_check(
                target,
                sid,
                gameid,
                player_name,
                game_name,
            )
        )
        self.achievement_final_tasks[key] = final_task
        final_task.add_done_callback(lambda t: self.achievement_final_tasks.pop(key, None))

    async def _achievement_periodic_check(
        self,
        target: str,
        sid: str,
        gameid: str,
        player_name: str,
        game_name: str,
    ):
        key = self._achievement_key(target, sid, gameid)
        api_key = str(self.config.get("steam_api_key", "")).strip()
        interval_sec = self._achievement_poll_interval_sec()
        try:
            while not self._stop:
                await asyncio.sleep(interval_sec)
                if gameid in self.achievement_monitor.achievement_blacklist:
                    break

                before = set(self.achievement_snapshots.get(key, []))
                current = await self.achievement_monitor.get_player_achievements(
                    api_key,
                    target,
                    sid,
                    gameid,
                )
                today = datetime.now().strftime("%Y-%m-%d")
                fail_key = (gameid, today)
                if current is None:
                    cnt = self.achievement_fail_count.get(fail_key, 0) + 1
                    self.achievement_fail_count[fail_key] = cnt
                    if cnt >= self._achievement_fail_limit():
                        self.achievement_monitor.achievement_blacklist.add(gameid)
                        self.achievement_monitor._save_blacklist()
                        logger.info(
                            f"[steam-monitor] achievement app blacklisted appid={gameid} fail_count={cnt}"
                        )
                        break
                    continue

                new_achievements = set(current) - before
                if new_achievements:
                    await self._notify_new_achievements(
                        target,
                        sid,
                        player_name,
                        gameid,
                        game_name,
                        new_achievements,
                    )
                self.achievement_snapshots[key] = list(current)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(
                f"[steam-monitor] achievement periodic check failed sid={sid} gameid={gameid}: {e}"
            )

    async def _achievement_delayed_final_check(
        self,
        target: str,
        sid: str,
        gameid: str,
        player_name: str,
        game_name: str,
    ):
        key = self._achievement_key(target, sid, gameid)
        try:
            await asyncio.sleep(self._achievement_final_delay_sec())
            if gameid in self.achievement_monitor.achievement_blacklist:
                return

            api_key = str(self.config.get("steam_api_key", "")).strip()
            before = set(self.achievement_snapshots.get(key, []))
            current = await self.achievement_monitor.get_player_achievements(
                api_key,
                target,
                sid,
                gameid,
            )
            if current is None:
                return

            new_achievements = set(current) - before
            if new_achievements:
                await self._notify_new_achievements(
                    target,
                    sid,
                    player_name,
                    gameid,
                    game_name,
                    new_achievements,
                )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(
                f"[steam-monitor] achievement final check failed sid={sid} gameid={gameid}: {e}"
            )
        finally:
            self.achievement_snapshots.pop(key, None)
            self.achievement_monitor.clear_game_achievements(target, sid, gameid)

    async def _notify_new_achievements(
        self,
        target: str,
        sid: str,
        player_name: str,
        gameid: str,
        game_name: str,
        new_achievements: set[str],
    ):
        if not self._achievement_enabled() or not new_achievements:
            return

        achievements_to_notify = list(new_achievements)[: self._achievement_max_notify()]
        extra_count = len(new_achievements) - len(achievements_to_notify)
        key = self._achievement_key(target, sid, gameid)
        api_key = str(self.config.get("steam_api_key", "")).strip()

        details: Dict[str, Any] = {}
        try:
            details = await self.achievement_monitor.get_achievement_details(
                target,
                gameid,
                lang="schinese",
                api_key=api_key,
                steamid=sid,
            )
        except Exception as e:
            logger.debug(f"[steam-monitor] get achievement details failed appid={gameid}: {e}")

        if details and game_name:
            for d in details.values():
                if isinstance(d, dict):
                    d["game_name"] = game_name

        if details:
            try:
                unlocked_set = await self.achievement_monitor.get_player_achievements(
                    api_key,
                    target,
                    sid,
                    gameid,
                )
                if not unlocked_set:
                    unlocked_set = set(self.achievement_snapshots.get(key, []))
                if unlocked_set is None:
                    unlocked_set = set()

                font_path = self.get_font_path("NotoSansHans-Regular.otf") or str(
                    self.plugin_dir / "fonts" / "NotoSansCJKsc-Regular.otf"
                )

                img_bytes = await self.achievement_monitor.render_achievement_image(
                    details,
                    set(achievements_to_notify),
                    player_name=player_name,
                    steamid=sid,
                    appid=gameid,
                    unlocked_set=unlocked_set,
                    font_path=font_path,
                    api_key=api_key,
                    target=target,
                )
                with tempfile.NamedTemporaryFile(
                    delete=False,
                    suffix=".png",
                    dir=str(self.data_dir),
                ) as tmp:
                    tmp.write(img_bytes)
                    tmp_path = tmp.name

                image_chain = MessageChain()
                image_chain.chain = [Comp.Image.fromFileSystem(tmp_path)]
                await self.context.send_message(target, image_chain)
                self._schedule_delayed_unlink(tmp_path, 30)
                return
            except Exception as e:
                logger.warning(f"[steam-monitor] achievement image render failed appid={gameid}: {e}")

        lines = [f"{player_name} 在《{game_name or gameid}》解锁了新成就："]
        for apiname in achievements_to_notify:
            detail = details.get(apiname, {}) if isinstance(details, dict) else {}
            show_name = str(detail.get("name") or apiname)
            desc = str(detail.get("description") or "").strip()
            if desc:
                lines.append(f"- {show_name}: {desc}")
            else:
                lines.append(f"- {show_name}")
        if extra_count > 0:
            lines.append(f"... 以及另外 {extra_count} 个成就")

        await self._push_text(target, "\n".join(lines))

    def _game_start_render_enabled(self) -> bool:
        default = self.local_config_defaults.get("enable_game_start_render", True)
        return bool(self.config.get("enable_game_start_render", default))

    async def _get_game_names(
        self, gameid: str, fallback_name: str | None = None
    ) -> tuple[str, str]:
        if not gameid:
            fallback = fallback_name or "未知游戏"
            return (fallback, fallback)

        gid = str(gameid)
        if gid in self._game_name_cache:
            return self._game_name_cache[gid]

        url_zh = f"{self.STEAM_STORE_BASE}/api/appdetails?appids={gid}&l=schinese"
        url_en = f"{self.STEAM_STORE_BASE}/api/appdetails?appids={gid}&l=en"
        name_zh = fallback_name or "未知游戏"
        name_en = fallback_name or "未知游戏"

        try:
            if not self.http:
                self.http = httpx.AsyncClient(timeout=15, follow_redirects=True)
            resp_zh = await self.http.get(url_zh)
            data_zh = resp_zh.json()
            info_zh = data_zh.get(gid, {}).get("data", {})
            name_zh = info_zh.get("name") or name_zh

            resp_en = await self.http.get(url_en)
            data_en = resp_en.json()
            info_en = data_en.get(gid, {}).get("data", {})
            name_en = info_en.get("name") or name_en
        except Exception as e:
            logger.debug(f"[steam-monitor] get game names failed gameid={gid}: {e}")

        self._game_name_cache[gid] = (name_zh, name_en)
        return (name_zh, name_en)

    async def _get_game_online_count(self, gameid: str) -> int | None:
        gid = str(gameid or "").strip()
        if not gid:
            return None

        url = f"{self.STEAM_API_BASE}/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={gid}"
        try:
            if not self.http:
                self.http = httpx.AsyncClient(timeout=15, follow_redirects=True)
            resp = await self.http.get(url)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("response", {}).get("player_count")
        except Exception as e:
            logger.debug(f"[steam-monitor] get online count failed gameid={gid}: {e}")
        return None

    async def _push_game_start_render(
        self,
        target: str,
        sid: str,
        player_name: str,
        avatar_url: str,
        gameid: str,
        game_name: str,
    ):
        if not self._game_start_render_enabled():
            return

        try:
            zh_game_name, en_game_name = await self._get_game_names(gameid, game_name)
            online_count = await self._get_game_online_count(gameid)
            font_path = self.get_font_path("NotoSansHans-Regular.otf") or str(
                self.plugin_dir / "fonts" / "NotoSansCJKsc-Regular.otf"
            )

            img_bytes = await render_game_start(
                str(self.data_dir),
                sid,
                player_name,
                avatar_url,
                gameid,
                zh_game_name,
                api_key=str(self.config.get("steam_api_key", "")),
                superpower=None,
                sgdb_api_key=self.SGDB_API_KEY,
                font_path=font_path,
                sgdb_game_name=en_game_name,
                online_count=online_count,
                appid=gameid,
                sgdb_api_base=self.SGDB_API_BASE,
                steam_api_base=self.STEAM_API_BASE,
            )
            with tempfile.NamedTemporaryFile(
                delete=False,
                suffix=".png",
                dir=str(self.data_dir),
            ) as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name

            chain = MessageChain()
            chain.chain = [
                Comp.Plain(text=f"{player_name} 启动 {zh_game_name}"),
                Comp.Image.fromFileSystem(tmp_path),
            ]
            await self.context.send_message(target, chain)
            self._schedule_delayed_unlink(tmp_path, 30)
        except Exception as e:
            logger.warning(
                f"[steam-monitor] push game start render failed sid={sid} gameid={gameid}: {e}"
            )

    def _cache_ttl(self) -> int:
        return max(60, int(self.config.get("cache_ttl_sec", 3600) or 3600))

    def _cache_limit(self, kind: str) -> int:
        if kind == "bytes":
            return max(100, int(self.config.get("cache_max_bytes_items", 512) or 512))
        if kind == "profile_game":
            return max(
                100,
                int(self.config.get("cache_max_profile_game_items", 1024) or 1024),
            )
        return max(100, int(self.config.get("cache_max_icon_items", 1024) or 1024))

    def _cache_get(self, cache: OrderedDict, key: str):
        if key not in cache:
            return None
        ts, val = cache[key]
        if time.time() - ts > self._cache_ttl():
            cache.pop(key, None)
            return None
        cache.move_to_end(key)
        return val

    def _cache_set(self, cache: OrderedDict, key: str, val: Any, kind: str):
        cache[key] = (time.time(), val)
        cache.move_to_end(key)
        while len(cache) > self._cache_limit(kind):
            cache.popitem(last=False)

    def _disk_image_cache_path(self, url: str) -> Path:
        key = hashlib.sha256((url or "").encode("utf-8", errors="ignore")).hexdigest()
        sub = self.image_cache_dir / key[:2]
        sub.mkdir(parents=True, exist_ok=True)
        return sub / f"{key}.bin"

    def _disk_image_cache_get(self, url: str, max_bytes: int) -> bytes | None:
        p = self._disk_image_cache_path(url)
        if not p.exists():
            return None
        try:
            st = p.stat()
            if time.time() - st.st_mtime > self._cache_ttl():
                p.unlink(missing_ok=True)
                return None
            if st.st_size <= 0 or st.st_size > max_bytes:
                p.unlink(missing_ok=True)
                return None
            return p.read_bytes()
        except Exception as e:
            logger.debug(f"[steam-monitor] read disk image cache failed url={url}: {e}")
            return None

    def _disk_image_cache_set(self, url: str, raw: bytes):
        if not raw:
            return
        p = self._disk_image_cache_path(url)
        tmp = p.with_suffix(".tmp")
        try:
            tmp.write_bytes(raw)
            tmp.replace(p)
        except Exception as e:
            logger.debug(f"[steam-monitor] write disk image cache failed url={url}: {e}")
            with contextlib.suppress(Exception):
                tmp.unlink(missing_ok=True)

    def _is_authorized(self, event: AstrMessageEvent) -> bool:
        allow = parse_ids(self.config.get("admin_origins", ""))
        if not allow:
            return True
        return event.unified_msg_origin in allow

    def _get_targets(self) -> List[str]:
        cfg_targets = parse_ids(self.config.get("push_targets", ""))
        legacy_targets = self.state.get("_push_targets", [])
        if not isinstance(legacy_targets, list):
            logger.warning("[steam-monitor] invalid legacy _push_targets type; ignored")
            legacy_targets = []
        legacy_targets = [x for x in legacy_targets if isinstance(x, str)]
        return _dedup_keep_order(cfg_targets + legacy_targets)

    def _set_targets(self, targets: List[str]):
        uniq = _dedup_keep_order(targets)
        self.config["push_targets"] = ",".join(uniq)
        self._save_config_safe()

    async def _fetch_players(self, steam_ids: List[str]) -> List[Dict[str, Any]]:
        """获取玩家数据，带重试机制"""
        api_key = self.config.get("steam_api_key", "")
        if not api_key:
            raise RuntimeError("未配置 steam_api_key")

        if not self.http:
            self.http = httpx.AsyncClient(timeout=15, follow_redirects=True)

        uniq_ids = _dedup_keep_order(steam_ids)

        batch_size = min(
            100, max(1, int(self.config.get("steam_batch_size", 100) or 100))
        )
        
        max_retries = 3
        retry_delay = 1.0
        
        players: List[Dict[str, Any]] = []
        for i in range(0, len(uniq_ids), batch_size):
            chunk = uniq_ids[i : i + batch_size]
            params = {"key": api_key, "steamids": ",".join(chunk)}
            
            for attempt in range(max_retries + 1):
                try:
                    r = await self.http.get(STEAM_SUMMARY_API, params=params)
                    r.raise_for_status()
                    data = r.json()
                    players.extend(data.get("response", {}).get("players", []))
                    break  # 成功，跳出重试循环
                except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
                    if attempt < max_retries:
                        logger.warning(
                            f"[steam-monitor] fetch players network error (attempt {attempt + 1}/{max_retries + 1}): {e}"
                        )
                        await asyncio.sleep(retry_delay * (2 ** attempt))  # 指数退避
                        continue
                    else:
                        logger.error(
                            f"[steam-monitor] fetch players failed after {max_retries + 1} attempts: {e}"
                        )
                        raise RuntimeError(
                            f"无法连接 Steam API（已重试 {max_retries} 次）：{type(e).__name__}"
                        ) from e

        return players

    def _order_players_by_ids(
        self, players: List[Dict[str, Any]], steam_ids: List[str]
    ) -> List[Dict[str, Any]]:
        if not players:
            return []

        desired = _dedup_keep_order(steam_ids)
        player_map = {str(p.get("steamid", "")): p for p in players}

        ordered = [player_map[sid] for sid in desired if sid in player_map]

        desired_set = set(desired)
        rest = [
            p
            for p in players
            if str(p.get("steamid", "")) not in desired_set
        ]
        rest.sort(
            key=lambda p: (
                str(p.get("personaname", "")).lower(),
                str(p.get("steamid", "")),
            )
        )

        return ordered + rest

    def _is_private_host(self, host: str) -> bool:
        host = (host or "").strip().lower()
        if not host:
            return True
        if host in {"localhost", "localhost.localdomain"}:
            return True
        try:
            ip = ipaddress.ip_address(host)
            return (
                ip.is_loopback
                or ip.is_private
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            )
        except Exception:
            # 域名层面的基础阻断（可按需扩展白名单）
            bad_suffixes = (
                ".local",
                ".lan",
                ".home",
                ".internal",
                ".corp",
                ".localhost",
            )
            return host.endswith(bad_suffixes)

    def _remote_host_allow_domains(self) -> List[str]:
        # 默认允许的公开域名后缀；可通过配置追加
        defaults = [
            "steamcommunity.com",
            "steamstatic.com",
            "steampowered.com",
            "akamaihd.net",
            "images.weserv.nl",
        ]
        custom = parse_ids(self.config.get("remote_host_allowlist", ""))
        return [x.strip().lower() for x in (defaults + custom) if str(x).strip()]

    def _is_host_in_domains(self, host: str, domains: List[str]) -> bool:
        host = (host or "").strip().lower()
        if not host:
            return False
        for d in domains:
            if host == d or host.endswith("." + d):
                return True
        return False

    def _with_image_proxy(self, url: str, proxy_prefix: str) -> str:
        prefix = (proxy_prefix or "").strip()
        if not prefix:
            logger.debug(f"[steam-monitor] no image proxy prefix, use origin url={url}")
            return url

        # 仅允许 http/https 且禁止本地回环/文件协议，避免恶意中转配置
        try:
            parsed = urlparse(prefix)
            scheme = (parsed.scheme or "").lower()
            host = (parsed.hostname or "").lower()
            logger.debug(
                f"[steam-monitor] validate image proxy prefix={prefix} scheme={scheme} host={host}"
            )
            if scheme and scheme not in ("http", "https"):
                logger.warning(f"[steam-monitor] invalid proxy scheme: {scheme}")
                return url
            if self._is_private_host(host):
                logger.warning("[steam-monitor] blocked private/local proxy host")
                return url
        except Exception as e:
            logger.warning(f"[steam-monitor] invalid proxy prefix: {e}")
            return url

        encoded = quote(url, safe="")
        if "{url}" in prefix:
            proxied = prefix.replace("{url}", encoded)
            logger.debug(
                f"[steam-monitor] proxy url built with '{{url}}': src={url} proxied={proxied}"
            )
            return proxied
        if "%s" in prefix:
            try:
                proxied = prefix % encoded
                logger.debug(
                    f"[steam-monitor] proxy url built with '%s': src={url} proxied={proxied}"
                )
                return proxied
            except Exception as e:
                logger.warning(f"[steam-monitor] invalid proxy format: {e}")
                return url
        proxied = prefix + encoded
        logger.debug(
            f"[steam-monitor] proxy url built by concat: src={url} proxied={proxied}"
        )
        return proxied

    async def _is_allowed_remote_url(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
            scheme = (parsed.scheme or "").lower()
            host = (parsed.hostname or "").lower()
            logger.debug(
                f"[steam-monitor] validate remote url={url} scheme={scheme} host={host}"
            )
            if scheme not in ("http", "https"):
                logger.warning(
                    f"[steam-monitor] blocked remote url by scheme: url={url} scheme={scheme}"
                )
                return False
            if self._is_private_host(host):
                logger.warning(
                    f"[steam-monitor] blocked remote url by private/local host: url={url} host={host}"
                )
                return False

            allow_domains = self._remote_host_allow_domains()
            bypass_dns_private = bool(
                self.config.get("allow_dns_private_for_allow_domains", True)
            )
            if bypass_dns_private and self._is_host_in_domains(host, allow_domains):
                logger.debug(
                    "[steam-monitor] skip dns-private check for allow-domain: "
                    f"url={url} host={host}"
                )
            else:
                if await self._is_host_resolved_private(host):
                    logger.warning(
                        f"[steam-monitor] blocked remote url by dns-resolved private ip: url={url} host={host}"
                    )
                    return False

            strict = bool(self.config.get("strict_remote_host", False))
            if not strict:
                logger.debug(
                    f"[steam-monitor] remote url allowed (strict disabled): url={url} host={host}"
                )
                return True

            ok = self._is_host_in_domains(host, allow_domains)
            logger.debug(
                f"[steam-monitor] strict allowlist check host={host} allowed={ok} allow={allow_domains}"
            )
            return ok
        except Exception as e:
            logger.warning(
                f"[steam-monitor] validate remote url exception url={url}: {e}"
            )
            return False

    async def _fetch_url_bytes(
        self,
        url: str,
        proxy_prefix: str = "",
        allowed_types: tuple[str, ...] = ("image/", "application/json"),
        max_bytes: int = 3 * 1024 * 1024,
        headers: Dict[str, str] | None = None,
    ) -> bytes | None:
        if not url:
            logger.debug("[steam-monitor] _fetch_url_bytes skipped: empty url")
            return None

        cached = self._cache_get(self.bytes_cache, url)
        if cached is not None:
            logger.debug(
                f"[steam-monitor] bytes cache hit: url={url} size={len(cached)}"
            )
            return cached
        logger.debug(f"[steam-monitor] bytes cache miss: url={url}")

        image_fetch = any((x or "").startswith("image/") for x in allowed_types)
        if image_fetch:
            disk_cached = self._disk_image_cache_get(url, max_bytes=max_bytes)
            if disk_cached is not None:
                self._cache_set(self.bytes_cache, url, disk_cached, "bytes")
                logger.info(
                    f"[steam-monitor] disk image cache hit: url={url} size={len(disk_cached)}"
                )
                return disk_cached

        if not self.http:
            self.http = httpx.AsyncClient(timeout=15, follow_redirects=True)

        candidates = [url]
        if proxy_prefix:
            candidates.append(self._with_image_proxy(url, proxy_prefix))
        logger.debug(
            f"[steam-monitor] fetch candidates for url={url}: count={len(candidates)} candidates={candidates}"
        )

        max_redirects = max(0, int(self.config.get("max_redirects", 3) or 3))
        logger.debug(
            "[steam-monitor] fetch options: "
            f"url={url} allowed_types={allowed_types} max_bytes={max_bytes} max_redirects={max_redirects}"
        )
        fail_reasons: List[str] = []

        for origin in candidates:
            current = origin
            logger.debug(f"[steam-monitor] start fetch origin={origin}")
            if not await self._is_allowed_remote_url(current):
                logger.debug(f"[steam-monitor] blocked remote url: {current}")
                fail_reasons.append(f"blocked:{current}")
                continue

            try:
                for hop in range(max_redirects + 1):
                    logger.debug(
                        f"[steam-monitor] request hop={hop}/{max_redirects} current={current}"
                    )
                    async with self.http.stream(
                        "GET", current, follow_redirects=False, headers=headers
                    ) as resp:
                        logger.debug(
                            "[steam-monitor] response received: "
                            f"url={current} status={resp.status_code} "
                            f"content-type={resp.headers.get('content-type', '')} "
                            f"content-length={resp.headers.get('content-length', '')}"
                        )
                        # redirect handling with per-hop validation
                        if resp.status_code in (301, 302, 303, 307, 308):
                            location = resp.headers.get("location")
                            if not location:
                                logger.warning(
                                    f"[steam-monitor] redirect without location: from={current} status={resp.status_code}"
                                )
                                fail_reasons.append(
                                    f"redirect-no-location:{current}:status={resp.status_code}"
                                )
                                break
                            next_url = urljoin(str(resp.request.url), location)
                            logger.debug(
                                f"[steam-monitor] redirect: from={current} to={next_url} status={resp.status_code}"
                            )
                            if not await self._is_allowed_remote_url(next_url):
                                logger.warning(
                                    f"[steam-monitor] blocked redirect target: {next_url}"
                                )
                                fail_reasons.append(f"blocked-redirect:{next_url}")
                                break
                            current = next_url
                            continue

                        if resp.status_code != 200:
                            logger.warning(
                                f"[steam-monitor] fetch non-200: url={current} status={resp.status_code}"
                            )
                            fail_reasons.append(
                                f"non-200:{current}:status={resp.status_code}"
                            )
                            break

                        ctype = (resp.headers.get("content-type") or "").lower()
                        if allowed_types and not any(
                            ctype.startswith(x) for x in allowed_types
                        ):
                            logger.warning(
                                f"[steam-monitor] content-type not allowed: url={current} content-type={ctype} allowed={allowed_types}"
                            )
                            fail_reasons.append(
                                f"bad-content-type:{current}:ctype={ctype}"
                            )
                            break

                        clen = resp.headers.get("content-length")
                        if clen:
                            with contextlib.suppress(Exception):
                                if int(clen) > max_bytes:
                                    logger.warning(
                                        f"[steam-monitor] content-length too large: url={current} content-length={clen} max={max_bytes}"
                                    )
                                    fail_reasons.append(
                                        f"too-large-header:{current}:content-length={clen}"
                                    )
                                    break

                        buf = bytearray()
                        async for chunk in resp.aiter_bytes(65536):
                            buf.extend(chunk)
                            if len(buf) > max_bytes:
                                logger.warning(
                                    f"[steam-monitor] streamed bytes exceeded limit: url={current} size={len(buf)} max={max_bytes}"
                                )
                                fail_reasons.append(
                                    f"too-large-stream:{current}:size={len(buf)}"
                                )
                                buf = bytearray()
                                break

                        if not buf:
                            logger.warning(
                                f"[steam-monitor] empty body after fetch: url={current}"
                            )
                            fail_reasons.append(f"empty-body:{current}")
                            break

                        raw = bytes(buf)
                        self._cache_set(self.bytes_cache, url, raw, "bytes")
                        if image_fetch:
                            self._disk_image_cache_set(url, raw)
                        logger.debug(
                            f"[steam-monitor] fetch success: url={current} origin_key={url} size={len(raw)}"
                        )
                        return raw
                    logger.debug(
                        f"[steam-monitor] stop trying current origin after hop={hop} current={current}"
                    )
                    break
            except Exception as e:
                logger.debug(
                    f"[steam-monitor] fetch image bytes failed: {origin} err={e}"
                )
                fail_reasons.append(f"exception:{origin}:{type(e).__name__}:{e}")

        reason_text = " | ".join(fail_reasons[-6:]) if fail_reasons else "unknown"
        logger.warning(
            f"[steam-monitor] all fetch candidates failed: url={url} reasons={reason_text}"
        )
        return None

    async def _get_game_icon_url(self, appid: str) -> str | None:
        if not appid:
            return None
        cached = self._cache_get(self.icon_url_cache, appid)
        if cached is not None:
            logger.debug(f"[steam-monitor] game icon url cache hit: appid={appid}")
            return cached
        logger.debug(f"[steam-monitor] game icon url cache miss: appid={appid}")

        api = f"https://store.steampowered.com/api/appdetails?appids={appid}&l=schinese"
        logger.debug(f"[steam-monitor] fetch game icon metadata appid={appid} api={api}")
        try:
            raw = await self._fetch_url_bytes(
                api,
                allowed_types=("application/json", "text/json", "text/plain"),
                max_bytes=512 * 1024,
            )
            if not raw:
                logger.warning(
                    f"[steam-monitor] game icon metadata fetch empty appid={appid}"
                )
                icon_url = await self._get_sgdb_game_icon_url(appid)
                if icon_url:
                    self._cache_set(self.icon_url_cache, appid, icon_url, "icon")
                return icon_url

            data = json.loads(raw.decode("utf-8", errors="ignore"))
            node = data.get(str(appid), {})
            if not node.get("success"):
                logger.warning(
                    f"[steam-monitor] game metadata success=false appid={appid}"
                )
                icon_url = await self._get_sgdb_game_icon_url(appid)
                if icon_url:
                    self._cache_set(self.icon_url_cache, appid, icon_url, "icon")
                return icon_url
            app = node.get("data", {})
            icon_url = app.get("header_image") or app.get("capsule_image")
            if not icon_url:
                icon_url = await self._get_sgdb_game_icon_url(appid)
            if icon_url:
                self._cache_set(self.icon_url_cache, appid, icon_url, "icon")
                logger.debug(
                    f"[steam-monitor] game icon url resolved appid={appid} url={icon_url}"
                )
            else:
                logger.warning(
                    f"[steam-monitor] game icon url missing in metadata appid={appid}"
                )
            return icon_url
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
            logger.warning(
                f"[steam-monitor] game icon metadata fetch network error appid={appid}: {type(e).__name__}"
            )
            icon_url = await self._get_sgdb_game_icon_url(appid)
            if icon_url:
                self._cache_set(self.icon_url_cache, appid, icon_url, "icon")
            return icon_url
        except Exception as e:
            logger.warning(f"[steam-monitor] parse game icon failed appid={appid}: {e}")
            icon_url = await self._get_sgdb_game_icon_url(appid)
            if icon_url:
                self._cache_set(self.icon_url_cache, appid, icon_url, "icon")
            return icon_url

    async def _get_sgdb_game_icon_url(self, appid: str) -> str | None:
        """使用 SteamGridDB 为状态图提供横幅封面兜底。"""
        gid = str(appid or "").strip()
        if not gid or not self.SGDB_API_KEY:
            return None

        headers = {"Authorization": f"Bearer {self.SGDB_API_KEY}"}
        try:
            if not self.http:
                self.http = httpx.AsyncClient(timeout=15, follow_redirects=True)

            game_url = f"{self.SGDB_API_BASE}/api/v2/games/steam/{gid}"
            resp_game = await self.http.get(game_url, headers=headers)
            if resp_game.status_code != 200:
                return None
            game_data = resp_game.json()
            if not game_data.get("success") or not game_data.get("data"):
                return None

            sgdb_game_id = game_data["data"].get("id")
            if not sgdb_game_id:
                return None

            grid_url = (
                f"{self.SGDB_API_BASE}/api/v2/grids/game/{sgdb_game_id}"
                "?dimensions=460x215&type=static&limit=1"
            )
            resp_grid = await self.http.get(grid_url, headers=headers)
            if resp_grid.status_code != 200:
                return None
            grid_data = resp_grid.json()
            if not grid_data.get("success") or not grid_data.get("data"):
                return None

            first = grid_data["data"][0] if grid_data["data"] else None
            if not isinstance(first, dict):
                return None
            return str(first.get("url") or "").strip() or None
        except Exception as e:
            logger.debug(f"[steam-monitor] sgdb icon fallback failed appid={gid}: {e}")
            return None

    def _profile_game_fallback_enabled(self) -> bool:
        return bool(self.config.get("enable_profile_game_fallback", True))

    def _normalize_game_name(self, game_name: str) -> str:
        """标准化游戏名称，处理特殊状态文案"""
        game_name = (game_name or "").strip()
        if not game_name:
            return ""

        # 精确匹配规则
        exact_mappings = {
            "当前正在游戏": "",
            "当前在线": "",
            "VR 在线": "VR",
            "非 Steam 游戏中": "非 Steam 游戏",
        }

        if game_name in exact_mappings:
            return exact_mappings[game_name]

        return game_name

    def _extract_profile_in_game_header_from_html(self, page: str) -> str:
        """从 HTML 中直接提取 profile_in_game_header 的游戏名。"""
        if not page:
            return ""

        # 简单正则提取 profile_in_game_header class 中的内容
        pattern = re.compile(
            r'<(?P<tag>[a-z0-9]+)[^>]*class\s*=\s*["\']?[^"\']*profile_in_game_header[^"\']*["\']?[^>]*>(?P<body>.*?)</\1>',
            re.I | re.S,
        )

        with contextlib.suppress(Exception):
            match = pattern.search(page)
            if match:
                raw_text = html.unescape(match.group("body"))
                raw_text = re.sub(r"<[^>]+>", " ", raw_text)
                game_name = re.sub(r"\s+", " ", raw_text).strip()
                if game_name:
                    return self._normalize_game_name(game_name)

        return ""

    async def _get_profile_game_name_fallback(self, steamid: str) -> str:
        """当 API 未返回 gameextrainfo 时，从个人资料 HTML 状态区读取游戏名（profile_in_game_header）。"""
        steamid = (steamid or "").strip()
        if not steamid:
            return ""

        cached = self._cache_get(self.profile_game_cache, steamid)
        if cached is not None:
            return str(cached or "").strip()

        profile_urls = [
            f"https://steamcommunity.com/profiles/{steamid}/?l=schinese",
        ]
        profile_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        for profile_url in profile_urls:
            raw = await self._fetch_url_bytes(
                profile_url,
                allowed_types=("text/", "application/xhtml+xml"),
                max_bytes=1024 * 1024,
                headers=profile_headers,
            )
            if not raw:
                continue

            try:
                page = raw.decode("utf-8", errors="ignore")
                game_name = self._extract_profile_in_game_header_from_html(page)

                if game_name:
                    self._cache_set(
                        self.profile_game_cache, steamid, game_name, "profile_game"
                    )
                    logger.debug(
                        f"[steam-monitor] profile fallback game name resolved steamid={steamid} game={game_name} url={profile_url}"
                    )
                    return game_name
                else:
                    logger.debug(
                        f"[steam-monitor] profile fallback: profile_in_game_header not found steamid={steamid} url={profile_url}"
                    )
            except Exception as e:
                logger.debug(
                    f"[steam-monitor] profile fallback parse failed steamid={steamid} url={profile_url}: {e}"
                )

        return ""

    async def _enrich_players_with_profile_game_fallback(
        self, players: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """统一补全非 Steam 游戏名，确保轮询推送与手动状态图显示一致。"""
        if not players or not self._profile_game_fallback_enabled():
            return players

        attempted = 0
        filled = 0
        for p in players:
            st = int(p.get("personastate", 0) or 0)
            game = (p.get("gameextrainfo", "") or "").strip()
            sid = str(p.get("steamid", "") or "").strip()
            if st == 0 or game or not sid:
                continue

            attempted += 1
            fallback_game = await self._get_profile_game_name_fallback(sid)
            if fallback_game:
                p["gameextrainfo"] = fallback_game
                filled += 1

        if attempted:
            logger.info(
                f"[steam-monitor] profile fallback enrichment attempted={attempted} filled={filled}"
            )
        return players

    def _process_image_bytes(
        self, raw: bytes, size: tuple[int, int], circle: bool = False
    ) -> Image.Image | None:
        try:
            logger.debug(
                f"[steam-monitor] decode image start bytes={len(raw)} target_size={size} circle={circle}"
            )
            with Image.open(BytesIO(raw)) as opened:
                max_pixels = max(
                    512 * 512,
                    int(self.config.get("max_image_pixels", 4_000_000) or 4_000_000),
                )
                logger.debug(
                    "[steam-monitor] decode image metadata: "
                    f"width={opened.width} height={opened.height} mode={opened.mode} max_pixels={max_pixels}"
                )
                if opened.width * opened.height > max_pixels:
                    logger.warning(
                        f"[steam-monitor] image too large: {opened.width}x{opened.height}"
                    )
                    return None
                img = opened.convert("RGBA")
            img = img.resize(size, Image.Resampling.LANCZOS)
            if circle:
                img = circle_crop(img)
            logger.debug(
                f"[steam-monitor] decode image success target_size={size} circle={circle}"
            )
            return img
        except Exception as e:
            logger.warning(f"[steam-monitor] decode image failed: {e}")
            return None

    async def _fallback_load_cached_image(
        self, url: str, size: tuple[int, int], circle: bool = False
    ) -> Image.Image | None:
        """当网络失败时，尝试从缓存中恢复相同尺寸的图片"""
        logger.debug(
            f"[steam-monitor] fallback to cached image url={url} size={size} circle={circle}"
        )
        cached_raw = self._cache_get(self.bytes_cache, url)
        if cached_raw is None:
            max_bytes = max(
                256 * 1024,
                int(
                    self.config.get("max_image_bytes", 3 * 1024 * 1024)
                    or 3 * 1024 * 1024
                ),
            )
            cached_raw = self._disk_image_cache_get(url, max_bytes=max_bytes)
            if cached_raw is not None:
                self._cache_set(self.bytes_cache, url, cached_raw, "bytes")
                logger.info(f"[steam-monitor] using disk image cache for url={url}")
            else:
                logger.debug(f"[steam-monitor] no cached bytes for url={url}")
                return None
        
        logger.info(
            f"[steam-monitor] using cached image for url={url} (network error fallback)"
        )
        img = await asyncio.to_thread(self._process_image_bytes, cached_raw, size, circle)
        if img is None:
            logger.warning(f"[steam-monitor] fallback decode failed url={url}")
        else:
            logger.debug(f"[steam-monitor] fallback image success url={url}")
        return img

    async def _load_remote_image(
        self,
        url: str,
        size: tuple[int, int],
        proxy_prefix: str = "",
        circle: bool = False,
    ) -> Image.Image | None:
        logger.debug(
            f"[steam-monitor] load remote image start url={url} size={size} circle={circle} proxy_prefix={proxy_prefix}"
        )
        try:
            raw = await self._fetch_url_bytes(
                url,
                proxy_prefix=proxy_prefix,
                allowed_types=("image/",),
                max_bytes=max(
                    256 * 1024,
                    int(
                        self.config.get("max_image_bytes", 3 * 1024 * 1024)
                        or 3 * 1024 * 1024
                    ),
                ),
            )
            if not raw:
                logger.warning(
                    f"[steam-monitor] load remote image failed to fetch bytes url={url}"
                )
                # 尝试从缓存中恢复
                return await self._fallback_load_cached_image(url, size, circle)
            img = await asyncio.to_thread(self._process_image_bytes, raw, size, circle)
            if img is None:
                logger.warning(f"[steam-monitor] load remote image decode failed url={url}")
            else:
                logger.debug(f"[steam-monitor] load remote image success url={url}")
            return img
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
            logger.warning(
                f"[steam-monitor] load remote image network error url={url}: {type(e).__name__}"
            )
            # 网络错误时尝试从缓存中恢复
            return await self._fallback_load_cached_image(url, size, circle)

    async def _prepare_assets(
        self, players: List[Dict[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        proxy_prefix = self.config.get(
            "image_proxy_prefix", "https://images.weserv.nl/?url="
        )
        concurrency = max(1, int(self.config.get("asset_concurrency", 6) or 6))
        logger.info(
            f"[steam-monitor] prepare assets start players={len(players)} proxy_prefix={proxy_prefix} concurrency={concurrency}"
        )
        sem = asyncio.Semaphore(concurrency)

        async def one_player(p: Dict[str, Any]):
            sid = str(p.get("steamid", ""))
            avatar_url = p.get("avatarfull") or p.get("avatarmedium") or p.get("avatar")
            gameid = str(p.get("gameid", "") or "").strip()
            logger.debug(
                f"[steam-monitor] prepare one player sid={sid} avatar_url={avatar_url} gameid={gameid}"
            )

            async with sem:
                avatar = await self._load_remote_image(
                    avatar_url or "", (64, 64), proxy_prefix, circle=True
                )
            if avatar is None:
                logger.warning(
                    f"[steam-monitor] avatar load failed sid={sid} avatar_url={avatar_url}"
                )
            else:
                logger.debug(f"[steam-monitor] avatar load ok sid={sid}")

            game_icon = None
            if gameid:
                async with sem:
                    icon_url = await self._get_game_icon_url(gameid)
                logger.debug(
                    f"[steam-monitor] game icon metadata sid={sid} gameid={gameid} icon_url={icon_url}"
                )
                if icon_url:
                    async with sem:
                        game_icon = await self._load_remote_image(
                            icon_url, (180, 68), proxy_prefix
                        )
                    if game_icon is None:
                        logger.warning(
                            f"[steam-monitor] game icon load failed sid={sid} gameid={gameid} icon_url={icon_url}"
                        )
                    else:
                        logger.debug(
                            f"[steam-monitor] game icon load ok sid={sid} gameid={gameid}"
                        )

            return sid, {"avatar": avatar, "game_icon": game_icon}

        pairs = await asyncio.gather(
            *(one_player(p) for p in players), return_exceptions=False
        )
        logger.info(
            f"[steam-monitor] prepare assets done players={len(players)} pairs={len(pairs)}"
        )
        return dict(pairs)

    def _format_game_duration(self, seconds: int) -> str:
        """将秒数格式化为游戏时长显示格式 HH时MM分（不包含数字0）"""
        if seconds < 0:
            return ""
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60

        if hours > 0:
            return f"{hours}h{minutes}m"
        elif minutes > 0:
            return f"{minutes}m"
        else:
            return ""

    def _get_game_duration_for_player(
        self, sid: str, now: datetime, target: str = ""
    ) -> str:
        """获取当前玩家的游戏时长，返回格式化字符串；如果没有在玩游戏则返回空"""
        record: Dict[str, Any] = {}
        if target:
            target_state = self._get_target_player_state(target)
            data = target_state.get(sid, {})
            if isinstance(data, dict):
                record = data
        if not record:
            data = self.state.get(sid, {})
            if isinstance(data, dict):
                record = data
        if not isinstance(record, dict):
            return ""

        st = int(record.get("personastate", 0) or 0)
        game = (record.get("gameextrainfo", "") or "").strip()
        game_start_ts = record.get("game_start_ts")
        game_accum_seconds = self._safe_int(record.get("game_accum_seconds", 0), 0)

        # 只在可计时状态且有游戏名且已记录开始时间才显示时长
        if not self._is_duration_countable_state(st) or not game or game_start_ts is None:
            return ""

        duration_sec = game_accum_seconds + self._session_seconds_in_current_cycle(
            game_start_ts,
            now,
            self._daily_cycle_start_utc8(now),
        )
        if duration_sec > 0:
            return self._format_game_duration(duration_sec)

        return ""

    def _get_daily_game_duration_for_player(
        self, sid: str, now: datetime, target: str = ""
    ) -> str:
        record: Dict[str, Any] = {}
        if target:
            target_state = self._get_target_player_state(target)
            data = target_state.get(sid, {})
            if isinstance(data, dict):
                record = data
        if not record:
            data = self.state.get(sid, {})
            if isinstance(data, dict):
                record = data

        if not isinstance(record, dict):
            return ""

        cycle_key = self._daily_cycle_key_utc8(now)
        total_sec = self._safe_int(record.get("daily_game_seconds", 0), 0)
        if str(record.get("daily_cycle_key", "")) != cycle_key:
            total_sec = 0

        st = self._safe_int(record.get("personastate", 0), 0)
        game = (record.get("gameextrainfo", "") or "").strip()
        if self._is_duration_countable_state(st) and game:
            total_sec += self._session_seconds_in_current_cycle(
                record.get("game_start_ts"),
                now,
                self._daily_cycle_start_utc8(now),
            )

        if total_sec <= 0:
            return ""
        return self._format_game_duration(total_sec)

    def _get_display_game_duration_for_player(
        self, sid: str, now: datetime, target: str = ""
    ) -> str:
        record: Dict[str, Any] = {}
        if target:
            target_state = self._get_target_player_state(target)
            data = target_state.get(sid, {})
            if isinstance(data, dict):
                record = data
        if not record:
            data = self.state.get(sid, {})
            if isinstance(data, dict):
                record = data
        if not isinstance(record, dict):
            return ""

        st = self._safe_int(record.get("personastate", 0), 0)
        game = (record.get("gameextrainfo", "") or "").strip()
        if self._is_duration_countable_state(st) and game:
            return self._get_game_duration_for_player(sid, now, target)
        return self._get_daily_game_duration_for_player(sid, now, target)

    def _build_status_image(
        self,
        players: List[Dict[str, Any]],
        assets: Dict[str, Dict[str, Any]],
        target: str = "",
    ) -> str:
        w = 800
        row_h = 110
        top = 56
        h = top + row_h * max(1, len(players)) + 20

        img = Image.new("RGB", (w, h), (22, 26, 31))
        draw = ImageDraw.Draw(img)

        font_text = safe_font(24, self.plugin_dir)
        font_small = safe_font(18, self.plugin_dir)

        now = datetime.now()
        now_text = now.strftime("%Y-%m-%d %H:%M:%S")
        box = draw.textbbox((0, 0), now_text, font=font_small)
        text_w = box[2] - box[0]
        draw.text(
            (w - 24 - text_w, 18), now_text, fill=(160, 170, 180), font=font_small
        )

        y = top
        for p in players:
            sid = str(p.get("steamid", ""))
            aset = assets.get(sid, {})

            name = p.get("personaname", "Unknown")
            state = int(p.get("personastate", 0))
            game = (p.get("gameextrainfo", "") or "").strip()

            draw.rounded_rectangle(
                (20, y, w - 20, y + 96), radius=14, fill=(35, 41, 48)
            )

            avatar = aset.get("avatar")
            if avatar is not None:
                img.paste(avatar, (34, y + 16), avatar)
            else:
                color = (67, 160, 71) if state != 0 else (120, 130, 140)
                draw.ellipse((34, y + 28, 54, y + 48), fill=color)

            name_x = 112
            name_y = y + 18
            draw.text((name_x, name_y), name, fill=(240, 240, 240), font=font_text)
            name_box = draw.textbbox((0, 0), name, font=font_text)
            name_w = name_box[2] - name_box[0]
            line2 = game if game else persona_text(state)
            draw.text((112, y + 54), line2, fill=(170, 180, 190), font=font_small)

            # 显示游戏时长
            game_duration = self._get_display_game_duration_for_player(sid, now, target)
            duration_x = min(name_x + name_w + 50, w - 230)
            if game_duration:
                # 统一显示在姓名右侧 50px，并限制到右侧图标区域之外
                duration_text = game_duration
                draw.text((duration_x, name_y + 6), duration_text, fill=(100, 150, 200), font=font_small)

            # 在线（state=1）时仅显示绿色圆点，避免与下方状态文案重复。
            if state == 1:
                dot_x = name_x + name_w + 15
                dot_y = name_y + 13
                draw.ellipse((dot_x, dot_y, dot_x + 12, dot_y + 12), fill=(67, 200, 88))

            game_icon = aset.get("game_icon")
            if game_icon is not None:
                img.paste(game_icon, (w - 220, y + 14), game_icon)

            y += row_h

        out = (
            self.data_dir
            / f"steam_status_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        )
        try:
            img.save(out)
            return str(out)
        finally:
            img.close()

    async def _render_status_image(
        self, players: List[Dict[str, Any]], target: str = ""
    ) -> str:
        logger.info(f"[steam-monitor] render status image start players={len(players)}")
        assets = await self._prepare_assets(players)
        out = await asyncio.to_thread(self._build_status_image, players, assets, target)
        logger.info(f"[steam-monitor] render status image done output={out}")
        return out

    async def _push_image(self, umo: str, text: str, image_path: str):
        logger.info(
            f"[steam-monitor] push image start target={umo} image_path={image_path} text_len={len(text or '')}"
        )
        clean_text = "\n".join(
            line.strip() for line in (text or "").splitlines() if line.strip()
        )

        # 先发文字，再发图片，避免图文混发导致展示样式不符合预期
        if clean_text:
            text_chain = MessageChain()
            text_chain.chain = [Comp.Plain(text=clean_text)]
            await self.context.send_message(umo, text_chain)

        image_chain = MessageChain()
        image_chain.chain = [Comp.Image.fromFileSystem(image_path)]
        await self.context.send_message(umo, image_chain)
        logger.info(f"[steam-monitor] push image success target={umo} image_path={image_path}")

    async def _push_text(self, umo: str, text: str):
        clean_text = "\n".join(
            line.strip() for line in (text or "").splitlines() if line.strip()
        )
        if not clean_text:
            return
        chain = MessageChain()
        chain.chain = [Comp.Plain(text=clean_text)]
        await self.context.send_message(umo, chain)

    def _compute_next_interval(self, steam_ids: List[str], default_sec: int) -> int:
        # 基于完整时间集合（配置 ID + state）计算，而不是仅 API 返回列表
        all_ids = _dedup_keep_order(
            sid
            for sid in (steam_ids + list(self.state.keys()))
            if not (isinstance(sid, str) and sid.startswith("_"))
        )

        any_online = False
        offline_minutes_max = 0.0
        for sid in all_ids:
            record = self.state.get(sid, {})
            if not isinstance(record, dict):
                continue
            st = int(record.get("personastate", 0) or 0)
            if st != 0:
                any_online = True
                break
            off_since = parse_iso(record.get("offline_since", ""))
            if off_since:
                mins = (datetime.now() - off_since).total_seconds() / 60.0
                if mins > offline_minutes_max:
                    offline_minutes_max = mins

        if any_online:
            return max(10, default_sec)
        if offline_minutes_max >= 30:
            return 600
        if offline_minutes_max >= 10:
            return 300
        return max(10, default_sec)

    async def _poll_loop(self):
        await asyncio.sleep(3)
        while not self._stop:
            image_path = None
            try:
                # 获取全局 steam_ids 和推送目标
                global_steam_ids = parse_ids(self.config.get("steam_ids", ""))
                default_interval = int(self.config.get("poll_interval_sec", 60) or 60)
                targets = self._get_targets()

                if not global_steam_ids and not any(
                    self._get_group_steam_ids(target) for target in targets
                ):
                    await asyncio.sleep(max(30, default_interval))
                    continue

                # 为每个 target 单独处理一次（需要使用其对应的 steam_ids）
                for target in targets:
                    try:
                        # 获取该 target 对应的 steam_ids，优先使用群级别，否则用全局
                        group_ids = self._get_group_steam_ids(target)
                        steam_ids = group_ids if group_ids is not None else global_steam_ids

                        if not steam_ids:
                            logger.debug(
                                f"[steam-monitor] skip poll for target={target} (no steam_ids)"
                            )
                            continue

                        await self._poll_for_target(target, steam_ids, default_interval)
                    except Exception as e:
                        logger.error(
                            f"[steam-monitor] poll for target {target} failed: {e}"
                        )

                next_sleep = self._compute_next_interval(
                    global_steam_ids, default_interval
                )
                logger.info(f"[steam-monitor] next poll in {next_sleep}s")
                await asyncio.sleep(next_sleep)
            except asyncio.CancelledError:
                break
            except RuntimeError as e:
                logger.error(f"[steam-monitor] poll error: {e}")
                if "steam_api_key" in str(e):
                    await asyncio.sleep(
                        max(
                            300,
                            int(self.config.get("missing_key_sleep_sec", 600) or 600),
                        )
                    )
                else:
                    await asyncio.sleep(30)
            except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
                logger.warning(
                    f"[steam-monitor] poll network error (will retry): {type(e).__name__}: {e}"
                )
                await asyncio.sleep(60)  # 网络错误等更久再重试
            except Exception as e:
                logger.error(f"[steam-monitor] poll error: {e}")
                await asyncio.sleep(30)
            finally:
                if image_path:
                    with contextlib.suppress(Exception):
                        Path(image_path).unlink(missing_ok=True)

    async def _poll_for_target(
        self, target: str, steam_ids: List[str], default_interval: int
    ):
        """为单个 target 执行一次轮询"""
        image_path = None
        try:
            players = await self._fetch_players(steam_ids)
            players = await self._enrich_players_with_profile_game_fallback(players)
            player_map = {str(p.get("steamid", "")): p for p in players}
            target_state = self._get_target_player_state(target)

            events: List[str] = []
            event_types: set[str] = set()
            now_dt = datetime.now()
            now = now_dt.isoformat(timespec="seconds")
            cycle_key = self._daily_cycle_key_utc8(now_dt)
            cycle_start = self._daily_cycle_start_utc8(now_dt)
            flap_suppress_sec = self._presence_flap_suppress_min() * 60
            for sid in steam_ids:
                p = player_map.get(sid)
                if p is None:
                    prev_record = target_state.get(sid, {})
                    prev = prev_record.get("personastate")
                    prev_game = (prev_record.get("gameextrainfo", "") or "").strip()
                    prev_gameid = str(prev_record.get("gameid", "") or "").strip()
                    prev_game_start_ts = prev_record.get("game_start_ts")
                    prev_game_accum_seconds = self._safe_int(
                        prev_record.get("game_accum_seconds", 0), 0
                    )

                    daily_game_seconds = self._safe_int(
                        prev_record.get("daily_game_seconds", 0), 0
                    )
                    if str(prev_record.get("daily_cycle_key", "")) != cycle_key:
                        daily_game_seconds = 0
                    daily_game_seconds += prev_game_accum_seconds
                    daily_game_seconds += self._session_seconds_in_current_cycle(
                        prev_game_start_ts,
                        now_dt,
                        cycle_start,
                    )

                    next_record = {
                        "personaname": prev_record.get("personaname", sid),
                        "personastate": 0,
                        "gameid": "",
                        "gameextrainfo": "",
                        "offline_since": prev_record.get("offline_since", now),
                        "game_start_ts": None,
                        "game_accum_seconds": 0,
                        "daily_cycle_key": cycle_key,
                        "daily_game_seconds": daily_game_seconds,
                        "presence_flap_offline_since": prev_record.get(
                            "presence_flap_offline_since", now
                        ),
                        "presence_flap_prev_game": prev_record.get(
                            "presence_flap_prev_game", prev_game
                        ),
                        "presence_flap_prev_game_start_ts": prev_record.get(
                            "presence_flap_prev_game_start_ts", prev_game_start_ts
                        ),
                        "presence_flap_prev_game_accum_seconds": self._safe_int(
                            prev_record.get("presence_flap_prev_game_accum_seconds", 0),
                            prev_game_accum_seconds,
                        ),
                        "presence_flap_confirmed": False,
                        "ts": now,
                        "missing": True,
                    }
                    target_state[sid] = next_record
                    self.state[sid] = next_record
                    if prev is not None and prev != 0:
                        events.append(
                            f"{prev_record.get('personaname', sid)}: 下线（接口未返回）"
                        )
                        event_types.add("offline")
                    elif prev is not None and prev_game:
                        events.append(
                            f"{prev_record.get('personaname', sid)}: 关闭游戏《{prev_game}》（接口未返回）"
                        )
                        event_types.add("game_stop")
                    if prev_gameid:
                        self._schedule_achievement_final_check(
                            target,
                            sid,
                            prev_gameid,
                            prev_record.get("personaname", sid),
                            prev_game or prev_gameid,
                        )
                    continue

                st = int(p.get("personastate", 0))
                current_gameid = str(p.get("gameid", "") or "").strip()
                game = (p.get("gameextrainfo", "") or "").strip()

                prev_record = target_state.get(sid, {})
                prev = prev_record.get("personastate")
                prev_st = self._safe_int(prev, 0)
                prev_gameid = str(prev_record.get("gameid", "") or "").strip()
                prev_game = (prev_record.get("gameextrainfo", "") or "").strip()
                prev_game_start_ts = prev_record.get("game_start_ts")
                game_accum_seconds = self._safe_int(
                    prev_record.get("game_accum_seconds", 0), 0
                )
                prev_game_accum_seconds = game_accum_seconds
                pending_offline_since = (
                    prev_record.get("presence_flap_offline_since", "") or ""
                )
                pending_prev_game = (
                    prev_record.get("presence_flap_prev_game", "") or ""
                )
                pending_prev_game_start_ts = prev_record.get(
                    "presence_flap_prev_game_start_ts"
                )
                pending_prev_game_accum_seconds = self._safe_int(
                    prev_record.get("presence_flap_prev_game_accum_seconds", 0), 0
                )
                pending_confirmed = bool(prev_record.get("presence_flap_confirmed", False))

                daily_game_seconds = self._safe_int(
                    prev_record.get("daily_game_seconds", 0), 0
                )
                if str(prev_record.get("daily_cycle_key", "")) != cycle_key:
                    daily_game_seconds = 0

                current_countable = self._is_duration_countable_state(st)
                prev_countable = self._is_duration_countable_state(prev_st)
                is_offline_candidate = (
                    flap_suppress_sec > 0 and prev is not None and prev != 0 and st == 0
                )

                # 关闭游戏/切换游戏：结算当前段到当日总时长；离开等非计时状态则仅暂停不清零。
                if prev_game:
                    game_changed_or_closed = (not game) or (prev_game != game)
                    if not is_offline_candidate and game_changed_or_closed:
                        if prev_countable and prev_game_start_ts:
                            game_accum_seconds += self._session_seconds_in_current_cycle(
                                prev_game_start_ts,
                                now_dt,
                                cycle_start,
                            )
                        daily_game_seconds += game_accum_seconds
                        game_accum_seconds = 0
                    elif (
                        not is_offline_candidate
                        and prev_game == game
                        and prev_countable
                        and (not current_countable)
                    ):
                        # 仅暂停当前游戏计时，待恢复可计时状态后继续累计。
                        if prev_game_start_ts:
                            game_accum_seconds += self._session_seconds_in_current_cycle(
                                prev_game_start_ts,
                                now_dt,
                                cycle_start,
                            )

                offline_since = prev_record.get("offline_since", "")
                if st == 0:
                    if not (prev == 0 and offline_since):
                        offline_since = now
                else:
                    offline_since = ""

                # 计算游戏开始时间
                game_start_ts = prev_game_start_ts
                if game and current_countable:
                    if prev_game == game and prev_game_start_ts and prev_countable:
                        start_dt = parse_iso(game_start_ts)
                        if start_dt and start_dt < cycle_start:
                            game_start_ts = cycle_start.isoformat(timespec="seconds")
                    elif prev_game == game and not prev_countable:
                        # 从暂停状态恢复：基于已累计秒数继续，而非重置。
                        game_start_ts = now
                    else:
                        # 新游戏开始，或从不可计时状态恢复为可计时状态
                        game_start_ts = now
                        if prev_game != game:
                            game_accum_seconds = 0
                else:
                    # 不在可计时状态或无游戏时，不持有活跃计时段
                    game_start_ts = None

                suppress_online_event = False
                suppress_offline_event = False

                # 抖动抑制：在线->离线先进入候选状态，窗口内恢复不播报上下线
                if flap_suppress_sec > 0 and prev is not None:
                    if prev != 0 and st == 0:
                        pending_offline_since = now
                        pending_prev_game = prev_game
                        pending_prev_game_start_ts = prev_game_start_ts
                        pending_prev_game_accum_seconds = game_accum_seconds
                        pending_confirmed = False
                        suppress_offline_event = True
                    elif prev == 0 and st == 0 and pending_offline_since and not pending_confirmed:
                        pending_dt = parse_iso(pending_offline_since)
                        if pending_dt and (now_dt - pending_dt).total_seconds() >= flap_suppress_sec:
                            events.append(f"{p.get('personaname', '?')} 下线")
                            event_types.add("offline")
                            if pending_prev_game:
                                pending_end = pending_dt
                                daily_game_seconds += pending_prev_game_accum_seconds
                                daily_game_seconds += self._session_seconds_in_current_cycle(
                                    pending_prev_game_start_ts,
                                    pending_end,
                                    cycle_start,
                                )
                                game_accum_seconds = 0
                                game_start_ts = None
                                pending_prev_game_accum_seconds = 0
                            pending_confirmed = True
                    elif prev == 0 and st != 0 and pending_offline_since:
                        pending_dt = parse_iso(pending_offline_since)
                        elapsed = None
                        if pending_dt:
                            elapsed = (now_dt - pending_dt).total_seconds()

                        # 窗口内离线后恢复在线：不播报上下线；若恢复同款游戏则延续原开始时间
                        if elapsed is not None and elapsed < flap_suppress_sec:
                            suppress_online_event = True
                            if (
                                game
                                and pending_prev_game
                                and game == pending_prev_game
                                and current_countable
                            ):
                                if pending_prev_game_start_ts:
                                    game_start_ts = pending_prev_game_start_ts
                                    game_accum_seconds = pending_prev_game_accum_seconds
                            elif pending_prev_game:
                                # 窗口内恢复但不是同款游戏：按下线时刻结算上一局，避免时长丢失。
                                pending_end = pending_dt or now_dt
                                daily_game_seconds += pending_prev_game_accum_seconds
                                daily_game_seconds += self._session_seconds_in_current_cycle(
                                    pending_prev_game_start_ts,
                                    pending_end,
                                    cycle_start,
                                )
                                pending_prev_game_accum_seconds = 0
                        else:
                            # 窗口过期后恢复在线：报告下线，但不再报告上线（避免重复）
                            if not pending_confirmed:
                                events.append(f"{p.get('personaname', '?')} 下线")
                                event_types.add("offline")
                                if pending_prev_game:
                                    pending_end = pending_dt or now_dt
                                    daily_game_seconds += pending_prev_game_accum_seconds
                                    daily_game_seconds += self._session_seconds_in_current_cycle(
                                        pending_prev_game_start_ts,
                                        pending_end,
                                        cycle_start,
                                    )
                                    game_accum_seconds = 0
                                    pending_prev_game_accum_seconds = 0
                                    if game_start_ts == pending_prev_game_start_ts:
                                        game_start_ts = None
                            # 窗口过期后恢复时抑制上线播报，避免出现"下线"后立即"上线"
                            suppress_online_event = True

                        pending_offline_since = ""
                        pending_prev_game = ""
                        pending_prev_game_start_ts = None
                        pending_prev_game_accum_seconds = 0
                        pending_confirmed = False
                    elif st != 0:
                        pending_offline_since = ""
                        pending_prev_game = ""
                        pending_prev_game_start_ts = None
                        pending_prev_game_accum_seconds = 0
                        pending_confirmed = False

                next_record = {
                    "personaname": p.get("personaname", ""),
                    "personastate": st,
                    "gameid": current_gameid,
                    "gameextrainfo": game,
                    "offline_since": offline_since,
                    "game_start_ts": game_start_ts,
                    "game_accum_seconds": game_accum_seconds,
                    "daily_cycle_key": cycle_key,
                    "daily_game_seconds": daily_game_seconds,
                    "presence_flap_offline_since": pending_offline_since,
                    "presence_flap_prev_game": pending_prev_game,
                    "presence_flap_prev_game_start_ts": pending_prev_game_start_ts,
                    "presence_flap_prev_game_accum_seconds": pending_prev_game_accum_seconds,
                    "presence_flap_confirmed": pending_confirmed,
                    "ts": now,
                    "missing": False,
                }
                target_state[sid] = next_record
                self.state[sid] = next_record

                if prev is None:
                    continue

                name = p.get("personaname", "?")
                if prev == 0 and st != 0 and not suppress_online_event:
                    events.append(f"{name} 上线")
                    event_types.add("online")
                elif prev != 0 and st == 0 and not suppress_offline_event:
                    events.append(f"{name} 下线")
                    event_types.add("offline")

                if st != 0:
                    avatar_url = (
                        str(p.get("avatarfull") or p.get("avatarmedium") or p.get("avatar") or "")
                    )
                    if not prev_game and game:
                        events.append(f"{name} 启动《{game}》")
                        event_types.add("game_start")
                        if current_gameid:
                            self._spawn_bg_task(
                                self._push_game_start_render(
                                    target,
                                    sid,
                                    name,
                                    avatar_url,
                                    current_gameid,
                                    game,
                                )
                            )
                    elif prev_game and not game:
                        game_duration = self._format_game_duration(
                            prev_game_accum_seconds
                            + self._session_seconds_total(prev_game_start_ts, now_dt)
                        )
                        events.append(f"{name} 结束《{prev_game}》 {game_duration}")
                        event_types.add("game_stop")
                    elif prev_game and game and prev_game != game:
                        game_duration = self._format_game_duration(
                            prev_game_accum_seconds
                            + self._session_seconds_total(prev_game_start_ts, now_dt)
                        )
                        events.append(
                            f"{name} 切换游戏《{prev_game}》 -> 《{game}》 {game_duration}"
                        )
                        event_types.add("game_switch")
                        if current_gameid:
                            self._spawn_bg_task(
                                self._push_game_start_render(
                                    target,
                                    sid,
                                    name,
                                    avatar_url,
                                    current_gameid,
                                    game,
                                )
                            )

                # 成就监控生命周期：开始游戏启动，结束/切换/掉线做延迟结算
                if self._achievement_enabled():
                    if prev_gameid and prev_gameid != current_gameid:
                        self._schedule_achievement_final_check(
                            target,
                            sid,
                            prev_gameid,
                            name,
                            prev_game or prev_gameid,
                        )
                    if current_gameid and current_gameid != prev_gameid:
                        self._spawn_bg_task(
                            self._start_achievement_monitoring(
                                target,
                                sid,
                                current_gameid,
                                name,
                                game or current_gameid,
                            )
                        )

            self._save_state()

            if events:
                text_trigger_types = self._status_text_trigger_types()
                image_trigger_types = self._status_image_trigger_types()
                send_text = bool(event_types & text_trigger_types)
                send_image = bool(event_types & image_trigger_types)

                if not send_text and not send_image:
                    logger.debug(
                        "[steam-monitor] event push skipped by trigger types: "
                        f"event_types={sorted(event_types)} "
                        f"text_trigger_types={sorted(text_trigger_types)} "
                        f"image_trigger_types={sorted(image_trigger_types)}"
                    )
                    return

                if send_image:
                    min_interval = self._status_image_min_interval_min()
                else:
                    min_interval = 0

                if send_image and min_interval > 0:
                    now_ts = time.time()
                    last_push_ts = self._get_target_last_push_ts(target)
                    if last_push_ts > 0 and (now_ts - last_push_ts) < min_interval * 60:
                        target_key = self._normalize_target_key(target)
                        logger.info(
                            "[steam-monitor] status image push skipped by interval limit: "
                            f"target={target} target_key={target_key} min_interval={min_interval}"
                        )
                        send_image = False

                if not send_text and not send_image:
                    return

                text = chr(10).join(events)
                try:
                    if send_image:
                        ordered_players = self._order_players_by_ids(players, steam_ids)
                        image_path = await self._render_status_image(ordered_players, target)
                        if send_text:
                            await self._push_image(target, text, image_path)
                        else:
                            await self._push_image(target, "", image_path)
                        self._set_target_last_push_ts(target, time.time())
                        self._save_state()
                    else:
                        await self._push_text(target, text)
                except Exception as e:
                    logger.error(f"[steam-monitor] push failed {target}: {e}")
        finally:
            if image_path:
                with contextlib.suppress(Exception):
                    Path(image_path).unlink(missing_ok=True)

    def _validate_steam_id64(self, sid: str) -> bool:
        sid = (sid or "").strip()
        return sid.isdigit() and len(sid) >= 10

    @filter.command("sfm_bind")
    async def bind_group(self, event: AstrMessageEvent):

        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return
        umo = event.unified_msg_origin
        targets = self._get_targets()
        if umo not in targets:
            targets.append(umo)
            await self._update_targets_atomic(targets)
        yield event.plain_result(
            "已绑定当前会话"
        )

    @filter.command("sfm_unbind")
    async def unbind_group(self, event: AstrMessageEvent):

        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return
        umo = event.unified_msg_origin
        targets = self._get_targets()
        if umo in targets:
            targets.remove(umo)
            self._set_targets(targets)
        yield event.plain_result("已取消当前会话绑定")

    @filter.command("sfm_targets")
    async def show_targets(self, event: AstrMessageEvent):

        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return
        targets = self._get_targets()
        if not targets:
            yield event.plain_result("当前无推送目标，请先 /sfm_bind")
            return
        yield event.plain_result("当前推送目标：" + chr(10) + chr(10).join(targets))

    @filter.command("sfm_add_id")
    async def bind_id(self, event: AstrMessageEvent, steam_id64: str):

        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return
        steam_id64 = (steam_id64 or "").strip()
        if not self._validate_steam_id64(steam_id64):
            yield event.plain_result("SteamID64 格式不正确")
            return

        ids = parse_ids(self.config.get("steam_ids", ""))
        if steam_id64 not in ids:
            ids.append(steam_id64)
        await self._update_config_atomic("steam_ids", ",".join(ids))
        yield event.plain_result(
            f"已绑定 SteamID64: {steam_id64}，当前时间数量: {len(ids)}"
        )

    @filter.command("sfm_del_id")
    async def unbind_id(self, event: AstrMessageEvent, steam_id64: str):

        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return
        steam_id64 = (steam_id64 or "").strip()
        ids = parse_ids(self.config.get("steam_ids", ""))
        if steam_id64 in ids:
            ids.remove(steam_id64)
        self.state.pop(steam_id64, None)
        self._save_state()
        await self._update_config_atomic("steam_ids", ",".join(ids))
        yield event.plain_result(
            f"已移除 SteamID64: {steam_id64}，当前时间数量: {len(ids)}"
        )

    @filter.command("sfm_set_ids")
    async def set_ids(self, event: AstrMessageEvent, ids: str):

        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return
        parsed = parse_ids(ids)
        valid = [sid for sid in parsed if self._validate_steam_id64(sid)]
        invalid = [sid for sid in parsed if not self._validate_steam_id64(sid)]
        await self._update_config_atomic("steam_ids", ",".join(valid))
        if invalid:
            yield event.plain_result(
                f"已设置时间ID数量: {len(valid)}；忽略非法ID {len(invalid)} 个："
                + ", ".join(invalid[:10])
            )
        else:
            yield event.plain_result(f"已设置时间ID数量: {len(valid)}")

    @filter.command("sfm_status")
    async def status(self, event: AstrMessageEvent):
        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return
        group_id = event.unified_msg_origin
        group_ids = self._get_group_steam_ids(group_id)
        global_ids = parse_ids(self.config.get("steam_ids", ""))
        steam_ids = group_ids if group_ids is not None else global_ids
        if not steam_ids:
            yield event.plain_result("未配置 steam_ids（当前群无独立配置，且全局也为空）")
            return

        image_path = None
        try:
            players = await self._fetch_players(steam_ids)
            players = await self._enrich_players_with_profile_game_fallback(players)
            ordered_players = self._order_players_by_ids(players, steam_ids)
            image_path = await self._render_status_image(
                ordered_players, event.unified_msg_origin
            )
            await self._push_image(event.unified_msg_origin, "", image_path)
        except RuntimeError as e:
            logger.error(f"[steam-monitor] status failed: {e}")
            yield event.plain_result(f"获取状态失败: {str(e)}")
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
            logger.error(f"[steam-monitor] status network error: {e}")
            error_msg = (
                "网络连接失败，请稍后重试\n"
                f"错误类型: {type(e).__name__}\n"
                "可能的原因:\n"
                "• Steam API 服务暂时不可用\n"
                "• 本地网络连接中断\n"
                "• DNS 解析失败"
            )
            yield event.plain_result(error_msg)
        except Exception as e:
            logger.error(f"[steam-monitor] status failed: {e}", exc_info=True)
            yield event.plain_result(f"获取状态失败: {e}")
        finally:
            if image_path:
                self._schedule_delayed_unlink(image_path, 30)

    @filter.command("sfm_test")
    async def steam_monitor_test(self, event: AstrMessageEvent, action: str = "all"):
        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return

        action = (action or "all").strip().lower()
        group_id = event.unified_msg_origin
        group_ids = self._get_group_steam_ids(group_id)
        global_ids = parse_ids(self.config.get("steam_ids", ""))
        steam_ids = group_ids if group_ids is not None else global_ids
        targets = self._get_targets()

        if action in ("cfg", "config"):
            msg = [
                "[steam_monitor_test: config]",
                f"steam_ids_count={len(steam_ids)}",
                f"steam_ids_source={'group' if group_ids is not None else 'global'}",
                f"push_targets_count={len(targets)}",
                f"poll_interval_sec={self.config.get('poll_interval_sec', 60)}",
                f"steam_api_key_set={'yes' if bool(self.config.get('steam_api_key', '')) else 'no'}",
            ]
            yield event.plain_result(chr(10).join(msg))
            return

        if not steam_ids:
            yield event.plain_result("[steam_monitor_test] 未配置 steam_ids")
            return

        image_path = None
        try:
            players = await self._fetch_players(steam_ids)
            players = await self._enrich_players_with_profile_game_fallback(players)
            ordered_players = self._order_players_by_ids(players, steam_ids)
            status_text = chr(10).join(
                [
                    f"{p.get('personaname', '?')}: {persona_text(int(p.get('personastate', 0)))}"
                    + (
                        f" | {p.get('gameextrainfo', '')}"
                        if p.get("gameextrainfo")
                        else ""
                    )
                    for p in ordered_players
                ]
            )

            if action in ("status", "pull"):
                yield event.plain_result(
                    "[steam_monitor_test: status]" + chr(10) + status_text
                )
                return

            image_path = await self._render_status_image(
                ordered_players, event.unified_msg_origin
            )
            await self._push_image(
                event.unified_msg_origin,
                "[steam_monitor_test] 状态拉取成功，测试图如下",
                image_path,
            )

            if action in ("push", "all"):
                if not targets:
                    yield event.plain_result("[steam_monitor_test] 未配置推送目标")
                else:
                    ok = 0
                    for umo in targets:
                        try:
                            await self._push_image(
                                umo, "[steam_monitor_test] 目标会话测试推送", image_path
                            )
                            ok += 1
                        except Exception as e:
                            logger.error(f"[steam-monitor] test push failed {umo}: {e}", exc_info=True)
                    yield event.plain_result(
                        f"[steam_monitor_test] 目标会话测试推送完成: {ok}/{len(targets)}"
                    )
        except RuntimeError as e:
            logger.error(f"[steam-monitor] test failed: {e}")
            yield event.plain_result(f"[steam_monitor_test] 执行失败: {str(e)}")
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
            logger.error(f"[steam-monitor] test network error: {e}")
            error_msg = (
                "[steam_monitor_test] 网络连接失败\n"
                f"错误类型: {type(e).__name__}\n"
                "请检查网络连接或稍后重试"
            )
            yield event.plain_result(error_msg)
        except Exception as e:
            logger.error(f"[steam-monitor] test failed: {e}", exc_info=True)
            yield event.plain_result(f"[steam_monitor_test] 执行失败: {e}")
        finally:
            if image_path:
                self._schedule_delayed_unlink(image_path, 30)

    @filter.command("steam test_achievement_render")
    async def steam_test_achievement_render(
        self,
        event: AstrMessageEvent,
        steamid: str,
        gameid: int,
        count: int = 3,
    ):
        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return

        gid = str(gameid)
        api_key = str(self.config.get("steam_api_key", "")).strip()
        if not api_key:
            yield event.plain_result("未配置 steam_api_key")
            return

        achievements = await self.achievement_monitor.get_player_achievements(
            api_key,
            event.unified_msg_origin,
            steamid,
            gid,
        )
        if not achievements:
            yield event.plain_result("未获取到任何成就，可能为隐私或无成就")
            return

        details = await self.achievement_monitor.get_achievement_details(
            event.unified_msg_origin,
            gid,
            lang="schinese",
            api_key=api_key,
            steamid=steamid,
        )
        if not details:
            yield event.plain_result("获取成就详情失败")
            return

        for d in details.values():
            if isinstance(d, dict):
                d["game_name"] = "测试渲染"

        sample_count = max(1, min(int(count or 1), len(achievements)))
        unlocked = set(random.sample(list(achievements), sample_count))

        font_path = self.get_font_path("NotoSansHans-Regular.otf") or str(self.plugin_dir / "fonts" / "NotoSansCJKsc-Regular.otf")

        try:
            img_bytes = await self.achievement_monitor.render_achievement_image(
                details,
                unlocked,
                player_name=steamid,
                steamid=steamid,
                appid=gid,
                unlocked_set=set(achievements),
                font_path=font_path,
                api_key=api_key,
                target=event.unified_msg_origin,
            )
            with tempfile.NamedTemporaryFile(
                delete=False,
                suffix=".png",
                dir=str(self.data_dir),
            ) as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            self._schedule_delayed_unlink(tmp_path, 30)
            yield event.image_result(tmp_path)
        except Exception as e:
            logger.error(f"[steam-monitor] test achievement render failed: {e}", exc_info=True)
            yield event.plain_result(f"成就图片渲染失败: {e}")

    @filter.command("steam test_game_start_render")
    async def steam_test_game_start_render(
        self,
        event: AstrMessageEvent,
        steamid: str,
        gameid: int,
    ):
        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return

        gid = str(gameid)
        try:
            players = await self._fetch_players([steamid])
            player = players[0] if players else {}
            player_name = str(player.get("personaname") or steamid)
            avatar_url = str(
                player.get("avatarfull") or player.get("avatarmedium") or player.get("avatar") or ""
            )

            zh_game_name, en_game_name = await self._get_game_names(gid, fallback_name=gid)
            online_count = await self._get_game_online_count(gid)

            font_path = self.get_font_path("NotoSansHans-Regular.otf") or str(self.plugin_dir / "fonts" / "NotoSansCJKsc-Regular.otf")

            img_bytes = await render_game_start(
                str(self.data_dir),
                steamid,
                player_name,
                avatar_url,
                gid,
                zh_game_name,
                api_key=str(self.config.get("steam_api_key", "")).strip(),
                superpower=None,
                sgdb_api_key=self.SGDB_API_KEY,
                font_path=font_path,
                sgdb_game_name=en_game_name,
                online_count=online_count,
                appid=gid,
                sgdb_api_base=self.SGDB_API_BASE,
                steam_api_base=self.STEAM_API_BASE,
            )
            with tempfile.NamedTemporaryFile(
                delete=False,
                suffix=".png",
                dir=str(self.data_dir),
            ) as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            self._schedule_delayed_unlink(tmp_path, 30)
            yield event.image_result(tmp_path)
        except Exception as e:
            logger.error(f"[steam-monitor] test game start render failed: {e}", exc_info=True)
            yield event.plain_result(f"开始游戏图片渲染失败: {e}")

    @filter.command("sfm_achievement_on")
    async def achievement_on(self, event: AstrMessageEvent):
        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return
        await self._update_config_atomic("enable_achievement_monitor", True)
        yield event.plain_result("已开启成就监控")

    @filter.command("sfm_achievement_off")
    async def achievement_off(self, event: AstrMessageEvent):
        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return
        await self._update_config_atomic("enable_achievement_monitor", False)
        for t in list(self.achievement_poll_tasks.values()):
            t.cancel()
        self.achievement_poll_tasks.clear()
        for t in list(self.achievement_final_tasks.values()):
            t.cancel()
        self.achievement_final_tasks.clear()
        self.achievement_snapshots.clear()
        yield event.plain_result("已关闭成就监控，并停止当前成就轮询任务")

    @filter.command("sfm_achievement_status")
    async def achievement_status(self, event: AstrMessageEvent):
        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return
        lines = [
            f"enable_achievement_monitor={self._achievement_enabled()}",
            f"achievement_poll_interval_sec={self._achievement_poll_interval_sec()}",
            f"achievement_final_check_delay_sec={self._achievement_final_delay_sec()}",
            f"max_achievement_notifications={self._achievement_max_notify()}",
            f"active_achievement_poll_tasks={len(self.achievement_poll_tasks)}",
            f"active_achievement_final_tasks={len(self.achievement_final_tasks)}",
            f"achievement_blacklist_size={len(self.achievement_monitor.achievement_blacklist)}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("sfm_set_group_ids")
    async def set_group_ids(self, event: AstrMessageEvent, ids: str):
        """为当前群设置独立的时间 ID"""
        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return
        
        group_id = event.unified_msg_origin
        parsed = parse_ids(ids)
        valid = [sid for sid in parsed if self._validate_steam_id64(sid)]
        invalid = [sid for sid in parsed if not self._validate_steam_id64(sid)]
        
        await self._update_group_steam_ids_atomic(group_id, valid)
        
        if not valid:
            yield event.plain_result("未设置任何有效的 SteamID")
            return
        
        if invalid:
            yield event.plain_result(
                f"[当前群] 已设置时间ID数量: {len(valid)}；忽略非法ID {len(invalid)} 个："
                + ", ".join(invalid[:10])
            )
        else:
            yield event.plain_result(f"[当前群] 已设置时间ID数量: {len(valid)}")

    @filter.command("sfm_add_group_id")
    async def add_group_id(self, event: AstrMessageEvent, ids: str):
        """为当前群添加时间 ID（支持逗号/换行批量）"""
        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return

        parsed = parse_ids(ids)
        valid = [sid for sid in parsed if self._validate_steam_id64(sid)]
        invalid = [sid for sid in parsed if not self._validate_steam_id64(sid)]
        if not valid:
            yield event.plain_result("未添加任何有效的 SteamID64")
            return

        group_id = event.unified_msg_origin
        current_ids = self._get_group_steam_ids(group_id) or []

        added = 0
        for sid in valid:
            if sid not in current_ids:
                current_ids.append(sid)
                added += 1

        await self._update_group_steam_ids_atomic(group_id, current_ids)

        msg = (
            f"添加完成：新增 {added} 个，"
            f"当前时间数量: {len(current_ids)}"
        )
        if invalid:
            msg += (
                f"；忽略非法ID {len(invalid)} 个："
                + ", ".join(invalid[:10])
            )
        yield event.plain_result(msg)

    @filter.command("sfm_del_group_id")
    async def del_group_id(self, event: AstrMessageEvent, steam_id64: str):
        """为当前群删除一个时间 ID"""
        if not self._is_authorized(event):
            yield event.plain_result("无权限")
            return
        
        steam_id64 = (steam_id64 or "").strip()
        group_id = event.unified_msg_origin
        ids = self._get_group_steam_ids(group_id)
        
        if ids and steam_id64 in ids:
            ids.remove(steam_id64)
            await self._update_group_steam_ids_atomic(group_id, ids)
            yield event.plain_result(
                f"[当前群] 已移除 {steam_id64}，当前时间数量: {len(ids)}"
            )
        else:
            yield event.plain_result(
                "[当前群] 该 SteamID64 不在时间列表中，或未为本群单独设置时间"
            )

    @filter.command("sfm_group_ids")
    async def show_group_ids(self, event: AstrMessageEvent):
        """查看当前群的时间 ID 设置"""
        if not self._is_authorized(event):
            yield event.plain_result("无权限")
            return
        
        group_id = event.unified_msg_origin
        group_ids = self._get_group_steam_ids(group_id)
        
        if group_ids is None:
            global_ids = parse_ids(self.config.get("steam_ids", ""))
            if global_ids:
                yield event.plain_result(
                    f"未设置独立时间，使用全局配置({len(global_ids)}个)：\n"
                    + ",\n".join(global_ids)
                )
            else:
                yield event.plain_result("未设置独立时间，也无全局配置")
        else:
            yield event.plain_result(
                f"群独立时间配置({len(group_ids)}个)：\n"
                + ",\n".join(group_ids)
            )

    @filter.command("sfm_clear_group_ids")
    async def clear_group_ids(self, event: AstrMessageEvent):
        """清除当前群的独立配置，回到全局配置"""
        if not self._is_authorized(event):
            yield event.plain_result("无权限")
            return
        
        group_id = event.unified_msg_origin
        if group_id in self.group_configs:
            await self._update_group_steam_ids_atomic(group_id, [])
            yield event.plain_result("本群已清除独立配置")
        else:
            yield event.plain_result("本群未设置独立配置")



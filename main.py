import asyncio
import contextlib
import ipaddress
import json
import platform
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import datetime
from io import BytesIO
import uuid
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote, urlparse

import httpx
from PIL import Image, ImageDraw, ImageFont

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

        self.state: Dict[str, Any] = self._load_state()
        self.group_configs: Dict[str, List[str]] = self._load_group_configs()
        self._stop = False
        self._task: asyncio.Task | None = None

        self.http: httpx.AsyncClient | None = None
        self.bytes_cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self.icon_url_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self.profile_game_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._config_lock = asyncio.Lock()
        self._bg_tasks: set[asyncio.Task] = set()

    async def initialize(self):
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
        """获取某个群的专属监控 ID，如果未设置则返回 None"""
        group_id = str(group_id or "").strip()
        return self.group_configs.get(group_id)

    def _set_group_steam_ids(self, group_id: str, steam_ids: List[str]):
        """设置某个群的专属监控 ID"""
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

        for origin in candidates:
            current = origin
            logger.debug(f"[steam-monitor] start fetch origin={origin}")
            if not await self._is_allowed_remote_url(current):
                logger.debug(f"[steam-monitor] blocked remote url: {current}")
                continue

            try:
                for hop in range(max_redirects + 1):
                    logger.debug(
                        f"[steam-monitor] request hop={hop}/{max_redirects} current={current}"
                    )
                    async with self.http.stream(
                        "GET", current, follow_redirects=False
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
                                break
                            next_url = str(httpx.URL(location, base=resp.request.url))
                            logger.debug(
                                f"[steam-monitor] redirect: from={current} to={next_url} status={resp.status_code}"
                            )
                            if not await self._is_allowed_remote_url(next_url):
                                logger.warning(
                                    f"[steam-monitor] blocked redirect target: {next_url}"
                                )
                                break
                            current = next_url
                            continue

                        if resp.status_code != 200:
                            logger.warning(
                                f"[steam-monitor] fetch non-200: url={current} status={resp.status_code}"
                            )
                            break

                        ctype = (resp.headers.get("content-type") or "").lower()
                        if allowed_types and not any(
                            ctype.startswith(x) for x in allowed_types
                        ):
                            logger.warning(
                                f"[steam-monitor] content-type not allowed: url={current} content-type={ctype} allowed={allowed_types}"
                            )
                            break

                        clen = resp.headers.get("content-length")
                        if clen:
                            with contextlib.suppress(Exception):
                                if int(clen) > max_bytes:
                                    logger.warning(
                                        f"[steam-monitor] content-length too large: url={current} content-length={clen} max={max_bytes}"
                                    )
                                    break

                        buf = bytearray()
                        async for chunk in resp.aiter_bytes(65536):
                            buf.extend(chunk)
                            if len(buf) > max_bytes:
                                logger.warning(
                                    f"[steam-monitor] streamed bytes exceeded limit: url={current} size={len(buf)} max={max_bytes}"
                                )
                                buf = bytearray()
                                break

                        if not buf:
                            logger.warning(
                                f"[steam-monitor] empty body after fetch: url={current}"
                            )
                            break

                        raw = bytes(buf)
                        self._cache_set(self.bytes_cache, url, raw, "bytes")
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

        logger.warning(f"[steam-monitor] all fetch candidates failed: url={url}")
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
                return None

            data = json.loads(raw.decode("utf-8", errors="ignore"))
            node = data.get(str(appid), {})
            if not node.get("success"):
                logger.warning(
                    f"[steam-monitor] game metadata success=false appid={appid}"
                )
                return None
            app = node.get("data", {})
            icon_url = app.get("header_image") or app.get("capsule_image")
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
            # 网络错误时返回 None，下次会重新尝试，但本次不中断处理
            return None
        except Exception as e:
            logger.warning(f"[steam-monitor] parse game icon failed appid={appid}: {e}")
            return None

    def _profile_game_fallback_enabled(self) -> bool:
        return bool(self.config.get("enable_profile_game_fallback", True))

    async def _get_profile_game_name_fallback(self, steamid: str) -> str:
        """当 API 未返回 gameextrainfo 时，从个人资料 XML 兜底读取游戏名。"""
        steamid = (steamid or "").strip()
        if not steamid:
            return ""

        cached = self._cache_get(self.profile_game_cache, steamid)
        if cached is not None:
            return str(cached or "").strip()

        profile_xml_url = f"https://steamcommunity.com/profiles/{steamid}/?xml=1"
        raw = await self._fetch_url_bytes(
            profile_xml_url,
            allowed_types=("text/", "application/xml"),
            max_bytes=256 * 1024,
        )
        if not raw:
            self._cache_set(self.profile_game_cache, steamid, "", "profile_game")
            return ""

        try:
            text = raw.decode("utf-8", errors="ignore")
            root = ET.fromstring(text)
            game_name = (root.findtext(".//inGameInfo/gameName") or "").strip()
            self._cache_set(
                self.profile_game_cache, steamid, game_name, "profile_game"
            )
            if game_name:
                logger.debug(
                    f"[steam-monitor] profile fallback game name resolved steamid={steamid} game={game_name}"
                )
            return game_name
        except Exception as e:
            logger.debug(
                f"[steam-monitor] profile fallback parse failed steamid={steamid}: {e}"
            )
            self._cache_set(self.profile_game_cache, steamid, "", "profile_game")
            return ""

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

    def _build_status_image(
        self, players: List[Dict[str, Any]], assets: Dict[str, Dict[str, Any]]
    ) -> str:
        w = 980
        row_h = 110
        top = 56
        h = top + row_h * max(1, len(players)) + 20

        img = Image.new("RGB", (w, h), (22, 26, 31))
        draw = ImageDraw.Draw(img)

        font_text = safe_font(24, self.plugin_dir)
        font_small = safe_font(18, self.plugin_dir)

        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

            draw.text((112, y + 18), name, fill=(240, 240, 240), font=font_text)
            line2_parts = [persona_text(state)]
            if game:
                line2_parts.append(game)
            line2 = " | ".join(line2_parts)
            draw.text((112, y + 54), line2, fill=(170, 180, 190), font=font_small)

            # 在线（state=1）时仅显示绿色圆点，避免与下方状态文案重复。
            if state == 1:
                dot_x = w - 246
                dot_y = y + 30
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

    async def _render_status_image(self, players: List[Dict[str, Any]]) -> str:
        logger.info(f"[steam-monitor] render status image start players={len(players)}")
        assets = await self._prepare_assets(players)
        out = await asyncio.to_thread(self._build_status_image, players, assets)
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
        # 基于完整监控集合（配置 ID + state）计算，而不是仅 API 返回列表
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
            player_map = {str(p.get("steamid", "")): p for p in players}
            target_state = self._get_target_player_state(target)

            events: List[str] = []
            event_types: set[str] = set()
            now = now_iso()
            for sid in steam_ids:
                p = player_map.get(sid)
                if p is None:
                    prev_record = target_state.get(sid, {})
                    prev = prev_record.get("personastate")
                    prev_game = (prev_record.get("gameextrainfo", "") or "").strip()
                    next_record = {
                        "personaname": prev_record.get("personaname", sid),
                        "personastate": 0,
                        "gameextrainfo": "",
                        "offline_since": prev_record.get("offline_since", now),
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
                    continue

                st = int(p.get("personastate", 0))
                game = (p.get("gameextrainfo", "") or "").strip()
                if st != 0 and not game and self._profile_game_fallback_enabled():
                    fallback_game = await self._get_profile_game_name_fallback(sid)
                    if fallback_game:
                        game = fallback_game

                prev_record = target_state.get(sid, {})
                prev = prev_record.get("personastate")
                prev_game = (prev_record.get("gameextrainfo", "") or "").strip()

                offline_since = prev_record.get("offline_since", "")
                if st == 0:
                    if not (prev == 0 and offline_since):
                        offline_since = now
                else:
                    offline_since = ""

                next_record = {
                    "personaname": p.get("personaname", ""),
                    "personastate": st,
                    "gameextrainfo": game,
                    "offline_since": offline_since,
                    "ts": now,
                    "missing": False,
                }
                target_state[sid] = next_record
                self.state[sid] = next_record

                if prev is None:
                    continue

                name = p.get("personaname", "?")
                if prev == 0 and st != 0:
                    events.append(f"{name} 上线 ({persona_text(st)})")
                    event_types.add("online")
                elif prev != 0 and st == 0:
                    events.append(f"{name} 下线")
                    event_types.add("offline")

                if st != 0:
                    if not prev_game and game:
                        events.append(f"{name} 启动游戏《{game}》")
                        event_types.add("game_start")
                    elif prev_game and not game:
                        events.append(f"{name} 关闭游戏《{prev_game}》")
                        event_types.add("game_stop")
                    elif prev_game and game and prev_game != game:
                        events.append(
                            f"{name} 切换游戏《{prev_game}》 -> 《{game}》"
                        )
                        event_types.add("game_switch")

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
                        image_path = await self._render_status_image(ordered_players)
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
            f"已绑定 SteamID64: {steam_id64}，当前监控数量: {len(ids)}"
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
            f"已移除 SteamID64: {steam_id64}，当前监控数量: {len(ids)}"
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
                f"已设置监控ID数量: {len(valid)}；忽略非法ID {len(invalid)} 个："
                + ", ".join(invalid[:10])
            )
        else:
            yield event.plain_result(f"已设置监控ID数量: {len(valid)}")

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
            ordered_players = self._order_players_by_ids(players, steam_ids)
            image_path = await self._render_status_image(ordered_players)
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

            image_path = await self._render_status_image(ordered_players)
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

    @filter.command("sfm_set_group_ids")
    async def set_group_ids(self, event: AstrMessageEvent, ids: str):
        """为当前群设置独立的监控 ID"""
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
                f"[当前群] 已设置监控ID数量: {len(valid)}；忽略非法ID {len(invalid)} 个："
                + ", ".join(invalid[:10])
            )
        else:
            yield event.plain_result(f"[当前群] 已设置监控ID数量: {len(valid)}")

    @filter.command("sfm_add_group_id")
    async def add_group_id(self, event: AstrMessageEvent, ids: str):
        """为当前群添加监控 ID（支持逗号/换行批量）"""
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
            f"[当前群] 批量添加完成：新增 {added} 个，"
            f"当前监控数量: {len(current_ids)}"
        )
        if invalid:
            msg += (
                f"；忽略非法ID {len(invalid)} 个："
                + ", ".join(invalid[:10])
            )
        yield event.plain_result(msg)

    @filter.command("sfm_del_group_id")
    async def del_group_id(self, event: AstrMessageEvent, steam_id64: str):
        """为当前群删除一个监控 ID"""
        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return
        
        steam_id64 = (steam_id64 or "").strip()
        group_id = event.unified_msg_origin
        ids = self._get_group_steam_ids(group_id)
        
        if ids and steam_id64 in ids:
            ids.remove(steam_id64)
            await self._update_group_steam_ids_atomic(group_id, ids)
            yield event.plain_result(
                f"[当前群] 已移除 SteamID64: {steam_id64}，当前监控数量: {len(ids)}"
            )
        else:
            yield event.plain_result(
                "[当前群] 该 SteamID64 不在监控列表中，或未为本群单独设置监控"
            )

    @filter.command("sfm_group_ids")
    async def show_group_ids(self, event: AstrMessageEvent):
        """查看当前群的监控 ID 设置"""
        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return
        
        group_id = event.unified_msg_origin
        group_ids = self._get_group_steam_ids(group_id)
        
        if group_ids is None:
            global_ids = parse_ids(self.config.get("steam_ids", ""))
            if global_ids:
                yield event.plain_result(
                    f"[当前群] 未设置独立监控，使用全局设置({len(global_ids)}个)：\n"
                    + ",\n".join(global_ids)
                )
            else:
                yield event.plain_result("[当前群] 未设置独立监控，全局也无监控设置")
        else:
            yield event.plain_result(
                f"[当前群] 独立监控设置({len(group_ids)}个)：\n"
                + ",\n".join(group_ids)
            )

    @filter.command("sfm_clear_group_ids")
    async def clear_group_ids(self, event: AstrMessageEvent):
        """清除当前群的独立设置，回到全局设置"""
        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return
        
        group_id = event.unified_msg_origin
        if group_id in self.group_configs:
            await self._update_group_steam_ids_atomic(group_id, [])
            yield event.plain_result("[当前群] 已清除独立设置，将使用全局监控配置")
        else:
            yield event.plain_result("[当前群] 未设置独立监控，无需清除")

    @filter.command("sfm_debug_group")
    async def debug_group(self, event: AstrMessageEvent):
        """诊断当前群的配置状态"""
        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return
        
        group_id = event.unified_msg_origin
        targets = self._get_targets()
        group_ids = self._get_group_steam_ids(group_id)
        global_ids = parse_ids(self.config.get("steam_ids", ""))
        
        msg = [
            "=== 群配置诊断 ===",
            f"当前群 ID: {group_id}",
            f"在推送目标中? {'✓ 是' if group_id in targets else '✗ 否'}",
            f"有独立配置? {'✓ 是' if group_ids is not None else '✗ 否'}",
            "",
            f"统计信息:",
            f"  推送目标数: {len(targets)}",
            f"  全局 IDs: {len(global_ids)}",
            f"  群独立 IDs: {len(group_ids) if group_ids else 0}",
            "",
        ]
        
        # 详细显示全局和群配置
        if global_ids:
            msg.append(f"全局 steam_ids: {', '.join(global_ids[:3])}" + 
                      (f" 等({len(global_ids)}个)" if len(global_ids) > 3 else ""))
        
        if group_ids:
            msg.append(f"群独立 IDs: {', '.join(group_ids[:3])}" + 
                      (f" 等({len(group_ids)}个)" if len(group_ids) > 3 else ""))
        
        # 排查建议
        msg.extend(["", "建议:"])
        if group_id not in targets:
            msg.append("❌ 本群未绑定 → 先执行 /sfm_bind")
        else:
            msg.append("✓ 本群已绑定")
        
        if group_ids is None:
            msg.append("⚠  无独立配置 → 使用全局")
            if not global_ids:
                msg.append("⚠  全局也无设置 → 执行 /sfm_set_ids")
        else:
            msg.append(f"✓ 有独立配置")
        
        yield event.plain_result(chr(10).join(msg))

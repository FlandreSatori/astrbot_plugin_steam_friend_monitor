import asyncio
import contextlib
import ipaddress
import json
import time
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

    return ImageFont.load_default()


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

        self.state: Dict[str, Any] = self._load_state()
        self._stop = False
        self._task: asyncio.Task | None = None

        self.http: httpx.AsyncClient | None = None
        self.bytes_cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self.icon_url_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()

    async def initialize(self):
        self.http = httpx.AsyncClient(timeout=15, follow_redirects=True)
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("[steam-monitor] initialized")

    async def terminate(self):
        self._stop = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
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

    def _save_config_safe(self):
        try:
            self.config.save_config()
        except Exception as e:
            logger.warning(f"[steam-monitor] save config failed: {e}")

    def _cache_ttl(self) -> int:
        return max(60, int(self.config.get("cache_ttl_sec", 3600) or 3600))

    def _cache_limit(self, kind: str) -> int:
        if kind == "bytes":
            return max(100, int(self.config.get("cache_max_bytes_items", 512) or 512))
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
        merged = []
        for t in cfg_targets + legacy_targets:
            if t and t not in merged:
                merged.append(t)
        return merged

    def _set_targets(self, targets: List[str]):
        uniq = []
        for t in targets:
            if t and t not in uniq:
                uniq.append(t)
        self.config["push_targets"] = ",".join(uniq)
        self._save_config_safe()

    async def _fetch_players(self, steam_ids: List[str]) -> List[Dict[str, Any]]:
        api_key = self.config.get("steam_api_key", "")
        if not api_key:
            raise RuntimeError("未配置 steam_api_key")

        if not self.http:
            self.http = httpx.AsyncClient(timeout=15, follow_redirects=True)

        uniq_ids = []
        for sid in steam_ids:
            if sid and sid not in uniq_ids:
                uniq_ids.append(sid)

        batch_size = max(10, int(self.config.get("steam_batch_size", 100) or 100))
        players: List[Dict[str, Any]] = []
        for i in range(0, len(uniq_ids), batch_size):
            chunk = uniq_ids[i : i + batch_size]
            params = {"key": api_key, "steamids": ",".join(chunk)}
            r = await self.http.get(STEAM_SUMMARY_API, params=params)
            r.raise_for_status()
            data = r.json()
            players.extend(data.get("response", {}).get("players", []))

        return players

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

    def _with_image_proxy(self, url: str, proxy_prefix: str) -> str:
        prefix = (proxy_prefix or "").strip()
        if not prefix:
            return url

        # 仅允许 http/https 且禁止本地回环/文件协议，避免恶意中转配置
        try:
            parsed = urlparse(prefix)
            scheme = (parsed.scheme or "").lower()
            host = (parsed.hostname or "").lower()
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
            return prefix.replace("{url}", encoded)
        if "%s" in prefix:
            try:
                return prefix % encoded
            except Exception as e:
                logger.warning(f"[steam-monitor] invalid proxy format: {e}")
                return url
        return prefix + encoded

    async def _fetch_url_bytes(
        self,
        url: str,
        proxy_prefix: str = "",
        allowed_types: tuple[str, ...] = ("image/", "application/json"),
        max_bytes: int = 3 * 1024 * 1024,
    ) -> bytes | None:
        if not url:
            return None

        cached = self._cache_get(self.bytes_cache, url)
        if cached is not None:
            return cached

        if not self.http:
            self.http = httpx.AsyncClient(timeout=15, follow_redirects=True)

        candidates = [url]
        if proxy_prefix:
            candidates.append(self._with_image_proxy(url, proxy_prefix))

        for u in candidates:
            try:
                async with self.http.stream("GET", u) as resp:
                    if resp.status_code != 200:
                        continue

                    ctype = (resp.headers.get("content-type") or "").lower()
                    if allowed_types and not any(
                        ctype.startswith(x) for x in allowed_types
                    ):
                        continue

                    clen = resp.headers.get("content-length")
                    if clen:
                        with contextlib.suppress(Exception):
                            if int(clen) > max_bytes:
                                continue

                    buf = bytearray()
                    async for chunk in resp.aiter_bytes(65536):
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            buf = bytearray()
                            break

                    if not buf:
                        continue

                    raw = bytes(buf)
                    self._cache_set(self.bytes_cache, url, raw, "bytes")
                    return raw
            except Exception as e:
                logger.debug(f"[steam-monitor] fetch image bytes failed: {u} err={e}")

        return None

    async def _get_game_icon_url(self, appid: str) -> str | None:
        if not appid:
            return None
        cached = self._cache_get(self.icon_url_cache, appid)
        if cached is not None:
            return cached

        api = f"https://store.steampowered.com/api/appdetails?appids={appid}&l=schinese"
        raw = await self._fetch_url_bytes(
            api,
            allowed_types=("application/json", "text/json", "text/plain"),
            max_bytes=512 * 1024,
        )
        if not raw:
            return None

        try:
            data = json.loads(raw.decode("utf-8", errors="ignore"))
            node = data.get(str(appid), {})
            if not node.get("success"):
                return None
            app = node.get("data", {})
            icon_url = app.get("header_image") or app.get("capsule_image")
            if icon_url:
                self._cache_set(self.icon_url_cache, appid, icon_url, "icon")
            return icon_url
        except Exception as e:
            logger.warning(f"[steam-monitor] parse game icon failed appid={appid}: {e}")
            return None

    def _process_image_bytes(
        self, raw: bytes, size: tuple[int, int], circle: bool = False
    ) -> Image.Image | None:
        try:
            with Image.open(BytesIO(raw)) as opened:
                max_pixels = max(
                    512 * 512,
                    int(self.config.get("max_image_pixels", 4_000_000) or 4_000_000),
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
            return img
        except Exception as e:
            logger.warning(f"[steam-monitor] decode image failed: {e}")
            return None

    async def _load_remote_image(
        self,
        url: str,
        size: tuple[int, int],
        proxy_prefix: str = "",
        circle: bool = False,
    ) -> Image.Image | None:
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
            return None
        return await asyncio.to_thread(self._process_image_bytes, raw, size, circle)

    async def _prepare_assets(
        self, players: List[Dict[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        proxy_prefix = self.config.get(
            "image_proxy_prefix", "https://images.weserv.nl/?url="
        )
        concurrency = max(1, int(self.config.get("asset_concurrency", 6) or 6))
        sem = asyncio.Semaphore(concurrency)

        async def one_player(p: Dict[str, Any]):
            sid = str(p.get("steamid", ""))
            avatar_url = p.get("avatarfull") or p.get("avatarmedium") or p.get("avatar")
            gameid = str(p.get("gameid", "") or "").strip()

            async with sem:
                avatar = await self._load_remote_image(
                    avatar_url or "", (64, 64), proxy_prefix, circle=True
                )

            game_icon = None
            if gameid:
                async with sem:
                    icon_url = await self._get_game_icon_url(gameid)
                if icon_url:
                    async with sem:
                        game_icon = await self._load_remote_image(
                            icon_url, (180, 68), proxy_prefix
                        )

            return sid, {"avatar": avatar, "game_icon": game_icon}

        pairs = await asyncio.gather(
            *(one_player(p) for p in players), return_exceptions=False
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
            line2 = f"状态: {persona_text(state)}" + (
                f" | 游戏: {game}" if game else ""
            )
            draw.text((112, y + 54), line2, fill=(170, 180, 190), font=font_small)

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
        assets = await self._prepare_assets(players)
        return await asyncio.to_thread(self._build_status_image, players, assets)

    async def _push_image(self, umo: str, text: str, image_path: str):
        chain = MessageChain()
        chain.chain = [Comp.Plain(text=text), Comp.Image.fromFileSystem(image_path)]
        await self.context.send_message(umo, chain)

    def _compute_next_interval(self, steam_ids: List[str], default_sec: int) -> int:
        # 基于完整监控集合（配置 ID + state）计算，而不是仅 API 返回列表
        all_ids = []
        for sid in steam_ids + list(self.state.keys()):
            if sid and sid not in all_ids:
                all_ids.append(sid)

        any_online = False
        offline_minutes_max = 0.0
        for sid in all_ids:
            record = self.state.get(sid, {})
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
                steam_ids = parse_ids(self.config.get("steam_ids", ""))
                default_interval = int(self.config.get("poll_interval_sec", 60) or 60)

                if not steam_ids:
                    await asyncio.sleep(max(30, default_interval))
                    continue

                players = await self._fetch_players(steam_ids)
                player_map = {str(p.get("steamid", "")): p for p in players}

                events = []
                now = now_iso()
                for sid in steam_ids:
                    p = player_map.get(sid)
                    if p is None:
                        prev_record = self.state.get(sid, {})
                        prev = prev_record.get("personastate")
                        prev_game = (prev_record.get("gameextrainfo", "") or "").strip()
                        self.state[sid] = {
                            "personaname": prev_record.get("personaname", sid),
                            "personastate": 0,
                            "gameextrainfo": "",
                            "offline_since": prev_record.get("offline_since", now),
                            "ts": now,
                            "missing": True,
                        }
                        if prev is not None and prev != 0:
                            events.append(
                                f"{prev_record.get('personaname', sid)}: 下线（接口未返回）"
                            )
                        elif prev is not None and prev_game:
                            events.append(
                                f"{prev_record.get('personaname', sid)}: 关闭游戏《{prev_game}》（接口未返回）"
                            )
                        continue

                    st = int(p.get("personastate", 0))
                    game = (p.get("gameextrainfo", "") or "").strip()

                    prev_record = self.state.get(sid, {})
                    prev = prev_record.get("personastate")
                    prev_game = (prev_record.get("gameextrainfo", "") or "").strip()

                    offline_since = prev_record.get("offline_since", "")
                    if st == 0:
                        if not (prev == 0 and offline_since):
                            offline_since = now
                    else:
                        offline_since = ""

                    self.state[sid] = {
                        "personaname": p.get("personaname", ""),
                        "personastate": st,
                        "gameextrainfo": game,
                        "offline_since": offline_since,
                        "ts": now,
                        "missing": False,
                    }

                    if prev is None:
                        continue

                    name = p.get("personaname", "?")
                    if prev == 0 and st != 0:
                        events.append(f"{name}: 上线 ({persona_text(st)})")
                    elif prev != 0 and st == 0:
                        events.append(f"{name}: 下线")

                    if st != 0:
                        if not prev_game and game:
                            events.append(f"{name}: 启动游戏《{game}》")
                        elif prev_game and not game:
                            events.append(f"{name}: 关闭游戏《{prev_game}》")
                        elif prev_game and game and prev_game != game:
                            events.append(
                                f"{name}: 切换游戏《{prev_game}》 -> 《{game}》"
                            )

                self._save_state()

                if events:
                    image_path = await self._render_status_image(players)
                    targets = self._get_targets()
                    if targets:
                        text = "Steam 状态变化：" + chr(10) + chr(10).join(events)
                        for umo in targets:
                            try:
                                await self._push_image(umo, text, image_path)
                            except Exception as e:
                                logger.error(f"[steam-monitor] push failed {umo}: {e}")

                next_sleep = self._compute_next_interval(steam_ids, default_interval)
                logger.info(f"[steam-monitor] next poll in {next_sleep}s")
                await asyncio.sleep(next_sleep)
            except asyncio.CancelledError:
                break
            except Exception as e:
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
            finally:
                if image_path:
                    with contextlib.suppress(Exception):
                        Path(image_path).unlink(missing_ok=True)

    @filter.command("sfm_bind")
    async def bind_group(self, event: AstrMessageEvent):

        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return
        umo = event.unified_msg_origin
        targets = self._get_targets()
        if umo not in targets:
            targets.append(umo)
            self._set_targets(targets)
        yield event.plain_result(
            "已绑定当前会话为 Steam 监控推送目标（可在配置页 push_targets 查看）"
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
        if not steam_id64.isdigit() or len(steam_id64) < 10:
            yield event.plain_result("SteamID64 格式不正确")
            return

        ids = parse_ids(self.config.get("steam_ids", ""))
        if steam_id64 not in ids:
            ids.append(steam_id64)
        self.config["steam_ids"] = ",".join(ids)
        self._save_config_safe()
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
        self.config["steam_ids"] = ",".join(ids)
        self._save_config_safe()
        yield event.plain_result(
            f"已移除 SteamID64: {steam_id64}，当前监控数量: {len(ids)}"
        )

    @filter.command("sfm_set_ids")
    async def set_ids(self, event: AstrMessageEvent, ids: str):

        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return
        parsed = parse_ids(ids)
        self.config["steam_ids"] = ",".join(parsed)
        self._save_config_safe()
        yield event.plain_result(f"已设置监控ID数量: {len(parsed)}")

    @filter.command("sfm_status")
    async def status(self, event: AstrMessageEvent):

        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return
        steam_ids = parse_ids(self.config.get("steam_ids", ""))
        if not steam_ids:
            yield event.plain_result("未配置 steam_ids")
            return

        image_path = None
        try:
            players = await self._fetch_players(steam_ids)
            image_path = await self._render_status_image(players)
            msg = chr(10).join(
                [
                    f"{p.get('personaname', '?')}: {persona_text(int(p.get('personastate', 0)))}"
                    for p in players
                ]
            )

            chain = MessageChain()
            chain.chain = [
                Comp.Plain(text="当前状态：" + chr(10) + msg),
                Comp.Image.fromFileSystem(image_path),
            ]
            yield event.chain_result(chain)
        finally:
            if image_path:
                with contextlib.suppress(Exception):
                    Path(image_path).unlink(missing_ok=True)

    @filter.command("sfm_test")
    async def steam_monitor_test(self, event: AstrMessageEvent, action: str = "all"):

        if not self._is_authorized(event):
            yield event.plain_result("无权限执行该命令")
            return
        action = (action or "all").strip().lower()
        steam_ids = parse_ids(self.config.get("steam_ids", ""))
        targets = self._get_targets()

        if action in ("cfg", "config"):
            msg = [
                "[steam_monitor_test: config]",
                f"steam_ids_count={len(steam_ids)}",
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
            image_path = await self._render_status_image(players)
            status_text = chr(10).join(
                [
                    f"{p.get('personaname', '?')}: {persona_text(int(p.get('personastate', 0)))}"
                    + (
                        f" | {p.get('gameextrainfo', '')}"
                        if p.get("gameextrainfo")
                        else ""
                    )
                    for p in players
                ]
            )

            if action in ("status", "pull"):
                yield event.plain_result(
                    "[steam_monitor_test: status]" + chr(10) + status_text
                )
                return

            yield event.plain_result(
                "[steam_monitor_test] 状态拉取成功，发送测试图片中..."
            )
            chain = MessageChain()
            chain.chain = [
                Comp.Plain(text="[steam_monitor_test] 这是测试推送图"),
                Comp.Image.fromFileSystem(image_path),
            ]
            await self.context.send_message(event.unified_msg_origin, chain)

            if action in ("push", "all"):
                ok = 0
                for umo in targets:
                    try:
                        await self._push_image(
                            umo, "[steam_monitor_test] 目标会话测试推送", image_path
                        )
                        ok += 1
                    except Exception as e:
                        logger.error(f"[steam-monitor] test push failed {umo}: {e}")
                if targets:
                    yield event.plain_result(
                        f"[steam_monitor_test] 目标会话测试推送完成: {ok}/{len(targets)}"
                    )
        except Exception as e:
            yield event.plain_result(f"[steam_monitor_test] 执行失败: {e}")
        finally:
            if image_path:
                with contextlib.suppress(Exception):
                    Path(image_path).unlink(missing_ok=True)

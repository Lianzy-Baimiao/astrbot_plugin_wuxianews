# -*- coding: utf-8 -*-
"""天刀公告插件（AstrBot）。作者：lianzy

天刀公告列表 / 天刀最新公告 / 天刀最新公告改（按群去重）/ 天刀重置公告推送，
以及「天刀新闻推送 开/关/状态/测试」定时自动推送（每 5 分钟检查）。

所有指令统一以「天刀」开头，避免与其它插件的裸词指令冲突。
"""

import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path

# AstrBot 以 data.plugins.<name> 模块名加载 main.py，需显式将插件目录加入 sys.path
_PLUGIN_ROOT = Path(__file__).resolve().parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

_TEMPLATE_DIR = _PLUGIN_ROOT / "templates"

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from wxnews import screenshot
from wxnews.groups import (
    SOURCE_API,
    GroupNameResolver,
    parse_group_info,
    parse_group_list,
)
from wxnews.page import NewsPageController

NEWS_URL = "http://wuxia.qq.com/webplat/info/news_version3/5012/5013/5014/5016/m3485/list_1.shtml"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 列表项：标签 / 链接 / 标题 / 时间
_ITEM_RE = re.compile(
    r'<li class="news-st">.*?<a class="cltag"[^>]*>.*?<i>(.*?)</i>.*?</a>.*?'
    r'<a class="cltit"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?'
    r'<span class="cltime">(.*?)</span>.*?</li>',
    re.S,
)
_HTML_TAG_RE = re.compile(r"<[^>]*>")
# 正文容器到「相关新闻」之间即全文；不能用非贪婪 </div>，正文里有嵌套 div 会被截断
_CONTENT_RE = re.compile(r'(?s)<div[^>]*class="artws"[^>]*>(.*?)<dl class="morenews"')
_BLOCK_RE = re.compile(r'(?s)<div[^>]*class="fabric-editor-block-mark[^"]*"[^>]*>(.*?)</div>')
_P_RE = re.compile(r"(?s)<p[^>]*>(.*?)</p>")

T_LIST = r"^天刀公告列表$"
T_LATEST = r"^天刀最新公告$"
T_LATEST_DEDUP = r"^天刀最新公告改$"
T_PUSH = r"^天刀新闻推送[\s:：]*(开|关|状态|测试)?$"
T_RESET = r"^天刀重置公告推送$"

_RE_CACHE: dict[str, re.Pattern] = {}

DATA_DIR: Path | None = None


def _data_dir() -> Path:
    global DATA_DIR
    if DATA_DIR is None:
        DATA_DIR = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_wuxianews"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


class _RateLimit:
    def __init__(self, interval: float):
        self.interval = interval
        self._last: dict[str, float] = {}

    def ok(self, key: str) -> bool:
        now = time.time()
        if now - self._last.get(key, 0) >= self.interval:
            self._last[key] = now
            return True
        return False


async def _http_get(url: str, timeout: float = 15) -> bytes:
    import httpx

    async with httpx.AsyncClient(
        timeout=timeout, headers={"User-Agent": UA}, follow_redirects=True
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


def _decode_gbk(raw: bytes) -> str:
    return raw.decode("gbk", errors="replace")


def _strip_html(s: str) -> str:
    s = _HTML_TAG_RE.sub("", s)
    s = s.replace("&nbsp;", " ").replace("&mdash;", "—")
    return " ".join(s.split()).strip()


async def fetch_news_list() -> list[dict]:
    """公告列表（按时间倒序）。"""
    raw = await _http_get(NEWS_URL)
    html = _decode_gbk(raw)
    items = []
    for m in _ITEM_RE.findall(html):
        tag, href, title, t = m
        title = _strip_html(title).replace("<br>", " ").replace("\n", " ")
        if not title:
            continue
        items.append(
            {
                "tag": _strip_html(tag),
                "title": title,
                "url": ("https://wuxia.qq.com" + href) if not href.startswith("http") else href,
                "time": t.strip(),
            }
        )
    items.sort(key=lambda x: x["time"], reverse=True)
    if not items:
        raise RuntimeError("未找到公告数据（页面结构可能变化）")
    return items


def pick_latest(items: list[dict]) -> dict:
    """优先取「公告」类目，否则取列表第一条。"""
    for it in items:
        if "公告" in it["tag"]:
            return it
    return items[0]


async def fetch_summary(url: str) -> str:
    """公告详情页首段有意义正文（跳过「亲爱的少侠」等称呼段，200 字内）。"""
    try:
        raw = await _http_get(url)
        html = _decode_gbk(raw)
        blocks = _BLOCK_RE.findall(html)
        if not blocks:
            m = _CONTENT_RE.search(html)
            if m:
                blocks = _P_RE.findall(m.group(1))
        for b in blocks:
            text = _strip_html(b)
            if not text:
                continue
            head = text[:10]
            if any(k in head for k in ("亲爱的", "尊敬的", "敬爱的", "各位", "您好", "大家好")):
                continue  # 称呼段无意义
            if len(text) < 20:
                continue  # 过短噪声
            return text[:200]
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _load_records() -> dict:
    try:
        p = _data_dir() / "push_record.json"
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_records(records: dict) -> None:
    try:
        p = _data_dir() / "push_record.json"
        p.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def last_pushed(key: str) -> str:
    return _load_records().get(key, "")


def mark_pushed(key: str, title: str) -> None:
    records = _load_records()
    records[key] = title
    _save_records(records)


def clear_pushed(key: str) -> None:
    records = _load_records()
    records.pop(key, None)
    _save_records(records)


# ---------------- 摘要缓存（多群推送复用，避免重复抓取） ----------------

_SUMMARY_CACHE_FILE = "summary_cache.json"


def _load_summary_cache() -> dict:
    try:
        p = _data_dir() / _SUMMARY_CACHE_FILE
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_summary_cache(cache: dict) -> None:
    try:
        p = _data_dir() / _SUMMARY_CACHE_FILE
        p.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _clean_summary_cache(cache: dict) -> None:
    """防膨胀：只留最近 100 条。"""
    if len(cache) <= 100:
        return
    for k in sorted(cache, key=lambda x: cache[x]["t"])[:-100]:
        cache.pop(k, None)


async def fetch_summary_cached(url: str) -> str:
    """带缓存（24h）的摘要抓取：同一公告推多个群时只抓一次详情页。"""
    cache = _load_summary_cache()
    entry = cache.get(url)
    if entry and time.time() - entry.get("t", 0) < 86400:
        return entry.get("s", "")
    s = await fetch_summary(url)
    cache[url] = {"t": time.time(), "s": s}
    _clean_summary_cache(cache)
    _save_summary_cache(cache)
    return s


class WuxiaNewsPlugin(Star):
    _GROUP_MSG_TYPE = "GroupMessage"

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._limit = _RateLimit(3)
        self._last_check: float = 0
        self._scheduler_task: asyncio.Task | None = None
        self.data_dir = _data_dir()
        screenshot.configure(self.data_dir)
        # 面板：群号 → 群名（OneBot 的群消息事件不带群名，只能自己攒）
        self.groups = GroupNameResolver(self.data_dir / "groups.json")
        self.groups.load()
        self._group_name_tasks: dict[str, asyncio.Task] = {}
        self.page = NewsPageController(context, self, data_dir=self.data_dir)
        self.page.register_routes()
        logger.info("天刀公告插件初始化完成")

    # ---------------- 工具 ----------------

    @staticmethod
    def _cap(pattern: str, event: AstrMessageEvent) -> str:
        rx = _RE_CACHE.get(pattern)
        if rx is None:
            rx = _RE_CACHE[pattern] = re.compile(pattern)
        m = rx.match(event.message_str.strip())
        if not m or not m.groups():
            return ""
        return (m.group(1) or "").strip()

    @staticmethod
    def _group_key(event: AstrMessageEvent) -> str:
        try:
            return f"{event.get_platform_name()}:{event.get_session_id()}"
        except Exception:  # noqa: BLE001
            return event.unified_msg_origin

    @staticmethod
    def _group_id(event: AstrMessageEvent) -> str:
        # 优先用公开 API get_group_id()（跨平台稳定，qq_official 也认），
        # 取不到再退回 message_obj.group_id（老行为，保证不回归）。
        getter = getattr(event, "get_group_id", None)
        if callable(getter):
            try:
                gid = str(getter() or "")
                if gid:
                    return gid
            except Exception:  # noqa: BLE001
                pass
        try:
            return str(event.message_obj.group_id or "")
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        try:
            return event.is_admin()
        except Exception:  # noqa: BLE001
            return False

    def _platform_names(self) -> list[str]:
        for get in (
            lambda: self.context.platform_manager.platform_insts,
            lambda: self.context.get_platform_insts(),
        ):
            try:
                insts = get()
            except Exception:  # noqa: BLE001
                continue
            names = []
            for p in insts or []:
                try:
                    names.append(str(p.meta().name))
                except Exception:  # noqa: BLE001
                    continue
            if names:
                return names
        return []

    def _platform_instances(self) -> list[tuple]:
        """当前已加载平台的 (实例id, 适配器类型名) 列表；取不到返回空。

        兼容两种取法：platform_manager.platform_insts / get_insts()。
        """
        for get in (
            lambda: self.context.platform_manager.platform_insts,
            lambda: self.context.get_platform_insts(),
            lambda: self.context.platform_manager.get_insts(),
        ):
            try:
                insts = list(get() or [])
            except Exception:  # noqa: BLE001
                continue
            out = []
            for p in insts:
                try:
                    meta = p.meta()
                    out.append((str(meta.id), str(meta.name)))
                except Exception:  # noqa: BLE001
                    continue
            if out:
                return out
        return []

    def _bare_id_platform(self) -> str:
        """裸群号要挂到哪个平台**实例 id** 上。

        AstrBot 的 send_message 按实例 id 匹配平台（不是适配器类型名），所以：
        配置填了就用它（是类型名时映射到同类型实例）；留空则优先 aiocqhttp 类型的实例；
        都探不到才回落 aiocqhttp（与旧行为一致）。
        """
        configured = str(self.config.get("default_platform", "") or "").strip() if hasattr(self.config, "get") else ""
        insts = self._platform_instances()
        ids = [pid for pid, _ in insts]
        if configured:
            if not ids or configured in ids:
                return configured
            for pid, ptype in insts:
                if ptype == configured:
                    return pid
            return configured
        for pid, ptype in insts:
            if ptype == "aiocqhttp":
                return pid
        return ids[0] if ids else "aiocqhttp"

    def _norm_umo(self, entry) -> str:
        s = str(entry or "").strip()
        if not s:
            return ""
        if ":" not in s:  # 裸群号 → 补默认平台实例 id
            return f"{self._bare_id_platform()}:{self._GROUP_MSG_TYPE}:{s}"
        # 平台段写成了适配器类型名（aiocqhttp / qq_official）时换成实例 id，否则
        # send_message 按实例 id 匹配不到、静默丢消息；认不出来的原样返回。
        parts = s.split(":")
        plat = parts[0].strip()
        insts = self._platform_instances()
        if plat and not any(plat == pid for pid, _ in insts):
            for pid, ptype in insts:
                if ptype == plat:
                    parts[0] = pid
                    return ":".join(parts)
        return s

    def _push_groups(self) -> list[str]:
        out = []
        for raw in list(self.config.get("news_groups", []) or []):
            umo = self._norm_umo(raw)
            if umo and umo not in out:
                out.append(umo)
        return out

    async def _send_text_to(self, umo: str, text: str) -> None:
        from astrbot.api.event import MessageChain

        await self.context.send_message(umo, MessageChain().message(text))

    async def _send_img_to(self, umo: str, url: str) -> None:
        from astrbot.api.event import MessageChain

        chain = MessageChain()
        if url.startswith(("http://", "https://")):
            chain.url_image(url)
        else:
            chain.file_image(url)
        await self.context.send_message(umo, chain)

    # ---------------- 面板：群名与群列表 ----------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """任何消息都顺手记一下群名（面板要按群名选群）。不产出任何回复。"""
        try:
            self._remember_group(event)
        except Exception as e:  # noqa: BLE001
            logger.debug("记录群名失败: %s", e)

    @staticmethod
    def _event_group_name(event: AstrMessageEvent) -> str:
        """事件自带的群名（Telegram / Discord / QQ 官方等平台有，OneBot 没有）。"""
        group = getattr(getattr(event, "message_obj", None), "group", None)
        return str(getattr(group, "group_name", "") or "").strip()

    @staticmethod
    def _event_platform_id(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_platform_id", None)
        if callable(getter):
            try:
                return str(getter() or "")
            except Exception:  # noqa: BLE001
                pass
        meta = getattr(event, "platform_meta", None)
        return str(getattr(meta, "id", "") or "")

    def _remember_group(self, event: AstrMessageEvent) -> None:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return
        gid = self._group_id(event)
        self.groups.remember(
            umo,
            group_id=gid,
            group_name=self._event_group_name(event),
            platform_id=self._event_platform_id(event),
        )
        if gid and not self.groups.name_of(umo):
            self._schedule_group_name_lookup(event, umo, gid)

    def _schedule_group_name_lookup(self, event: AstrMessageEvent, umo: str, gid: str) -> None:
        """首次见到某个群时后台问一次 get_group_info（不阻塞消息处理）。"""
        if umo in self._group_name_tasks:
            return
        client = getattr(event, "bot", None) or getattr(event, "client", None)
        if client is None or not callable(getattr(client, "call_action", None)):
            return
        try:
            task = asyncio.create_task(
                self._learn_group_name(client, umo, gid, self._event_platform_id(event))
            )
        except RuntimeError:  # 没有运行中的事件循环
            return
        self._group_name_tasks[umo] = task
        task.add_done_callback(lambda _t, key=umo: self._group_name_tasks.pop(key, None))

    async def _learn_group_name(self, client, umo: str, gid: str, platform_id: str) -> None:
        try:
            result = await client.call_action(
                "get_group_info", group_id=int(gid) if str(gid).isdigit() else gid
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("查询群 %s 名称失败: %s", gid, e)
            return
        info = parse_group_info(result)
        name = str(info.get("group_name") or "").strip()
        if not name:
            return
        self.groups.remember(
            umo,
            group_id=gid or str(info.get("group_id") or ""),
            group_name=name,
            platform_id=platform_id,
            member_count=info.get("member_count"),
            source=SOURCE_API,
        )

    @staticmethod
    def _platform_instance_id(inst) -> str:
        meta = getattr(inst, "meta", None)
        if callable(meta):
            try:
                return str(getattr(meta(), "id", "") or "")
            except Exception:  # noqa: BLE001
                pass
        config = getattr(inst, "config", None)
        if isinstance(config, dict):
            return str(config.get("id") or "")
        return ""

    def _platform_clients(self):
        """列出 (平台实例 id, 客户端对象)；取不到平台管理器时一个都不返回。"""
        manager = getattr(self.context, "platform_manager", None)
        getter = getattr(manager, "get_insts", None)
        if not callable(getter):
            return
        try:
            insts = list(getter() or [])
        except Exception as e:  # noqa: BLE001
            logger.debug("取平台实例失败: %s", e)
            return
        for inst in insts:
            client = None
            get_client = getattr(inst, "get_client", None)
            if callable(get_client):
                try:
                    client = get_client()
                except Exception:  # noqa: BLE001
                    client = None
            if client is None:
                client = getattr(inst, "bot", None) or getattr(inst, "client", None)
            if client is not None:
                yield self._platform_instance_id(inst), client

    async def refresh_group_names(self, force: bool = False, interval: int = 300) -> int:
        """去平台要一遍群列表（OneBot get_group_list）补齐群名，返回有变化的群数。"""
        if not force and not self.groups.needs_refresh(interval):
            return 0
        changed = 0
        for platform_id, client in self._platform_clients():
            action = getattr(client, "call_action", None)
            if not callable(action):
                continue
            try:
                result = await action("get_group_list")
            except Exception as e:  # noqa: BLE001
                logger.debug("平台 %s 取群列表失败: %s", platform_id or "?", e)
                continue
            changed += self.groups.merge_api_groups(platform_id, parse_group_list(result))
        self.groups.mark_refreshed()
        return changed

    async def save_config_now(self) -> None:
        """面板改完配置后落盘（同步 / 异步两套 API 都兼容）。"""
        saver = getattr(self.config, "save_config_async", None)
        if callable(saver):
            await saver()
            return
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            saver()

    # ---------------- 指令 ----------------

    @filter.regex(T_LIST)
    async def list_cmd(self, event: AstrMessageEvent):
        '''天刀公告列表：最近 10 条天刀公告'''
        if not self._limit.ok(self._group_key(event)):
            yield event.plain_result("查询太频繁，请稍后再试")
            return
        try:
            items = await fetch_news_list()
            if not items:
                yield event.plain_result("暂无公告信息")
                return
            lines = ["天刀公告列表：\n"]
            for i, it in enumerate(items[:10]):
                lines.append(f"{i + 1}. [{it['tag']}] {it['title']}\n   {it['time']}\n   {it['url']}")
            if len(items) > 10:
                lines.append(f"... 共 {len(items)} 条公告，仅显示前 10 条")
            yield event.plain_result("\n\n".join(lines))
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"获取失败：{e}")

    @filter.regex(T_LATEST)
    async def latest_cmd(self, event: AstrMessageEvent):
        '''天刀最新公告：最新一条 + 内容摘要'''
        if not self._limit.ok(self._group_key(event)):
            yield event.plain_result("查询太频繁，请稍后再试")
            return
        for r in await self._latest(event, force=True):
            yield r

    @filter.regex(T_LATEST_DEDUP)
    async def latest_dedup_cmd(self, event: AstrMessageEvent):
        '''天刀最新公告改：有新公告才返回（按群去重）'''
        if not self._limit.ok(self._group_key(event)):
            yield event.plain_result("查询太频繁，请稍后再试")
            return
        for r in await self._latest(event, force=False):
            yield r

    async def _latest(self, event: AstrMessageEvent, force: bool) -> list:
        key = self._group_id(event) or f"session:{event.unified_msg_origin}"
        results = []
        try:
            items = await fetch_news_list()
            if not items:
                if force:
                    results.append(event.plain_result("暂无公告信息"))
                return results
            it = pick_latest(items)
            if not force and last_pushed(key) == it["title"]:
                return results
            mark_pushed(key, it["title"])
            summary = await fetch_summary(it["url"])
            text = (
                f"最新公告：\n\n类型：{it['tag']}\n标题：{it['title']}\n"
                f"日期：{it['time']}\n链接：{it['url']}"
            )
            if summary:
                text += f"\n\n{summary}"
            img, hint = await self._news_image(it, summary)
            results.append(event.plain_result(text + hint))
            if img:
                results.append(event.image_result(img))
        except Exception as e:  # noqa: BLE001
            logger.warning("最新公告获取失败: %s", e)
            if force:
                results.append(event.plain_result(f"获取失败：{e}"))
        return results

    @filter.regex(T_PUSH)
    async def push_cmd(self, event: AstrMessageEvent):
        '''天刀新闻推送 开/关/状态/测试：本群开启后每 5 分钟检查，有更新自动推送（开/关/测试需管理员）'''
        arg = self._cap(T_PUSH, event)
        if not self._group_id(event):
            yield event.plain_result("该命令仅支持在群聊中使用")
            return
        groups = list(self.config.get("news_groups", []) or [])
        umo = self._norm_umo(self._umo_of(event))
        if arg == "开":
            if not self._is_admin(event):
                yield event.plain_result("需要管理员权限")
                return
            if umo not in groups:
                groups.append(umo)
            self.config["news_groups"] = groups
            self.config.save_config()
            yield event.plain_result("已开启本群天刀公告自动推送（每 5 分钟检查一次，有更新实时推送）")
        elif arg == "关":
            if not self._is_admin(event):
                yield event.plain_result("需要管理员权限")
                return
            self.config["news_groups"] = [g for g in groups if g != umo]
            self.config.save_config()
            yield event.plain_result("已关闭本群天刀公告自动推送")
        elif arg == "状态":
            on = umo in groups
            yield event.plain_result(
                f"本群天刀公告推送：{'已开启（每 5 分钟检查）' if on else '已关闭'}\n"
                "开启后由机器人自动推送，无需手动查询"
            )
        elif arg == "测试":
            if not self._is_admin(event):
                yield event.plain_result("需要管理员权限")
                return
            for r in await self._latest(event, force=True):
                yield r
        else:
            yield event.plain_result("用法：天刀新闻推送 开 / 关 / 状态 / 测试")

    def _umo_of(self, event: AstrMessageEvent) -> str:
        return event.unified_msg_origin

    @filter.regex(T_RESET)
    async def reset_cmd(self, event: AstrMessageEvent):
        '''天刀重置公告推送：清空本群推送记录（需管理员）'''
        if not self._group_id(event):
            yield event.plain_result("该命令仅支持在群聊中使用")
            return
        if not self._is_admin(event):
            yield event.plain_result("需要管理员权限")
            return
        key = self._group_id(event)
        clear_pushed(key)
        yield event.plain_result("已重置本群的公告推送记录")

    # ---------------- 公告图（手机网页截图，失败回退卡片） ----------------

    async def _news_image(self, it: dict, summary: str) -> tuple[str | None, str]:
        """返回 (图片路径/URL, 追加到文字消息的提示)。

        首选「手机 UA + 手机视口打开该网址」的整页截图，与手机看官网一致；
        Playwright 不可用或截图失败时回退到 templates/news.html 卡片图。
        """
        note = ""
        if self.config.get("shot_enabled", True):
            try:
                path, truncated = await screenshot.shot_mobile(
                    it["url"],
                    max_height=int(
                        self.config.get("shot_max_height", screenshot.MAX_HEIGHT)
                    ),
                    quality=int(self.config.get("shot_quality", screenshot.JPEG_QUALITY)),
                )
                screenshot.clean_shots()
                if truncated:
                    note = "\n\n（公告较长，图片仅截取前部分，完整内容见链接）"
                return path, note
            except RuntimeError as e:
                if str(e) == "DOWNLOADING":
                    logger.info("浏览器组件下载中，本次先用卡片图")
                    note = "\n\n（浏览器组件正在后台下载，稍后公告图会自动切换为网页截图）"
                else:
                    logger.warning("公告网页截图失败: %s", e)
            except Exception as e:  # noqa: BLE001
                logger.warning("公告网页截图失败: %s", e)
        try:
            return await self._render_card(it, summary), note
        except Exception as e:  # noqa: BLE001
            logger.warning("公告卡片生成失败: %s", e)
            return None, note

    # ---------------- 公告卡片（html_render 渲染，深色风格，截图不可用时的兜底） ----------------

    @staticmethod
    def _measure_wrap_lines(
        text: str, content_px: float, font_px: float, ascii_ratio: float = 0.55
    ) -> int:
        """按 CSS 像素估算折行后的总行数（CJK 一字≈font_px，ASCII≈font_px*0.55）。"""
        if not text:
            return 0
        import math

        lines = 0
        for para in str(text).split("\n"):
            w = 0.0
            for ch in para:
                w += font_px if ord(ch) > 127 else font_px * ascii_ratio
            lines += max(1, math.ceil(w / content_px))
        return lines

    def _card_clip(self, it: dict, summary: str) -> dict:
        """估算卡片内容高度并构造 clip（t2i 端点固定输出 800x720，需按内容裁剪）。

        与 templates/news.html 版式逐项对应（2026-09-01 实测标定）：
        brand 57 + content pad 26 + tag 24 + title(margin12+行40.6) + meta(10+18)
        + desc(margin18+行30.4) + divider 21 + link 63 + foot 33 + page pad 14。
        底部留 20px 同色余量（深色底不可见），宁多勿少防切字。
        """
        title_lines = self._measure_wrap_lines(it.get("title", ""), 736, 28)
        h = 57 + 26 + 24 + 12 + title_lines * 40.6 + 10 + 18 + 21 + 63 + 33 + 14
        if summary:
            desc_lines = self._measure_wrap_lines(summary, 736, 16)
            h += 18 + desc_lines * 30.4
        return {"x": 0, "y": 0, "width": 800, "height": min(int(h) + 20, 720)}

    async def _render_card(self, it: dict, summary: str) -> str:
        """渲染公告卡片图片（对齐饰品排行深色风格），返回图片 URL。"""
        tmpl = _TEMPLATE_DIR / "news.html"
        if not tmpl.is_file():
            raise RuntimeError(f"模板缺失：{tmpl}。插件目录不完整，请重新安装完整 zip")
        return await self.html_render(
            tmpl.read_text(encoding="utf-8"),
            {
                "tag": it["tag"],
                "title": it["title"],
                "time": it["time"],
                "url": it["url"],
                "desc": summary,
            },
            options={"type": "png", "clip": self._card_clip(it, summary)},
        )

    # ---------------- 定时推送 ----------------

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        if self._scheduler_task is None:
            self._scheduler_task = asyncio.create_task(self._scheduler())

    async def _scheduler(self):
        while True:
            try:
                await self._tick()
            except Exception as e:  # noqa: BLE001
                logger.warning("定时任务异常: %s", e)
            await asyncio.sleep(30)

    async def _tick(self):
        if not self._push_groups() or time.time() - self._last_check < 300:
            return
        self._last_check = time.time()
        await self._push_round()

    async def _push_round(self, target: str | None = None, force: bool = False) -> dict:
        """跑一轮推送：面板「立刻检查并推送」与定时任务共用同一段逻辑。

        target 指定只推一个群；force=True 忽略去重记录重发（面板「测试推送」用）。
        返回 {reason, title, pushed, skipped, message}，pushed/skipped 是 umo 列表。
        """
        if target:
            groups = [self._norm_umo(target)]
        else:
            groups = self._push_groups()
        groups = [u for u in groups if u]
        if not groups:
            return {
                "reason": "no_groups",
                "title": "",
                "pushed": [],
                "skipped": [],
                "message": "还没有设置推送群，先在面板里勾选或群里发「天刀新闻推送 开」",
            }
        try:
            items = await fetch_news_list()
            if not items:
                return {
                    "reason": "no_list",
                    "title": "",
                    "pushed": [],
                    "skipped": [],
                    "message": "公告列表为空（数据源没返回内容）",
                }
            it = pick_latest(items)
        except Exception as e:  # noqa: BLE001
            logger.warning("定时公告列表获取失败: %s", e)
            return {
                "reason": "fetch_failed",
                "title": "",
                "pushed": [],
                "skipped": [],
                "message": f"公告列表获取失败：{e}",
            }

        need = (
            groups
            if force
            else [umo for umo in groups if last_pushed(f"push:{umo}") != it["title"]]
        )
        skipped = [umo for umo in groups if umo not in need]
        if not need:
            return {
                "reason": "up_to_date",
                "title": it["title"],
                "pushed": [],
                "skipped": skipped,
                "message": f"最新公告已经推过了：{it['title']}",
            }

        # 摘要/截图只生成一次（内部有缓存，多群共用同一文件）
        summary = await fetch_summary_cached(it["url"])
        text = (
            f"最新公告：\n\n类型：{it['tag']}\n标题：{it['title']}\n"
            f"日期：{it['time']}\n链接：{it['url']}"
        )
        if summary:
            text += f"\n\n{summary}"
        img, hint = await self._news_image(it, summary)
        text += hint
        pushed: list[str] = []
        for umo in need:
            try:
                mark_pushed(f"push:{umo}", it["title"])
                await self._send_text_to(umo, text)
                if img:
                    await self._send_img_to(umo, img)
                pushed.append(umo)
            except Exception as e:  # noqa: BLE001
                logger.warning("定时公告推送失败 %s: %s", umo, e)
        message = f"已推送「{it['title']}」到 {len(pushed)} 个群"
        if skipped:
            message += f"，{len(skipped)} 个群已是最新（跳过）"
        return {
            "reason": "ok",
            "title": it["title"],
            "pushed": pushed,
            "skipped": skipped,
            "message": message,
        }

    async def push_now(self, target: str | None = None, *, force: bool = False) -> dict:
        """面板「立刻检查并推送」：忽略 5 分钟节流跑一轮。"""
        self._last_check = time.time()
        return await self._push_round(target, force=force)

    async def terminate(self):
        if self._scheduler_task:
            self._scheduler_task.cancel()
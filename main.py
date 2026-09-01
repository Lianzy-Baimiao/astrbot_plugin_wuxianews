# -*- coding: utf-8 -*-
"""天刀公告插件（AstrBot）。

移植自 ZeroBot-Plugin 的 plugin/wuxianews
（https://github.com/FloatTech/ZeroBot-Plugin），逻辑与原版一致：
公告列表 / 最新公告 / 最新公告改（按群去重）/ 重置推送记录，
并新增「天刀新闻推送 开/关/状态/测试」实现定时自动推送（每 5 分钟检查）。
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
_CONTENT_RE = re.compile(r'(?s)<div[^>]*class="artws"[^>]*>(.*?)</div>')
_BLOCK_RE = re.compile(r'(?s)<div[^>]*class="fabric-editor-block-mark[^"]*"[^>]*>(.*?)</div>')
_P_RE = re.compile(r"(?s)<p[^>]*>(.*?)</p>")

T_LIST = r"^公告列表$"
T_LATEST = r"^最新公告$"
T_LATEST_DEDUP = r"^最新公告改$"
T_PUSH = r"^天刀新闻推送[\s:：]*(开|关|状态|测试)?$"
T_RESET = r"^重置公告推送$"

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

    def _norm_umo(self, entry) -> str:
        s = str(entry or "").strip()
        if not s:
            return ""
        if ":" in s:
            return s
        plats = self._platform_names()
        if "aiocqhttp" in plats:
            plat = "aiocqhttp"
        elif plats:
            plat = plats[0]
        else:
            plat = "aiocqhttp"
        return f"{plat}:{self._GROUP_MSG_TYPE}:{s}"

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

    # ---------------- 指令 ----------------

    @filter.regex(T_LIST)
    async def list_cmd(self, event: AstrMessageEvent):
        '''公告列表：最近 10 条天刀公告'''
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
        '''最新公告：最新一条 + 内容摘要'''
        if not self._limit.ok(self._group_key(event)):
            yield event.plain_result("查询太频繁，请稍后再试")
            return
        for r in await self._latest(event, force=True):
            yield r

    @filter.regex(T_LATEST_DEDUP)
    async def latest_dedup_cmd(self, event: AstrMessageEvent):
        '''最新公告改：有新公告才返回（按群去重）'''
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
            results.append(event.plain_result(text))
            card = None
            try:
                card = await self._render_card(it, summary)
            except Exception as e:  # noqa: BLE001
                logger.warning("公告卡片生成失败: %s", e)
            if card:
                results.append(event.image_result(card))
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
        '''重置公告推送：清空本群推送记录（需管理员）'''
        if not self._group_id(event):
            yield event.plain_result("该命令仅支持在群聊中使用")
            return
        if not self._is_admin(event):
            yield event.plain_result("需要管理员权限")
            return
        key = self._group_id(event)
        clear_pushed(key)
        yield event.plain_result("已重置本群的公告推送记录")

    # ---------------- 公告卡片（html_render 渲染，深色风格） ----------------

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
        groups = self._push_groups()
        if not groups or time.time() - self._last_check < 300:
            return
        self._last_check = time.time()
        # 一次抓取，多群复用：先取最新公告，找出所有需要推送的群
        try:
            items = await fetch_news_list()
            if not items:
                return
            it = pick_latest(items)
        except Exception as e:  # noqa: BLE001
            logger.warning("定时公告列表获取失败: %s", e)
            return
        targets = [
            umo for umo in groups if last_pushed(f"push:{umo}") != it["title"]
        ]
        if not targets:
            return
        # 摘要/截图只生成一次（内部有缓存，多群共用同一文件）
        summary = await fetch_summary_cached(it["url"])
        text = (
            f"最新公告：\n\n类型：{it['tag']}\n标题：{it['title']}\n"
            f"日期：{it['time']}\n链接：{it['url']}"
        )
        if summary:
            text += f"\n\n{summary}"
        card = None
        try:
            card = await self._render_card(it, summary)
        except Exception as e:  # noqa: BLE001
            logger.warning("定时公告卡片生成失败: %s", e)
        for umo in targets:
            try:
                mark_pushed(f"push:{umo}", it["title"])
                await self._send_text_to(umo, text)
                if card:
                    await self._send_img_to(umo, card)
            except Exception as e:  # noqa: BLE001
                logger.warning("定时公告推送失败 %s: %s", umo, e)

    async def terminate(self):
        if self._scheduler_task:
            self._scheduler_task.cancel()
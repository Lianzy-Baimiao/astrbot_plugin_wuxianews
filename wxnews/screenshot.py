# -*- coding: utf-8 -*-
"""手机视口网页截图：把公告按「手机访问官网」的样式整页截下来。

为什么必须关掉 JavaScript：
    天刀公告详情页的 head 里会被注入 `if("1" == 1){window.location.href='…';}`，
    JS 一开就被劫持到活动页，截出来的是别的页面。关掉 JS 后停在公告页，正文和
    配图（普通 `<img src>`，无懒加载）依然完整渲染。
    真正的「跳转型公告」（artws 正文为空）由 resolve_target() 主动解析出跳转目标，
    再以开启 JS 的方式截那个活动页 —— 与手机上点进去看到的一致。

浏览器组件由插件首次使用时自动后台下载（playwright install chromium），
无需用户手动安装系统软件，依赖已在 requirements.txt 声明。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

logger = logging.getLogger("astrbot_plugin_wuxianews.screenshot")

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

VIEWPORT_W = 390
VIEWPORT_H = 844
DEVICE_SCALE = 2

MAX_HEIGHT = 5000  # 默认截断高度（CSS px），0 表示不截断
JPEG_QUALITY = 88
MAX_BYTES = 2_500_000  # 单图字节预算，超了自动降质/缩放，避免平台拒收

# head 里的整页跳转：if("1" == 1){window.location.href='https://…';}
_REDIRECT_RE = re.compile(r"window\.location\.href\s*=\s*['\"]([^'\"]+)['\"]")
# 正文容器，空的说明这条公告没有正文、只是个跳转壳
_ARTWS_RE = re.compile(r'(?s)<div[^>]*id="artws"[^>]*>(.*?)<dl class="morenews"')
_BLANK_RE = re.compile(r"<[^>]*>|&nbsp;|\s")

# 腾讯游戏活动页（MILO 框架）未登录时会盖一层扫码登录弹窗，截图前遮掉
_HIDE_CSS = ".milo-qqLogin, .qqLoginCover, .qqLoginContent { display: none !important; }"

_data_dir: Path | None = None
_ready: bool | None = None
_install_started = False

def configure(data_dir: Path) -> None:
    """插件初始化时注入数据目录，截图缓存落在其下的 news_shots/。"""
    global _data_dir
    _data_dir = data_dir


def _shot_dir() -> Path:
    if _data_dir is None:
        raise RuntimeError("screenshot.configure() 未调用")
    d = _data_dir / "news_shots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _shot_paths(url: str) -> tuple[Path, Path]:
    """(完整图路径, 截断图路径)。用文件名区分，省掉额外的标记文件。"""
    h = hashlib.md5(url.encode()).hexdigest()
    d = _shot_dir()
    return d / f"{h}.jpg", d / f"{h}_t.jpg"


def clean_shots(max_keep: int = 20, max_age: int = 7 * 86400) -> None:
    """清理历史截图：最多保留最近 max_keep 张 + 7 天兜底。"""
    try:
        now = time.time()
        files = [f for f in _shot_dir().iterdir() if f.is_file()]
        if len(files) > max_keep:
            for f in sorted(files, key=lambda x: x.stat().st_mtime)[:-max_keep]:
                f.unlink(missing_ok=True)
        for f in _shot_dir().iterdir():
            if f.is_file() and now - f.stat().st_mtime > max_age:
                f.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------- 浏览器组件可用性 ----------------


async def _probe() -> bool:
    """探测 Playwright chromium 是否可用（结果缓存）。"""
    global _ready
    if _ready is not None:
        return _ready
    try:
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        try:
            b = await pw.chromium.launch(args=["--no-sandbox"])
            await b.close()
            _ready = True
        except Exception as e:  # noqa: BLE001
            logger.warning("Playwright chromium 不可用: %s", e)
            _ready = False
        await pw.stop()
    except Exception as e:  # noqa: BLE001
        logger.warning("Playwright 未安装或不可用: %s", e)
        _ready = False
    return _ready

async def _run_install() -> None:
    """后台下载 Playwright chromium（约 150MB，一次性）。"""
    logger.info("开始自动下载 Playwright chromium 浏览器组件（约 150MB）…")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "playwright",
        "install",
        "chromium",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    global _ready
    _ready = None  # 重新探测
    if await _probe():
        logger.info("Playwright chromium 下载完成，公告网页截图已可用")
    else:
        logger.error("Playwright chromium 下载失败，公告图回退为卡片模式")


def request_install() -> bool:
    """触发后台下载（幂等），返回是否已开始。"""
    global _install_started
    if _install_started:
        return False
    _install_started = True
    try:
        asyncio.get_running_loop().create_task(_run_install())
        return True
    except RuntimeError:
        _install_started = False
        return False


# ---------------- 跳转型公告解析 ----------------


async def resolve_target(url: str) -> tuple[str, bool]:
    """返回 (真正要截的 URL, 是否需要开启 JS)。

    公告页正文为空且 head 里有整页跳转脚本 → 这是跳转壳，改截跳转目标（活动页要 JS）。
    其余情况一律截公告页本身，且必须关掉 JS 才不会被那段脚本劫持。
    """
    try:
        import httpx

        async with httpx.AsyncClient(
            timeout=15, headers={"User-Agent": IPHONE_UA}, follow_redirects=True
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        html = resp.content.decode("gbk", errors="replace")
    except Exception as e:  # noqa: BLE001
        logger.warning("跳转检测失败，按普通公告页处理: %s", e)
        return url, False

    m = _REDIRECT_RE.search(html.split("</head>", 1)[0])
    if not m:
        return url, False
    body = _ARTWS_RE.search(html)
    if body and _BLANK_RE.sub("", body.group(1)):
        return url, False  # 有正文，跳转脚本不影响阅读
    target = urljoin(url, m.group(1))
    logger.info("跳转型公告（正文为空），改截活动页: %s", target)
    return target, True

# ---------------- 截图 ----------------


def _shrink_to_budget(path: Path, max_bytes: int) -> None:
    """超出字节预算时先降质、再等比缩小，避免超长公告图被平台拒收。"""
    if max_bytes <= 0 or path.stat().st_size <= max_bytes:
        return
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow 不可用，跳过压缩（当前 %d 字节）", path.stat().st_size)
        return
    with Image.open(path) as im:
        src = im.convert("RGB")
    for quality, scale in ((78, 1.0), (72, 0.75), (66, 0.5), (60, 0.35)):
        img = (
            src
            if scale == 1.0
            else src.resize(
                (max(1, int(src.width * scale)), max(1, int(src.height * scale))),
                Image.LANCZOS,
            )
        )
        img.save(path, format="JPEG", quality=quality, optimize=True)
        if path.stat().st_size <= max_bytes:
            return
    logger.warning("截图压缩后仍有 %d 字节", path.stat().st_size)


async def _page_size(page) -> tuple[int, int]:
    """页面真实宽高（CSS px）。JS 关闭时 evaluate 仍可用（走 Playwright 的隔离世界）。"""
    box = await page.evaluate(
        "() => {const d=document.documentElement, b=document.body||{};"
        "return [Math.max(d.scrollWidth, b.scrollWidth||0),"
        " Math.max(d.scrollHeight, b.scrollHeight||0)];}"
    )
    return int(box[0]), int(box[1])

async def shot_mobile(
    url: str,
    max_height: int = MAX_HEIGHT,
    quality: int = JPEG_QUALITY,
    max_bytes: int = MAX_BYTES,
) -> tuple[str, bool]:
    """手机视口整页截图，返回 (jpg 路径, 是否因过长被截断)。

    按原始 URL 做文件缓存：同一条公告推多个群只截一次。
    浏览器组件未就绪时触发后台下载并抛 RuntimeError("DOWNLOADING")，
    调用方应回退卡片模式并提示稍后自动生效。
    """
    full, trunc = _shot_paths(url)
    for p, was_truncated in ((full, False), (trunc, True)):
        if p.exists() and p.stat().st_size > 0:
            return str(p), was_truncated
    if not await _probe():
        request_install()
        raise RuntimeError("DOWNLOADING")

    target, need_js = await resolve_target(url)
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = None
    try:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        ctx = await browser.new_context(
            viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
            device_scale_factor=DEVICE_SCALE,
            user_agent=IPHONE_UA,
            is_mobile=True,
            has_touch=True,
            locale="zh-CN",
            java_script_enabled=need_js,
        )
        page = await ctx.new_page()
        try:
            await page.goto(target, wait_until="load", timeout=60000)
        except Exception:  # noqa: BLE001
            await page.goto(target, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1800)  # 等字体与正文配图落地
        if need_js:
            # 只有活动页需要（公告页没有登录弹窗）。用 evaluate 而非 add_style_tag：
            # 后者会等 <style> 的 load 事件，JS 关闭的页面上会一直挂住。
            try:
                await page.evaluate(
                    "css => {const s=document.createElement('style');"
                    "s.textContent=css;document.head.appendChild(s);}",
                    _HIDE_CSS,
                )
                await page.wait_for_timeout(300)
            except Exception as e:  # noqa: BLE001
                logger.debug("注入遮罩样式失败（不影响截图）: %s", e)

        width, height = await _page_size(page)
        if width > VIEWPORT_W + 8:
            # 活动页之类没有 viewport meta 的 PC 版排版：把视口拉宽到页面真实宽度再截
            await page.set_viewport_size({"width": width, "height": VIEWPORT_H})
            await page.wait_for_timeout(800)
            width, height = await _page_size(page)

        truncated = max_height > 0 and height > max_height
        out = trunc if truncated else full
        await page.screenshot(
            path=str(out),
            type="jpeg",
            quality=quality,
            full_page=True,
            clip={
                "x": 0,
                "y": 0,
                "width": width,
                "height": min(height, max_height) if max_height > 0 else height,
            },
        )
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("截图文件为空")
        _shrink_to_budget(out, max_bytes)
        (full if truncated else trunc).unlink(missing_ok=True)
        return str(out), truncated
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass
        await pw.stop()




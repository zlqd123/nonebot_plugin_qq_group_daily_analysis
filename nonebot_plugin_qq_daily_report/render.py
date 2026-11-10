# -*- coding: utf-8 -*-
"""海报渲染（scrapbook / atri 主题）+ 纯文字降级。

移植自 bison5 的 ``themes.py`` / ``report.py``。模板与素材复用 astrbot
原插件的主题（scrapbook 走 Google Fonts 字体；atri 素材已全部落盘）。

资源供给：**全部本地化，渲染时零外网依赖**——
- 主题本地素材（atri 的 ``file/`` 目录）→ fastapi 静态路由 ``/gdrq_theme``
- 远程资源（Google Fonts CSS + woff2）→ 启动时预取到本地缓存目录，
  静态路由 ``/gdrq_cache`` 供给，未命中时惰性回源补取
chromium 容器经宿主发布端口（IP:28080）访问这两条路由。
（data URI 内联方案已弃用：3 个 LXGW 字体会把 HTML 撑到 34MB，
set_content 截断后靠后的图片全裂。）

截图通道不走 htmlrender 0.8+ 的 rasterize（本环境实测
``Page.screenshot`` 会挂起），而是照搬 ``extra_plugins/sd_render.py``
的直连 CDP 方案：``connect_over_cdp(http://IP:18069)`` →
``set_content`` → ``full_page`` 截图。single-process 的 chromium 下
**只能用 ``contexts[0].new_page()``**，``browser.new_page()`` 会让整个
浏览器崩溃（详见 chromium 容器的 README.md）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from nonebot import get_driver
from nonebot.log import logger
from starlette.requests import Request
from starlette.routing import Mount

if TYPE_CHECKING:
    from .archive import ChatArchive
    from .stats import ChatStats

THEME_DIR = Path(__file__).parent / "templates" / "themes"

#: 远程资源本地缓存目录（与落库同盘，宿主机可直接查看）。
REMOTE_CACHE_DIR = Path(
    os.environ.get("GDRQ_CACHE_DIR", "/app/extra_plugins/nonebot_plugin_qq_daily_report/cache")
) / "remote_cache"

#: 模拟 Chrome UA：Google Fonts 只给现代 UA 返回 woff2。
_CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

#: 模板里对 Google Fonts 镜像变量的引用形式（预取扫描用）。
_REMOTE_REF_RE = re.compile(
    r"\{\{\s*t2i_google_fonts_mirror\s*\}\}(/[^\"'\s<)]+)"
)


def _host_ip() -> str:
    """chromium 容器可达的宿主机 IP（域名 Host 头会被 chromium 拒绝）。"""
    host = os.environ.get("SD_CDP_HOST", "host.docker.internal")
    try:
        return socket.gethostbyname(host)
    except OSError:
        return host


def _local_base_url() -> str:
    """本插件两条静态路由的基址（chromium 视角）。"""
    try:
        port = get_driver().config.port
    except Exception:
        port = 28080
    return f"http://{_host_ip()}:{port}"


def _asset_base_url(theme: str) -> str:
    """主题本地素材基址：/gdrq_theme/<主题>。"""
    return f"{_local_base_url()}/gdrq_theme/{theme}"


def _cache_base_url() -> str:
    """远程资源缓存基址：/gdrq_cache。"""
    return f"{_local_base_url()}/gdrq_cache"


# ── 远程资源预取与缓存 ─────────────────────────────────────────


def _cache_path_for(url: str) -> Path:
    """远程 URL → 本地缓存路径（query 用 md5 折叠进文件名）。"""
    parts = urlsplit(url)
    name = parts.path.lstrip("/")
    if parts.query:
        digest = hashlib.md5(parts.query.encode()).hexdigest()[:12]
        name += f"@{digest}"
    return REMOTE_CACHE_DIR / parts.netloc / name


def _rewrite_css(text: str) -> str:
    """把 CSS 里的 gstatic 绝对地址改写成根相对的本地缓存路由。"""
    return text.replace("https://fonts.gstatic.com", "/gdrq_cache/fonts.gstatic.com")


async def _fetch_to_cache(
    url: str, *, ua: str | None = None, rewrite_css: bool = False
) -> Path | None:
    """下载远程资源到本地缓存（原子写）；已缓存则直接返回。失败返回 None。"""
    local = _cache_path_for(url)
    if local.is_file() and local.stat().st_size > 0:
        return local
    headers = {"User-Agent": ua} if ua else {}
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
            r = await c.get(url, headers=headers)
            if r.status_code >= 400:
                logger.warning(f"[群日报] 资源预取失败 {r.status_code}: {url[:90]}")
                return None
            content = r.content
    except Exception as e:
        logger.warning(f"[群日报] 资源预取异常: {url[:90]}: {e!s}")
        return None
    if rewrite_css and "text/css" in (r.headers.get("content-type") or ""):
        content = _rewrite_css(r.text).encode()
    local.parent.mkdir(parents=True, exist_ok=True)
    tmp = local.with_name(local.name + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(local)
    return local


def _scan_remote_refs() -> list[str]:
    """扫描全部主题模板里对 Google Fonts 镜像变量的引用路径。"""
    refs: set[str] = set()
    for f in THEME_DIR.glob("*/*.html"):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        refs.update(m.group(1) for m in _REMOTE_REF_RE.finditer(text))
    return sorted(refs)


async def _precache_remote_assets() -> None:
    """启动时预取全部远程资源：css2 样式表 + 其引用的 woff2 字体。"""
    refs = _scan_remote_refs()
    if not refs:
        logger.info("[群日报] 模板无远程资源引用，跳过预取")
        return
    ok = fail = 0
    for path_qs in refs:
        url = f"https://fonts.googleapis.com{path_qs}"
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
                r = await c.get(url, headers={"User-Agent": _CHROME_UA})
            if r.status_code >= 400:
                fail += 1
                logger.warning(
                    f"[群日报] 字体样式预取失败 {r.status_code}: {url[:90]}"
                )
                continue
            css_text = r.text
            # 先从原始 CSS 提取 gstatic 字体地址，再改写成根相对缓存路由
            font_urls = re.findall(
                r"url\((https://fonts\.gstatic\.com/[^)]+)\)", css_text
            )
            css_text = _rewrite_css(css_text)
            local = _cache_path_for(url)
            local.parent.mkdir(parents=True, exist_ok=True)
            tmp = local.with_name(local.name + ".tmp")
            tmp.write_bytes(css_text.encode())
            tmp.replace(local)
            ok += 1
        except Exception as e:
            fail += 1
            logger.warning(f"[群日报] 字体样式预取异常: {url[:90]}: {e!s}")
            continue
        for u in font_urls:
            if await _fetch_to_cache(u, ua=_CHROME_UA) is not None:
                ok += 1
            else:
                fail += 1
    logger.info(
        f"[群日报] 远程资源预取完成: 成功 {ok} 失败 {fail}（{REMOTE_CACHE_DIR}）"
    )


async def _serve_cache(path: str, request: Request) -> Any:
    """/gdrq_cache/{path}：本地缓存供给，未命中时惰性回源补取一次。"""
    from fastapi.responses import FileResponse, Response

    query = request.url.query
    url = "https://" + path + (f"?{query}" if query else "")
    local = _cache_path_for(url)
    if not (local.is_file() and local.stat().st_size > 0):
        is_css = "googleapis" in path
        got = await _fetch_to_cache(url, ua=_CHROME_UA, rewrite_css=is_css)
        if got is None:
            return Response(status_code=404)
        local = got
    if "@" in local.name or local.suffix == ".css":
        media = "text/css"
    elif local.suffix == ".woff2":
        media = "font/woff2"
    else:
        media = "application/octet-stream"
    return FileResponse(local, media_type=media)


@get_driver().on_startup
async def _mount_local_routes() -> None:
    """挂载 /gdrq_theme（主题素材）与 /gdrq_cache（远程资源缓存）。"""
    from fastapi.routing import APIRoute
    from fastapi.staticfiles import StaticFiles

    app = get_driver().server_app
    # 必须插到路由表最前，否则被 SPA 的 mount("/") 兜底截走
    app.routes.insert(
        0, Mount("/gdrq_theme", app=StaticFiles(directory=str(THEME_DIR)))
    )
    app.routes.insert(
        0, APIRoute("/gdrq_cache/{path:path}", _serve_cache, methods=["GET"])
    )
    logger.info(
        f"[群日报] 本地资源路由已挂载: /gdrq_theme, /gdrq_cache（{REMOTE_CACHE_DIR}）"
    )
    # 预取放后台任务，不阻塞启动
    asyncio.get_running_loop().create_task(_precache_remote_assets())


# ── 模板映射 ───────────────────────────────────────────────────

#: 可用主题名（模板目录放进来即可扩展）。
REPORT_STYLES = ("scrapbook", "atri")

#: 需要本地素材的主题：主题名 -> 模板里引用的 mirror 变量名。
#: 变量值由 :func:`_asset_base_url` 生成（/gdrq_theme 静态路由）。
_THEME_LOCAL_MIRRORS = {
    "atri": "t2i_atri_font_mirror",
}

#: 简繁开关。模板用它决定 ``<html lang>`` 和字体优先级。
_FONT_SOURCE = "Mainland"

#: 维度色板，取自 ATRI 模板的主色（quality 条形图用）。
_DIM_COLORS = ("#ff9ec4", "#dcd3ff", "#9cc4e8", "#ffb3c6", "#b9a7e0", "#ff8eaa")


def _env(directory: str):
    """构造一个开了异步的 jinja 环境。"""
    import jinja2

    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(THEME_DIR / directory)), enable_async=True
    )


async def _frag(env: Any, name: str, **ctx: Any) -> str:
    """渲染一个子模板片段。"""
    return await env.get_template(name).render_async(**ctx)


def _dicts(value: Any) -> list[dict[str, Any]]:
    """把模型返回值规整成字典列表。"""
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _chart_data(hourly: list[int]) -> list[dict[str, Any]]:
    """把小时分布转成 ``activity_chart.html`` 认识的结构。

    ``percentage`` 直接当 CSS 高度用，归一到 0~100；``hour`` 必须是数字。
    """
    peak = max(hourly) if hourly else 0
    return [
        {
            "hour": h,
            "count": c,
            "percentage": round(c / peak * 100, 2) if peak else 0,
        }
        for h, c in enumerate(hourly or [0] * 24)
    ]


def clean_topics(topics: Any) -> list[dict[str, Any]]:
    """规整话题列表，丢弃结构不合法的条目。"""
    out: list[dict[str, Any]] = []
    for item in _dicts(topics):
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        users = item.get("users")
        out.append(
            {
                "title": title,
                "detail": str(item.get("detail") or "").strip(),
                "users": [str(u) for u in users] if isinstance(users, list) else [],
            }
        )
    return out


def clean_quotes(quotes: Any) -> list[dict[str, Any]]:
    """规整金句列表。"""
    out: list[dict[str, Any]] = []
    for item in _dicts(quotes):
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        out.append(
            {
                "text": text,
                "user": str(item.get("user") or "").strip(),
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    return out


def clean_titles(titles: Any) -> list[dict[str, str]]:
    """规整用户称号列表。称号名和昵称缺一不可。"""
    out: list[dict[str, str]] = []
    for item in _dicts(titles):
        label = str(item.get("title") or "").strip()
        user = str(item.get("user") or "").strip()
        if not label or not user:
            continue
        out.append(
            {"user": user, "title": label, "reason": str(item.get("reason") or "").strip()}
        )
    return out


def _topics(value: Any) -> list[dict[str, Any]]:
    """把话题映射成 ``topic_item.html`` 的入参形状（上游三层嵌套）。"""
    return [
        {
            "index": i,
            "topic": {"topic": t["title"]},
            "contributors": "、".join(t["users"]),
            "detail": t["detail"],
        }
        for i, t in enumerate(clean_topics(value), start=1)
    ]


def _quotes(value: Any) -> list[dict[str, Any]]:
    """把金句映射成 ``quote_item.html`` 的入参形状。"""
    return [
        {
            "sender": q["user"],
            "content": q["text"],
            "reason": q["reason"],
            "avatar_url": "",
            "avatar_data": "",
        }
        for q in clean_quotes(value)
    ]


def _titles(value: Any) -> list[dict[str, Any]]:
    """把称号映射成 ``user_title_item.html`` 的入参形状。"""
    return [
        {
            "name": t["user"],
            "title": t["title"],
            "reason": t["reason"],
            "profile_display": "",
            "avatar_data": "",
            "profile_image": "",
        }
        for t in clean_titles(value)
    ]


def clean_quality(quality: Any) -> dict[str, Any] | None:
    """规整群聊质量锐评；维度占比归一化到合计 100%。"""
    if not isinstance(quality, dict):
        return None

    dims: list[dict[str, Any]] = []
    for item in _dicts(quality.get("dimensions")):
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        try:
            pct = float(item.get("percentage") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        dims.append(
            {
                "name": name,
                "percentage": max(0.0, pct),
                "comment": str(item.get("comment") or "").strip(),
            }
        )
    if not dims:
        return None

    total = sum(d["percentage"] for d in dims)
    if total <= 0:  # 模型没给占比就平均分配，保证条形图有意义
        share = 100.0 / len(dims)
        for d in dims:
            d["percentage"] = share
    else:  # 归一化，模型给的占比之和未必正好 100
        for d in dims:
            d["percentage"] = d["percentage"] / total * 100.0

    return {
        "title": str(quality.get("title") or "").strip(),
        "subtitle": str(quality.get("subtitle") or "").strip(),
        "summary": str(quality.get("summary") or "").strip(),
        "dimensions": dims,
    }


def _quality(quality: Any) -> dict[str, Any]:
    """组装 ``chat_quality_item.html`` 的入参，并补齐 ``dim.color``。"""
    q = clean_quality(quality)
    if not q:
        return {}
    dims = q["dimensions"]
    for i, dim in enumerate(dims):
        dim["color"] = _DIM_COLORS[i % len(_DIM_COLORS)]
    return {
        "title": q["title"],
        "subtitle": q["subtitle"],
        "summary": q["summary"],
        "dimensions": dims,
    }


async def _build_theme_context(
    style: str,
    archive: ChatArchive,
    stats: ChatStats,
    analysis: dict[str, Any],
) -> tuple[dict[str, Any], str] | None:
    """组装上游模板渲染需要的全部入参。"""
    if style not in REPORT_STYLES:
        logger.warning(f"[群日报] 未知海报主题 {style!r}")
        return None

    directory = style
    env = _env(directory)

    # 子模板同样引用 mirror 变量，必须和主模板拿到同一份；漏传时 Jinja
    # 把未定义变量渲染成空串，资源请求必然 404 且不报错。
    frag_ctx: dict[str, Any] = {
        "t2i_google_fonts_mirror": _cache_base_url() + "/fonts.googleapis.com",
        "t2i_gstatic_mirror": _cache_base_url() + "/fonts.gstatic.com",
        "t2i_font_source": _FONT_SOURCE,
    }
    if local := _THEME_LOCAL_MIRRORS.get(style):
        frag_ctx[local] = _asset_base_url(style)

    chart = ""
    if any(stats.hourly):
        chart = await _frag(
            env, "activity_chart.html", chart_data=_chart_data(stats.hourly), **frag_ctx
        )

    topics_html = await _frag(
        env, "topic_item.html", topics=_topics(analysis.get("topics")), **frag_ctx
    )
    quotes_html = await _frag(
        env, "quote_item.html", quotes=_quotes(analysis.get("quotes")), **frag_ctx
    )
    titles_html = await _frag(
        env, "user_title_item.html", titles=_titles(analysis.get("titles")), **frag_ctx
    )

    quality_html = ""
    qctx = _quality(analysis.get("quality"))
    if qctx:
        quality_html = await _frag(env, "chat_quality_item.html", **qctx, **frag_ctx)

    peak = stats.peak_hour
    ctx: dict[str, Any] = {
        **frag_ctx,
        "current_date": archive.dt_text.split(" ")[0],
        "current_datetime": archive.dt_text,
        "group_name": archive.group_name or f"群 {archive.group_openid[:12]}…",
        "message_count": stats.total_messages,
        "participant_count": stats.participant_count,
        "total_characters": stats.total_chars,
        "emoji_count": stats.emoji.total,
        "most_active_period": f"{peak:02d}:00 - {(peak + 1) % 24:02d}:00",
        "hourly_chart_html": chart,
        "topics_html": topics_html,
        "quotes_html": quotes_html,
        "titles_html": titles_html,
        "chat_quality_html": quality_html,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    return ctx, directory


def _safe_style(style: str) -> str:
    """校验风格名，非法时回落到默认的 ``scrapbook``。"""
    name = (style or "").strip().lower()
    if name in REPORT_STYLES:
        return name
    logger.warning(f"[群日报] 未知海报主题 {style!r}，回落到 scrapbook")
    return "scrapbook"


# ── 截图 ───────────────────────────────────────────────────────


async def _render_html_to_pic(
    directory: str, ctx: dict[str, Any], width: int
) -> bytes:
    """HTML → PNG：按 ``GDRQ_RENDER_BACKEND`` 选择渲染通道。

    auto：优先 htmlrender 的 ``template_to_pic``（0.7.x 老 API，
    bison5 环境）；ImportError / 渲染失败时回落直连 chromium CDP
    （bison2 环境）。资源 URL 都是本插件静态路由的绝对地址，
    两条通道通用。
    """
    backend = os.environ.get("GDRQ_RENDER_BACKEND", "auto").strip().lower()
    if backend in ("auto", "htmlrender"):
        try:
            from nonebot import require

            require("nonebot_plugin_htmlrender")
            from nonebot_plugin_htmlrender import template_to_pic

            return await template_to_pic(
                template_path=str(THEME_DIR / directory),
                template_name="image_template.html",
                templates=ctx,
                pages={"viewport": {"width": width, "height": 10}},
            )
        except ImportError:
            if backend == "htmlrender":
                raise
            logger.info("[群日报] htmlrender 无 template_to_pic（0.8+），走 CDP")
        except Exception as e:
            if backend == "htmlrender":
                raise
            logger.warning(f"[群日报] htmlrender 渲染失败，回落 CDP: {e!s}")

    html = await _env(directory).get_template("image_template.html").render_async(
        **ctx
    )
    return await _screenshot(html, width)


async def _screenshot(html: str, width: int) -> bytes:
    """连接 chromium CDP 渲染 HTML 并整页截图；不关共享的 browser 连接。

    仅可用 ``contexts[0].new_page()``——single-process 的 chromium 下
    ``browser.new_page()`` 会让整个浏览器崩溃。
    """
    from playwright.async_api import async_playwright

    host = os.environ.get("SD_CDP_HOST", "host.docker.internal")
    try:
        host = socket.gethostbyname(host)
    except OSError:
        pass
    port = os.environ.get("SD_CDP_PORT", "18069")
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://{host}:{port}")
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({"width": width, "height": 10})
            await page.set_content(html, wait_until="networkidle")
            await page.evaluate("() => document.fonts.ready")
            return await page.screenshot(full_page=True, type="png", timeout=60_000)
        finally:
            await page.close()


async def render_report_image(
    archive: ChatArchive,
    stats: ChatStats,
    analysis: dict[str, Any],
    *,
    width: int = 900,
    style: str = "scrapbook",
) -> bytes | None:
    """把日报渲染为 PNG 图片字节；渲染失败时返回 ``None``。"""
    style = _safe_style(style)
    built = await _build_theme_context(style, archive, stats, analysis)
    if built is None:
        return None
    ctx, directory = built

    try:
        return await _render_html_to_pic(directory, ctx, width)
    except Exception as e:
        logger.error(f"[群日报] {style} 主题渲染失败: {e!s}")
        return None


# ── 分段渲染（一图分二）────────────────────────────────────────

_SPLIT_MARKER_ID = "gdr-split-marker"

_SPLIT_SCRIPT = """
  const m = document.getElementById('gdr-split-marker');
  if (!m) return 'no-marker';
  const hide = (el) => { el.style.display = 'none'; };
  const forward = mode === 'after';
  // 把标记之后（或之前）的内容逐层收集起来：
  // 先收标记的同级，再收父级的同级，直到根。
  const list = [];
  let node = m;
  while (node && node !== document.documentElement) {
    let sib = forward ? node.nextElementSibling : node.previousElementSibling;
    while (sib) {
      list.push(sib);
      sib = forward ? sib.nextElementSibling : sib.previousElementSibling;
    }
    node = node.parentElement;
  }
  list.forEach(hide);
  hide(m);
  if (!forward) {
    // 前半段隐藏后，标记所在的区块往往只剩题头——话题区的
    // 「高亮记忆碎片」标题属于上半图，下半图应从「神人名片颁发」开始。
    let p = m.parentElement;
    while (p && p !== document.body) {
      const hasAfter = Array.from(p.querySelectorAll('*')).some(
        (c) =>
          c !== m &&
          c.style.display !== 'none' &&
          (m.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING)
      );
      if (hasAfter) break;
      hide(p);
      p = p.parentElement;
    }
  }
  return 'ok';
"""


async def render_report_image_split(
    archive: ChatArchive,
    stats: ChatStats,
    analysis: dict[str, Any],
    *,
    width: int = 900,
    style: str = "scrapbook",
) -> list[bytes] | None:
    """渲染海报并切成上下两段；不适合分段或渲染失败时返回 ``None``。"""
    style = _safe_style(style)
    built = await _build_theme_context(style, archive, stats, analysis)
    if built is None:
        return None
    ctx, directory = built

    topics_html = str(ctx.get("topics_html") or "")
    if not topics_html.strip():
        return None
    if not any(
        str(ctx.get(k) or "").strip()
        for k in ("titles_html", "quotes_html", "chat_quality_html")
    ):
        return None
    ctx["topics_html"] = (
        topics_html
        + f'<div id="{_SPLIT_MARKER_ID}" style="height:0;overflow:hidden"></div>'
    )

    # 先渲染成整页 HTML，再把隐藏脚本注入 </body> 前——内联脚本在页面
    # 解析时同步执行，截图前必然已经完成。
    base_html = await _env(directory).get_template("image_template.html").render_async(
        **ctx
    )

    async def _shot(mode: str) -> bytes:
        script = (
            "<script>(()=>{const mode=" + json.dumps(mode) + ";" + _SPLIT_SCRIPT + "})();</script>"
        )
        html = base_html.replace("</body>", script + "</body>", 1)
        return await _screenshot(html, width)

    try:
        top = await _shot("after")
        bottom = await _shot("before")
        return [top, bottom]
    except Exception as e:
        logger.error(f"[群日报] {style} 主题分段渲染失败: {e!s}")
        return None


# ── 纯文字版 ───────────────────────────────────────────────────


def build_text_summary(
    analysis: dict[str, Any], stats: ChatStats, window_label: str = "今日"
) -> str:
    """生成纯文字版日报。"""
    lines: list[str] = [
        f"【群聊日报 · {window_label}】",
        f"时间：{stats.time_range_text}",
        f"消息 {stats.total_messages} 条 · 活跃 {stats.participant_count} 人"
        + (
            f"（{stats.active_ratio * 100:.1f}%）" if stats.member_count else ""
        )
        + f" · 最活跃 {stats.peak_hour:02d} 时",
        f"贡献前三：{stats.top_user_text}",
        "",
        "【整体总结】",
        str(analysis.get("summary") or "（无）"),
    ]

    mood = str(analysis.get("mood") or "").strip()
    if mood:
        lines += ["", f"【氛围】{mood}"]

    quality = clean_quality(analysis.get("quality"))
    if quality:
        lines += ["", f"【群聊锐评】{quality['title']} · {quality['subtitle']}"]
        for d in quality["dimensions"]:
            lines.append(f"  · {d['name']} {d['percentage']:.0f}% —— {d['comment']}")
        if quality["summary"]:
            lines.append(f"  {quality['summary']}")

    topics = clean_topics(analysis.get("topics") or [])
    if topics:
        lines += ["", "【话题】"]
        for i, topic in enumerate(topics, 1):
            lines.append(f"{i}. {topic['title']}")
            if topic["detail"]:
                lines.append(f"   {topic['detail']}")
            if topic["users"]:
                lines.append(f"   关键用户：{'、'.join(topic['users'])}")

    titles = clean_titles(analysis.get("titles") or [])
    if titles:
        lines += ["", "【今日称号】"]
        for t in titles:
            why = f"（{t['reason']}）" if t["reason"] else ""
            lines.append(f"{t['user']}：{t['title']}{why}")

    quotes = clean_quotes(analysis.get("quotes") or [])
    if quotes:
        lines += ["", "【金句】"]
        for q in quotes:
            who = f"{q['user']}：" if q["user"] else ""
            lines.append(f"“{q['text']}” ——{who}")

    return "\n".join(lines)

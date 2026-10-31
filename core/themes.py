"""海报主题渲染：直接复用 astrbot 原插件的模板与素材。

移植自 nonebot 版：6 套主题（``templates/themes/``）的上下文组装逻辑
**原样保留**，差异只在渲染出口——nonebot 用 ``template_to_pic`` +
Playwright 手控页面，MaiBot 插件没有页面控制权，改经 plugin.py 注入的
渲染钩子调宿主 ``ctx.render.html2png``。

两处为宿主渲染器做的适配：

1. **本地素材内联**。nonebot 版靠 ``page.goto(file://模板目录)`` 建立相对
   路径基准；宿主的 html2png 拿到的只是一段 HTML 字符串，基准 URL 不可控
   （file:// 子资源还可能被浏览器安全策略拦掉）。所以渲染前把模板 HTML 里
   指向主题目录内的相对资源（图片/字体，ATRI 的 24MB 字体也在内）统一
   改写为 base64 data URI，彻底自包含。Google 字体仍走公网——本地没有副本。
2. **分段渲染**。没有 ``page.evaluate``，改为把「隐藏标记前/后 DOM」的
   脚本直接嵌进 HTML（脚本在 ``</body>`` 前，此时全部内容已解析）。
   若宿主渲染器不执行脚本，两次截图结果会完全一致——据此判定分段
   不可用并返回 ``None``，调用方自动退回整图。
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import asset_server
from .log import logger

THEME_DIR = Path(__file__).resolve().parent.parent / "templates" / "themes"

#: 可用主题名（与 report.style 的取值一致）。
#:
#: 只收上游 README 列出的 6 套。上游 templates/ 下另有 simple、
#: spring_festival、BlueArchive，但不在其 README 清单内；而 ``format``
#: 本身只是个基础模板（没有自己的配色），所以都没搬。
REPORT_STYLES = (
    "scrapbook",  # 手账（默认）
    "retro",  # 复古未来
    "miku",  # 初音未来
    "hack",  # 赛博
    "atri",  # 亚托莉
    "nouveau",  # 新艺术运动
)

#: 主题名 -> 该主题独有的入参。
#:
#: 主题目录名与主题名一致（``templates/themes/<name>/``），上游那套
#: ``ATRI`` / ``retro_futurism`` / ``art_nouveau`` 之类的目录名只在构建期用。
#:
#: ``local_mirror`` 给 ATRI/Miku 用：上游原本指向一个 GitHub CDN，这里指向
#: 模板目录自身。素材由 :func:`_inline_assets` 内联进 HTML，从本地磁盘读。
#:
#: 字体 CDN 沿用上游默认值，本地没有副本。
_THEMES: dict[str, dict[str, Any]] = {
    "scrapbook": {},
    "retro": {},
    "hack": {},
    "atri": {"local_mirror": "t2i_atri_font_mirror"},
    "miku": {
        "local_mirror": "t2i_miku_assets_mirror",
        "extra": {"t2i_npm_mirror": "https://cdn.jsdelivr.net/npm"},
    },
    "nouveau": {},
}

_FONT_MIRRORS = {
    "t2i_google_fonts_mirror": "https://fonts.googleapis.com",
    "t2i_gstatic_mirror": "https://fonts.gstatic.com",
}

#: 简繁开关。6 套模板里有 5 套用它决定 ``<html lang>`` 和字体优先级：
#: ``Mainland`` 出 zh-CN 且简体在前，其它值出 zh-Hant 且繁体在前。
#: 漏掉这个键，jinja 会判为未定义，简体群就渲染成繁体字体顺序了。
_FONT_SOURCE = "Mainland"

#: 维度色板。取自 ATRI 模板自身出现最多的主色（粉、薰衣草、浅蓝），
#: 而不是另配一套——只有 ATRI 用到，但统一提供省得按主题分支。
_DIM_COLORS = ("#ff9ec4", "#dcd3ff", "#9cc4e8", "#ffb3c6", "#b9a7e0", "#ff8eaa")

# ── 渲染钩子（plugin.py 注入，包一层宿主 ctx.render.html2png）────────
#
# 签名：async (html: str, width: int) -> bytes | None（PNG 字节）。
_HTML_RENDERER: Callable[[str, int], Awaitable[bytes | None]] | None = None


def set_html_renderer(fn: Callable[[str, int], Awaitable[bytes | None]]) -> None:
    """注册宿主渲染钩子（plugin.py 在 on_load 时调用）。

    Args:
        fn: ``async (html, width) -> PNG bytes | None``。
    """
    global _HTML_RENDERER
    _HTML_RENDERER = fn


def _env(directory: str):
    """构造一个开了异步的 jinja 环境。

    Args:
        directory: 主题目录名。

    Returns:
        jinja2 环境实例。
    """
    import jinja2

    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(THEME_DIR / directory)), enable_async=True
    )


async def _frag(env: Any, name: str, **ctx: Any) -> str:
    """渲染一个子模板片段。

    Args:
        env: jinja 环境。
        name: 子模板文件名。
        **ctx: 模板变量。

    Returns:
        渲染后的 HTML 字符串。
    """
    return await env.get_template(name).render_async(**ctx)


def _dicts(value: Any) -> list[dict[str, Any]]:
    """把模型返回值规整成字典列表。

    Args:
        value: 模型返回值。

    Returns:
        仅含 dict 元素的列表。
    """
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _chart_data(hourly: list[int]) -> list[dict[str, Any]]:
    """把小时分布转成 ``activity_chart.html`` 认识的结构。

    上游把 ``percentage`` 直接当 CSS 高度用，所以归一到 0~100。
    ``hour`` 必须是数字：8 套主题里有 7 套用 ``"%02d" | format(item.hour)``，
    传字符串会直接 TypeError。上游也是共用同一个 ActivityVisualizer 喂全部
    主题，所以这里跟上游保持一致用 int（ATRI 那套因此显示 03 而非 3，与上游
    行为相同）。

    Args:
        hourly: 24 个小时的消息数。

    Returns:
        ``[{"hour", "count", "percentage"}, ...]``。
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
    """规整话题列表，丢弃结构不合法的条目。

    Args:
        topics: 模型返回的 topics 字段。

    Returns:
        ``[{"title", "detail", "users"}, ...]``。
    """
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
    """规整金句列表。

    Args:
        quotes: 模型返回的 quotes 字段。

    Returns:
        ``[{"text", "user", "reason"}, ...]``。
    """
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
    """规整用户称号列表。称号名和昵称缺一不可。

    Args:
        titles: 模型返回的 titles 字段。

    Returns:
        ``[{"user", "title", "reason"}, ...]``。
    """
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
    """把话题映射成 ``topic_item.html`` 的入参形状。

    上游是 ``topic.topic.topic`` 三层嵌套（条目 / 话题体 / 标题）。
    话题体还有一个可选的 ``count``，用于「讨论热度 N 次提及」——本插件
    不统计提及次数，故不提供该键，模板自带的 ``is defined`` 会跳过该行。

    Args:
        value: 模型返回的 topics 字段。

    Returns:
        模板可用的条目列表。
    """
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
    """把金句映射成 ``quote_item.html`` 的入参形状。

    Args:
        value: 模型返回的 quotes 字段。

    Returns:
        模板可用的条目列表。
    """
    return [
        {
            "sender": q["user"],
            "content": q["text"],
            "reason": q["reason"],
            # 本插件不取 QQ 头像，模板对空值有 if 保护
            "avatar_url": "",
            "avatar_data": "",
        }
        for q in clean_quotes(value)
    ]


def _titles(value: Any) -> list[dict[str, Any]]:
    """把称号映射成 ``user_title_item.html`` 的入参形状。

    ``profile_display`` 是上游的人格标签/MBTI 字段，按约定不做人格迁移，
    留空后模板会渲染出「人格标签:」的空行。

    Args:
        value: 模型返回的 titles 字段。

    Returns:
        模板可用的条目列表。
    """
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
    """规整群聊质量锐评。

    结构沿用 astrbot 的 ``QualityReview``：主题标题 + 副标题 + 若干抽象维度
    （各带占比与点评）+ 一句总结。维度占比为方便画条形图，会归一化到合计 100%。

    Args:
        quality: 模型返回的 quality 字段。

    Returns:
        规整后的字典；内容不足时返回 ``None``。
    """
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
    """组装 ``chat_quality_item.html`` 的入参，并补齐 ``dim.color``。

    上游直接用 ``dim.color`` 且没有 ``default`` 兜底，缺值会让
    ``color-mix()`` 整条失效，所以颜色必须在这里给。

    Args:
        quality: 规整后的质量分析字典。

    Returns:
        模板入参；``quality`` 为空时返回空字典。
    """
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
    archive: Any,
    stats: Any,
    analysis: dict[str, Any],
) -> tuple[dict[str, Any], str] | None:
    """组装上游模板渲染需要的全部入参。

    6 套主题的入参高度统一（16 个共享变量 + 各主题自己的 mirror），
    所以整份上下文只在这里构建一次，:func:`render_theme_image` 和
    :func:`render_theme_image_split` 共用。

    Args:
        style: 主题名，见 :data:`REPORT_STYLES`。
        archive: 消息采集结果。
        stats: 统计结果。
        analysis: 文字模型返回的结构化分析。

    Returns:
        ``(模板入参, 主题目录名)``；主题未知时返回 ``None``。
    """
    conf = _THEMES.get(style)
    if conf is None:
        logger.warning(f"[群日报] 未知海报主题 {style!r}")
        return None

    directory = style
    env = _env(directory)

    # 子模板同样可能引用 mirror 变量，**必须和主模板拿到同一份**。
    # 漏传时 Jinja 把未定义变量渲染成空串，于是
    # ``{{ t2i_atri_font_mirror }}/file/x.gif`` 变成 ``/file/x.gif``——
    # 文件系统根目录的绝对路径，图片必然裂，而且不报错。
    # ATRI 的 quote_item / user_title_item 正是这么写的。
    frag_ctx: dict[str, Any] = {
        **_FONT_MIRRORS,
        "t2i_font_source": _FONT_SOURCE,
        **conf.get("extra", {}),
    }
    if local := conf.get("local_mirror"):
        frag_ctx[local] = "."

    chart = ""
    if any(stats.hourly):
        chart = await _frag(
            env, "activity_chart.html", chart_data=_chart_data(stats.hourly), **frag_ctx
        )

    # 三个条目子模板都自带 {% for %}，所以整份列表交给它们渲染
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
        **_FONT_MIRRORS,
        "t2i_font_source": _FONT_SOURCE,
        **conf.get("extra", {}),
        "current_date": archive.dt_text.split(" ")[0],
        "current_datetime": archive.dt_text,
        "group_name": archive.group_name or f"群 {archive.group_id}",
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
        # 本插件不统计 token 用量，上游模板只在页脚显示
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    if local := conf.get("local_mirror"):
        # 指向模板目录自身：素材由 _inline_assets 从本地磁盘读并内联
        ctx[local] = "."
    return ctx, directory


# ── 本地素材改写 ──────────────────────────────────────────────

_MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".bmp": "image/bmp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
}

#: 小于该字节的文件直接内联为 data URI（免一次 HTTP 往返）；更大的
# （ATRI 的 24MB 字体）经本地素材服务器供给——内联会把 HTML 撑爆
# 16MB 的 RPC 帧上限，渲染请求根本到不了浏览器。
_INLINE_MAX_BYTES = 512 * 1024

_SKIP_PREFIXES = ("http://", "https://", "data:", "#", "//", "about:", "javascript:", "mailto:")

_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)")
_ATTR_RE = re.compile(r"\b(src|href)\s*=\s*(['\"])([^'\"]+)\2")


def _to_data_uri(path: Path) -> str | None:
    """把本地文件转成 data URI；读取失败返回 ``None``。"""
    try:
        if not path.is_file():
            return None
        data = path.read_bytes()
    except OSError:
        return None
    mime = _MIME_MAP.get(path.suffix.lower(), "application/octet-stream")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _resolve_local(base: Path, ref: str) -> Path | None:
    """把相对引用解析到主题目录内的文件；越界/远程引用返回 ``None``。"""
    ref = ref.strip().strip("'\"")
    if not ref or ref.lower().startswith(_SKIP_PREFIXES) or ref.startswith("file://"):
        return None
    ref = ref.split("#", 1)[0].split("?", 1)[0]
    if not ref:
        return None
    try:
        candidate = (base / ref).resolve()
    except (OSError, ValueError):
        return None
    # 只处理主题目录内的文件——防拼出来的路径越界读盘
    if base.resolve() not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def _inline_assets(html: str, directory: str) -> str:
    """把 HTML 里指向主题目录的相对资源引用改写为可访问地址。

    小文件（图片等 ≤512KB）内联为 data URI；大文件（ATRI 的 24MB 字体）
    改写到本地素材服务器的 URL——它们经 :mod:`core.asset_server` 由
    127.0.0.1 供给，避免 HTML 超过宿主 16MB 的 RPC 帧上限。
    远程资源（Google 字体等）与已有 data URI 原样保留。
    服务器未启动时全部尝试内联（小主题够用；atri 大字体会失败，
    依赖调用方按帧错误自行降级）。

    Args:
        html: 渲染好的完整 HTML。
        directory: 主题目录名（资源解析基准）。

    Returns:
        改写后的 HTML。
    """
    base = THEME_DIR / directory
    server_root = THEME_DIR.resolve()
    base_url = asset_server.get_asset_base_url()

    def _sub_url(m: re.Match[str]) -> str:
        path = _resolve_local(base, m.group(2))
        if path is None:
            return m.group(0)
        rel = path.relative_to(server_root).as_posix()
        if base_url:
            replacement = f"{base_url}/{rel}"
        elif path.stat().st_size <= _INLINE_MAX_BYTES:
            uri = _to_data_uri(path)
            if uri is None:
                return m.group(0)
            replacement = uri
        else:
            logger.warning(
                f"[群日报] 素材 {rel} 过大且素材服务器未启动，跳过（渲染可能缺字体）"
            )
            return m.group(0)
        return m.group(0).replace(m.group(2), replacement)

    def _sub_attr(m: re.Match[str]) -> str:
        path = _resolve_local(base, m.group(3))
        if path is None:
            return m.group(0)
        rel = path.relative_to(server_root).as_posix()
        if base_url:
            replacement = f"{base_url}/{rel}"
        elif path.stat().st_size <= _INLINE_MAX_BYTES:
            uri = _to_data_uri(path)
            if uri is None:
                return m.group(0)
            replacement = uri
        else:
            return m.group(0)
        return f"{m.group(1)}={m.group(2)}{replacement}{m.group(2)}"

    html = _CSS_URL_RE.sub(_sub_url, html)
    html = _ATTR_RE.sub(_sub_attr, html)
    return html


async def _render_html(html: str, width: int, style: str) -> bytes | None:
    """内联本地素材后调宿主渲染钩子出图。

    Args:
        html: 渲染好的完整 HTML。
        width: 渲染宽度（像素）。
        style: 主题名（内联基准目录）。

    Returns:
        PNG 图片字节；渲染钩子未注册或渲染失败时返回 ``None``。
    """
    if _HTML_RENDERER is None:
        logger.error(
            "[群日报] 宿主渲染钩子未注册（html2png 不可用），无法生成海报"
        )
        return None
    html = _inline_assets(html, style)
    try:
        return await _HTML_RENDERER(html, width)
    except Exception as e:
        logger.error(f"[群日报] {style} 主题渲染失败: {e!s}")
        return None


async def render_theme_image(
    archive: Any,
    stats: Any,
    analysis: dict[str, Any],
    *,
    style: str = "scrapbook",
    width: int = 900,
) -> bytes | None:
    """用上游模板渲染指定主题的海报。

    Args:
        archive: 消息采集结果。
        stats: 统计结果。
        analysis: 文字模型返回的结构化分析。
        style: 主题名，见 :data:`REPORT_STYLES`。
        width: 渲染宽度（像素）。

    Returns:
        PNG 图片字节；主题未知或渲染失败时返回 ``None``。
    """
    built = await _build_theme_context(style, archive, stats, analysis)
    if built is None:
        return None
    ctx, directory = built

    try:
        html = await _env(directory).get_template("image_template.html").render_async(
            **ctx
        )
    except Exception as e:
        logger.error(f"[群日报] {style} 主题模板渲染失败: {e!s}")
        return None
    return await _render_html(html, width, directory)


#: 分段渲染时插在「话题」片段末尾的标记。分隔点固定在这里——
#: 即「高亮记忆碎片」结束之后、「神人名片颁发」之前。
_SPLIT_MARKER_ID = "gdr-split-marker"

# 「隐藏标记一侧 DOM」的脚本主体（移植自 nonebot 版传给 page.evaluate 的
# 同一段 JS）。宿主渲染器没有 page.evaluate，所以这里把它包成 IIFE 直接
# 嵌进 HTML —— 插在 </body> 前，此时文档全部内容已解析完毕。
_SPLIT_JS_BODY = """
  const m = document.getElementById('__MARKER_ID__');
  if (!m) return;
  const hide = (el) => { el.style.display = 'none'; };
  const forward = mode === 'after';
  // 把标记之后（或之前）的内容逐层收集起来：
  // 先收标记的同级，再收父级的同级，直到根。这样文档序里
  // 标记一侧的全部内容都被隐藏，另一侧原样保留。
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
    // 「高亮记忆碎片」标题属于上半图，下半图应从「神人名片颁发」
    // 开始。判定：容器内没有「标记之后」的可见内容，就连题头
    // 一起藏掉；还能看到标记之后的内容，说明容器装着下半图，保留。
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
"""


def _split_html(html: str, mode: str) -> str:
    """往 HTML 里注入分段隐藏脚本。

    Args:
        html: 完整 HTML。
        mode: ``after``=隐藏标记之后的内容（上半图）；``before``=隐藏之前（下半图）。

    Returns:
        注入脚本后的 HTML。
    """
    body = _SPLIT_JS_BODY.replace("__MARKER_ID__", _SPLIT_MARKER_ID)
    script = f"<script>(function() {{ const mode = '{mode}'; {body} }})();</script>"
    lower = html.lower()
    idx = lower.rfind("</body>")
    if idx != -1:
        return html[:idx] + script + html[idx:]
    return html + script


async def render_theme_image_split(
    archive: Any,
    stats: Any,
    analysis: dict[str, Any],
    *,
    style: str = "scrapbook",
    width: int = 900,
) -> list[bytes] | None:
    """渲染海报并切成上下两段。

    切点固定在「高亮记忆碎片」结束之后、「神人名片颁发」之前。
    做法是在话题片段末尾注入一个零高标记，注入「隐藏标记前/后 DOM」的
    脚本后分别出图——不改上游模板，6 套主题通用。整页截图在页面过高时
    底部会出现空白，分段正是为了绕开浏览器的高度上限。

    宿主渲染器如果不执行页面脚本，两张图会完全一致——据此判定分段
    不可用，返回 ``None`` 让调用方退回整图。

    Args:
        archive: 消息采集结果。
        stats: 统计结果。
        analysis: 文字模型返回的结构化分析。
        style: 主题名，见 :data:`REPORT_STYLES`。
        width: 渲染宽度（像素）。

    Returns:
        ``[上半图, 下半图]``；话题为空、其后没有有效内容、或渲染
        失败时返回 ``None``，调用方应退回整图。
    """
    built = await _build_theme_context(style, archive, stats, analysis)
    if built is None:
        return None
    ctx, directory = built

    topics_html = str(ctx.get("topics_html") or "")
    if not topics_html.strip():
        logger.info("[群日报] 没有话题内容，无需分段")
        return None
    # 分隔点之后若无任何有效内容（名片/金句/锐评全空），下半图
    # 只剩页脚，发出去没有意义，直接退回整图。
    if not any(
        str(ctx.get(k) or "").strip()
        for k in ("titles_html", "quotes_html", "chat_quality_html")
    ):
        logger.info("[群日报] 分隔点之后没有内容，无需分段")
        return None
    ctx["topics_html"] = (
        topics_html
        + f'<div id="{_SPLIT_MARKER_ID}" style="height:0;overflow:hidden"></div>'
    )

    try:
        html = await _env(directory).get_template("image_template.html").render_async(
            **ctx
        )
    except Exception as e:
        logger.error(f"[群日报] {style} 主题模板渲染失败: {e!s}")
        return None

    html = _inline_assets(html, directory)
    if _HTML_RENDERER is None:
        logger.error("[群日报] 宿主渲染钩子未注册（html2png 不可用），无法生成海报")
        return None

    try:
        top = await _HTML_RENDERER(_split_html(html, "after"), width)
        bottom = await _HTML_RENDERER(_split_html(html, "before"), width)
    except Exception as e:
        logger.error(f"[群日报] {style} 主题分段渲染失败: {e!s}")
        return None
    if not top or not bottom:
        return None
    if top == bottom:
        # 脚本没被执行（宿主渲染器禁用 JS）：两图一致说明没分切开，
        # 退回整图，绝不能把两张一样的整图当上下半发出去。
        logger.info("[群日报] 分段脚本未生效（渲染器不执行脚本），退回整图")
        return None
    return [top, bottom]

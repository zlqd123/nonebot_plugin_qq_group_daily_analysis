"""海报主题渲染：直接复用 astrbot 原插件的模板与素材。

早先的 8 套主题是照着名字手写的 CSS 变量，和上游差得很远。这里改为把
``templates/themes/`` 下的上游模板原样跑起来，本模块只做一件事——把本插件的
数据结构翻译成上游模板认识的入参。

上游 8 套主题的入参高度统一（16 个共享变量 + 各主题自己的 mirror），
所以一个适配器就够了，差异全部体现在 :data:`_THEMES` 的配置里。

素材已全部落盘（ATRI 34MB 含 24MB 字体，Miku 6MB），不需要像上游那样
在渲染时去连 GitHub CDN。只有 Google 字体仍走公网——那是字体文件本身，
本地没有副本。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from nonebot import require
from nonebot.log import logger

if TYPE_CHECKING:
    from .collect import ChatArchive
    from .stats import ChatStats

require("nonebot_plugin_htmlrender")
from nonebot_plugin_htmlrender import template_to_pic  # noqa: E402
from nonebot_plugin_htmlrender.browser import get_new_page  # noqa: E402

THEME_DIR = Path(__file__).parent / "templates" / "themes"

#: 可用主题名（与 gdr_report_style 的取值一致）。
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
#: 模板目录自身，素材从本地磁盘读。之所以能这么填，是因为 template_to_pic 会
#: 先 ``page.goto("file://<模板目录>")`` 再 set_content，页面基准 URL 停在模板
#: 目录上，相对路径因此成立。
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
    archive: ChatArchive,
    stats: ChatStats,
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
        # 指向模板目录自身：素材从本地磁盘读，路径形如 ./file/xxx
        ctx[local] = "."
    return ctx, directory


async def render_theme_image(
    archive: ChatArchive,
    stats: ChatStats,
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
        return await template_to_pic(
            # 纯文件路径：jinja loader 直接用它，html_to_pic 内部会补 file://
            template_path=str(THEME_DIR / directory),
            template_name="image_template.html",
            templates=ctx,
            pages={"viewport": {"width": width, "height": 10}},
        )
    except Exception as e:
        logger.error(f"[群日报] {style} 主题渲染失败: {e!s}")
        return None


#: 分段渲染时插在「话题」片段末尾的标记。分隔点固定在这里——
#: 即「高亮记忆碎片」结束之后、「神人名片颁发」之前。
_SPLIT_MARKER_ID = "gdr-split-marker"

_SPLIT_SCRIPT = """
(mode) => {
  const m = document.getElementById('gdr-split-marker');
  if (!m) return 'no-marker';
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
  return 'ok';
}
"""


async def render_theme_image_split(
    archive: ChatArchive,
    stats: ChatStats,
    analysis: dict[str, Any],
    *,
    style: str = "scrapbook",
    width: int = 900,
) -> list[bytes] | None:
    """渲染海报并切成上下两段。

    切点固定在「高亮记忆碎片」结束之后、「神人名片颁发」之前。
    做法是在话题片段末尾注入一个零高标记，渲染后用 JS 分别隐藏
    标记前后的 DOM，再各截一张整页图——不改上游模板，6 套主题
    通用。整页截图在页面过高时底部会出现空白，分段正是为了
    绕开浏览器的高度上限。

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

    # 先渲染成 HTML 字符串（template_to_pic 内部也是这么做的），
    # 之后完全接管页面：先 goto 模板目录建立相对路径基准，
    # 再 set_content——与 html_to_pic 的流程一致。
    html = await _env(directory).get_template("image_template.html").render_async(
        **ctx
    )
    base_url = f"file://{THEME_DIR / directory}"

    try:
        async with get_new_page(
            2, viewport={"width": width, "height": 10}
        ) as page:
            page.on(
                "console", lambda msg: logger.debug(f"浏览器控制台: {msg.text}")
            )
            await page.goto(base_url)
            # 上半图：隐藏标记之后的内容
            await page.set_content(html, wait_until="networkidle")
            await page.evaluate(_SPLIT_SCRIPT, "after")
            top = await page.screenshot(
                full_page=True, type="png", timeout=30_000
            )
            # 下半图：重置页面后隐藏标记之前的内容
            await page.set_content(html, wait_until="networkidle")
            await page.evaluate(_SPLIT_SCRIPT, "before")
            bottom = await page.screenshot(
                full_page=True, type="png", timeout=30_000
            )
        return [top, bottom]
    except Exception as e:
        logger.error(f"[群日报] {style} 主题分段渲染失败: {e!s}")
        return None

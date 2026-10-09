"""日报输出：海报图片 + 纯文本摘要。

海报实际由 :mod:`themes` 渲染——那里复用 astrbot 原插件的全部 8 套模板与
素材，本模块只负责对外的入口函数和文本摘要的排版。
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

from nonebot.log import logger

from .stats import ChatStats
from .themes import (
    REPORT_STYLES,
    clean_quality,
    clean_quotes,
    clean_titles,
    clean_topics,
    render_theme_image,
    render_theme_image_split,
)

if TYPE_CHECKING:
    from .collect import ChatArchive

__all__ = [
    "REPORT_STYLES",
    "build_text_summary",
    "render_report_image",
    "render_report_image_split",
    "to_image_segment",
]


def _safe_style(style: str) -> str:
    """校验风格名，防止拼错目录或注入到模板入参里。

    Args:
        style: 配置里的风格名。

    Returns:
        合法的风格名；非法时回落到默认的 ``scrapbook``。
    """
    name = (style or "").strip().lower()
    if name in REPORT_STYLES:
        return name
    logger.warning(f"[群日报] 未知海报主题 {style!r}，回落到 scrapbook")
    return "scrapbook"


async def render_report_image(
    archive: ChatArchive,
    stats: ChatStats,
    analysis: dict[str, Any],
    *,
    width: int = 900,
    style: str = "scrapbook",
) -> bytes | None:
    """把日报渲染为 PNG 图片字节。

    Args:
        archive: 消息采集结果。
        stats: 统计结果。
        analysis: 文字模型返回的结构化分析。
        width: 渲染宽度（像素）。
        style: 海报风格，见 :data:`REPORT_STYLES`。

    Returns:
        PNG 图片字节；渲染失败时返回 ``None``。
    """
    return await render_theme_image(
        archive, stats, analysis, style=_safe_style(style), width=width
    )


async def render_report_image_split(
    archive: ChatArchive,
    stats: ChatStats,
    analysis: dict[str, Any],
    *,
    width: int = 900,
    style: str = "scrapbook",
) -> list[bytes] | None:
    """把日报渲染成上下两段海报。

    切点固定在「高亮记忆碎片」结束之后、「神人名片颁发」之前，
    详见 :func:`themes.render_theme_image_split`。

    Args:
        archive: 消息采集结果。
        stats: 统计结果。
        analysis: 文字模型返回的结构化分析。
        width: 渲染宽度（像素）。
        style: 海报风格，见 :data:`REPORT_STYLES`。

    Returns:
        ``[上半图, 下半图]``；不适合分段或渲染失败时返回 ``None``，
        调用方应退回整图。
    """
    return await render_theme_image_split(
        archive, stats, analysis, style=_safe_style(style), width=width
    )


def to_image_segment(data: bytes) -> dict[str, Any]:
    """把图片字节转成 OneBot 的 base64 图片消息段。

    Args:
        data: 图片字节。

    Returns:
        OneBot 消息段字典。
    """
    return {"type": "image", "data": {"file": f"base64://{base64.b64encode(data).decode()}"}}


def build_text_summary(
    analysis: dict[str, Any], stats: ChatStats, window_label: str = "今日"
) -> str:
    """生成纯文字版日报，供合并转发使用。

    Args:
        analysis: 结构化分析结果。
        stats: 统计结果。
        window_label: 统计范围标签。

    Returns:
        纯文本日报。
    """
    lines: list[str] = [
        f"【群聊日报 · {window_label}】",
        f"时间：{stats.time_range_text}",
        f"消息 {stats.total_messages} 条 · 活跃 {stats.participant_count} 人"
        f"（{stats.active_ratio * 100:.1f}%）· 最活跃 {stats.peak_hour:02d} 时",
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
    if stats.emoji.total:
        lines.append(f"表情使用 {stats.emoji.total} 个")

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

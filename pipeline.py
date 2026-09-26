"""日报生成主流程编排。

串联：采集 → 统计 → 文字分析 → 渲染图片 → （可选）生成漫画。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from nonebot.log import logger

from .collect import fetch_group_archive, resolve_window
from .imagegen import ImageModel
from .llm import build_analysis_prompt, build_comic_prompt_prompt
from .report import build_text_summary, render_report_image, to_image_segment
from .stats import analyze, compress_nicknames, restore_nicknames

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot

    from .collect import ChatArchive
    from .config import Config
    from .llm import TextModel
    from .stats import ChatStats


async def _run_comic(
    model: ImageModel,
    analysis: dict[str, Any],
    config: Config,
    text_model: TextModel,
) -> bytes | None:
    """生成漫画图片（单独包一层便于限流与异常隔离）。

    只依赖 ``analysis`` 里的 summary 和 topics，因此海报和漫画可以共用**同一次**
    文字模型分析——「群日报整体」不会为了多出这张图再调一次文字模型。

    Args:
        model: 生图模型客户端。
        analysis: 已完成的分析结果。
        config: 插件配置。
        text_model: 文字模型客户端，用来把话题翻成绘图提示词。

    Returns:
        图片字节；跳过或失败时返回 ``None``。
    """
    try:
        # 标题为空的话题画不出东西，先滤掉——省下的是一整次生图 API 调用
        topics = [
            t
            for t in (analysis.get("topics") or [])
            if isinstance(t, dict) and str(t.get("title") or "").strip()
        ]
        if not topics:
            logger.info("[群日报] 无有效话题，跳过漫画")
            return None
        prompt = await text_model.complete(
            build_comic_prompt_prompt(
                str(analysis.get("summary") or ""), topics, config
            ),
            "请直接输出英文绘图提示词。",
        )
        if not prompt.strip():
            return None
        logger.info(f"[群日报] 漫画提示词: {prompt[:200]}")
        return await model.generate(prompt)
    except Exception as e:
        logger.error(f"[群日报] 漫画生成失败: {e!s}")
        return None


async def generate_daily_report(
    bot: Bot,
    group_id: int,
    config: Config,
    *,
    text_model: TextModel,
    image_model: ImageModel,
    with_comic: bool = False,
    comic_only: bool = False,
    notify_missing_comic: bool = False,
) -> tuple[list[dict[str, Any]], ChatStats | None, ChatArchive | None]:
    """生成一份群日报的**消息段**（本函数不负责发送）。

    采集阶段会多取一些消息，真正决定日报范围的是随后的
    :meth:`ChatArchive.filter_window`——按 ``gdr_window`` 截取时间窗口。

    Args:
        bot: OneBot v11 机器人实例。
        group_id: 群号。
        config: 插件配置。
        text_model: 文字模型客户端。
        image_model: 生图模型客户端。
        with_comic: 是否在日报之外附带一张漫画。
        comic_only: 只出漫画，不出海报也不出正文。漫画仍然依赖文字模型
            先产出 summary/topics，所以前面的采集和分析一步都省不掉，
            省下的只是海报渲染那一次 HTML 截图。
        notify_missing_comic: 没配生图模型、漫画出不来时，是否在群里提示。
            定时推送传 ``False``：那个点没人在看，报缺 key 纯属噪音，
            日志里已经记了。只有用户主动敲漫画指令时才值得告诉他为什么没图。

    Returns:
        ``(消息段列表, 统计结果, 采集结果)``；无可用内容时消息段为空。
    """
    # 先定窗口，再按窗口起点回溯——顺序反了就只能「抓够 N 条」而不是「抓够时间」
    start_ts, end_ts, window_label = resolve_window(config.gdr_window)
    notices: list[str] = []

    archive = await fetch_group_archive(
        bot,
        group_id,
        history_lens=config.gdr_history_lens,
        source=config.gdr_source,
        page_size=config.gdr_fetch_page_size,
        max_pages=config.gdr_fetch_max_pages,
        interval=config.gdr_fetch_interval,
        member_cache_ttl=config.gdr_member_cache_ttl,
        self_id=bot.self_id,
        since_ts=start_ts,
    )
    if not archive.messages:
        logger.warning(f"[群日报] 群 {group_id} 未采集到消息")
        return [], None, archive

    archive.window_label = window_label

    # 采集已按窗口起点回溯，这里只做最后的边界收口
    dropped = archive.filter_window(start_ts, end_ts)
    if dropped:
        logger.info(
            f"[群日报] 群 {group_id} 时间窗口「{window_label}」内保留 "
            f"{len(archive.messages)} 条，过滤掉窗口外 {dropped} 条"
        )

    if archive.truncated:
        # 覆盖不全只在日志里提示，不再往群里发——用户明确要求日报只出海报
        covered = (archive.last_time - archive.first_time) / 3600 if archive.first_time else 0
        logger.warning(
            f"[群日报] 群 {group_id} 实际只回溯到 {archive.time_range_text}，"
            f"未覆盖完整{window_label}，仅 {covered:.1f} 小时；"
            f"如需更多请调大 gdr_fetch_page_size"
        )

    if len(archive.messages) < config.gdr_window_min_messages:
        logger.warning(
            f"[群日报] 群 {group_id} 窗口「{window_label}」内仅 "
            f"{len(archive.messages)} 条消息，低于下限 {config.gdr_window_min_messages}"
        )
        return [], None, archive

    stats = analyze(archive)
    chat_lines, code_to_name = compress_nicknames(
        archive.recent(config.gdr_max_messages)
    )

    system, user = build_analysis_prompt(
        stats, chat_lines, code_to_name, config, window_label
    )
    try:
        analysis = await text_model.complete_json(system, user)
    except Exception as e:
        logger.error(f"[群日报] 文字分析失败: {e!s}")
        return [], stats, archive

    # 还原昵称。注意 topics[].users 与 quotes[].user 也是代号，
    # 海报和文字版都会渲染它们，漏掉就会在日报里露出「000」这种代号。
    for key in ("summary", "mood"):
        if isinstance(analysis.get(key), str):
            analysis[key] = restore_nicknames(analysis[key], code_to_name)
    for topic in analysis.get("topics") or []:
        if isinstance(topic, dict):
            for f in ("title", "detail"):
                if isinstance(topic.get(f), str):
                    topic[f] = restore_nicknames(topic[f], code_to_name)
            if isinstance(topic.get("users"), list):
                topic["users"] = [
                    restore_nicknames(str(u), code_to_name) for u in topic["users"]
                ]
    for quote in analysis.get("quotes") or []:
        if isinstance(quote, dict):
            if isinstance(quote.get("text"), str):
                quote["text"] = restore_nicknames(quote["text"], code_to_name)
            if isinstance(quote.get("user"), str):
                quote["user"] = restore_nicknames(quote["user"], code_to_name)
            if isinstance(quote.get("reason"), str):
                quote["reason"] = restore_nicknames(quote["reason"], code_to_name)
    for title in analysis.get("titles") or []:
        if isinstance(title, dict):
            for f in ("user", "title", "reason"):
                if isinstance(title.get(f), str):
                    title[f] = restore_nicknames(title[f], code_to_name)
    if isinstance(analysis.get("quality"), dict):
        q = analysis["quality"]
        for f in ("title", "subtitle", "summary", "comment"):
            if isinstance(q.get(f), str):
                q[f] = restore_nicknames(q[f], code_to_name)
        dims = q.get("dimensions")
        if isinstance(dims, list):
            for d in dims:
                if isinstance(d, dict) and isinstance(d.get("comment"), str):
                    d["comment"] = restore_nicknames(d["comment"], code_to_name)

    analysis["titles"] = (analysis.get("titles") or [])[: config.gdr_max_titles]

    # 热词完全由模型产出；没给就留空，不用本地分词凑数
    analysis["keywords"] = [
        str(k).strip() for k in (analysis.get("keywords") or []) if str(k).strip()
    ][:12]

    segments: list[dict[str, Any]] = []

    # 「群日报漫画」只要漫画：跳过海报渲染，省掉一次 HTML 截图
    if config.gdr_report_mode in ("image", "both") and not comic_only:
        image = await render_report_image(
            archive,
            stats,
            analysis,
            width=config.gdr_report_width,
            style=config.gdr_report_style,
        )
        if image:
            segments.append(to_image_segment(image))
        else:
            # 静默降级会让人误以为「插件只输出文字」，所以明确告诉用户
            logger.warning("[群日报] 海报渲染失败，降级为纯文字")
            notices.append("海报渲染失败，本次只发文字版（详见日志）")

    # 只要漫画时，若漫画也没出来就没什么可发的了，不该再补一段正文
    if (with_comic or comic_only) and not image_model.available:
        if notify_missing_comic:
            notices.append("未配置 gdr_image_api_keys，漫画已跳过")
        else:
            logger.warning("[群日报] 未配置 gdr_image_api_keys，跳过漫画（不打扰群里）")

    # 纯文字段：显式要求时给，或海报没出来时兜底
    want_text = not comic_only and (
        config.gdr_report_mode == "text"
        or (config.gdr_report_mode == "both" and config.gdr_enable_text_summary)
    )
    if want_text or not segments:
        text = build_text_summary(analysis, stats, archive.window_label)
        if text.strip():
            segments.append({"type": "text", "data": {"text": text}})

    if (with_comic or comic_only) and image_model.available:
        async with _COMIC_SEM:
            comic = await _run_comic(image_model, analysis, config, text_model)
            if comic:
                segments.append(to_image_segment(comic))
            else:
                notices.append("漫画生成失败（详见日志）")

    if notices:
        # 打上 notice 标记：关掉文字模式时告警仍要发出去，
        # 否则海报一渲染失败群里就什么都收不到了
        segments.append(
            {"type": "text", "data": {"text": "⚠️ " + "；".join(notices)}, "notice": True}
        )

    return segments, stats, archive


# 限制漫画生成并发，避免同时打爆生图服务
_COMIC_SEM = asyncio.Semaphore(2)

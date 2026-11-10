# -*- coding: utf-8 -*-
"""日报主流程编排：按群所属适配器选择采集通道。

- OneBot 群：历史接口按时间回溯（可回补离线时段），接口拿不到时回落落库
- 官方群：实时落库查询（无历史接口），空昵称走成员接口补全

产物为通道无关的 :class:`~.send.Report`（notices / text / images），
发送格式由 :mod:`.send` 按通道处理。
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx
from nonebot.log import logger

from . import store
from .archive import ChatArchive, ChatMessage, resolve_window
from .comicgen import ComicImageModel
from .config import Config
from .llm import (
    TextModel,
    build_analysis_prompt,
    build_scene_rewrite_prompt,
    build_storyboard_prompt,
    extract_scene,
    strip_reasoning,
)
from .render import build_text_summary, render_report_image, render_report_image_split
from .send import Report
from .stats import analyze, compress_nicknames, restore_nicknames

API = "https://api.sgroup.qq.com"

_META_CACHE_TTL = 21600
"""群名/成员数缓存时长（秒）。"""

_NICKNAME_LOOKUP_CAP = 30
"""单次报告最多补查多少个成员昵称（成员信息接口有频控）。"""


# ── 群元信息（官方通道）────────────────────────────────────────


async def _fetch_group_meta_official(bot: Any, group_openid: str) -> tuple[int, str]:
    """官方通道：群成员数与群名，落库缓存优先，过期才打 API。"""
    cached = await store.load_group_meta(group_openid, _META_CACHE_TTL)
    if cached:
        return int(cached["member_count"]), str(cached["group_name"])

    member_count, group_name = 0, ""
    try:
        token = await bot.get_access_token()
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(
                f"{API}/v2/groups/{group_openid}/info",
                headers={"Authorization": f"QQBot {token}"},
            )
            if r.status_code < 400:
                info = r.json()
                group_name = str(info.get("group_name") or "").strip()
                try:
                    member_count = int(info.get("group_member_num") or 0)
                except (TypeError, ValueError):
                    member_count = 0
            else:
                logger.warning(f"[群日报] 群信息接口 {r.status_code}: {r.text[:120]}")
    except Exception as e:
        logger.warning(f"[群日报] 获取群信息失败: {e!s}")

    await store.save_group_meta(group_openid, group_name, member_count)
    return member_count, group_name


async def _enrich_nicknames(
    bot: Any, group_openid: str, messages: list[ChatMessage]
) -> None:
    """官方通道补全空昵称：成员缓存回填 → 成员接口补查 → openid 尾号占位。"""
    missing = {m.member_openid for m in messages if not m.nickname}
    if not missing:
        return

    updated = await store.backfill_nicknames(group_openid)
    if updated:
        logger.info(f"[群日报] 用成员缓存回填了 {updated} 条消息的空昵称")
        cached = await store.load_members(group_openid)
        for m in messages:
            if not m.nickname and m.member_openid in cached:
                m.nickname = cached[m.member_openid]
        missing = {m.member_openid for m in messages if not m.nickname}

    for openid in list(missing)[:_NICKNAME_LOOKUP_CAP]:
        try:
            info = await bot.get_group_member(
                group_openid=group_openid, member_openid=openid
            )
            username = str(getattr(info, "username", "") or "").strip()
            if username:
                await store.save_member(group_openid, openid, username)
                for m in messages:
                    if m.member_openid == openid and not m.nickname:
                        m.nickname = username
        except Exception as e:
            logger.debug(f"[群日报] 补查成员昵称失败 {openid[:16]}…: {e!s}")
            continue

    for m in messages:
        if not m.nickname:
            m.nickname = f"用户{m.member_openid[-6:]}"


# ── 采集 ───────────────────────────────────────────────────────


async def _collect(
    bot: Any, channel: str, group: str | int, start_ts: int, config: Config
) -> ChatArchive:
    """按通道采集：OneBot 历史回溯优先，官方查落库。"""
    if channel == "onebot":
        archive = None
        try:
            from .collect import fetch_group_archive

            archive = await fetch_group_archive(
                bot, int(group), since_ts=start_ts,
                history_lens=20000,
                page_size=5000,
            )
        except Exception as e:
            logger.warning(f"[群日报] OneBot 历史接口采集失败，回落落库: {e!s}")
        if archive and archive.messages:
            return archive
        # 接口失败/为空：实时落库兜底（ingest_ob11 写入的数据）
        messages = await store.fetch_messages(
            str(group), start_ts, int(time.time()), limit=50000, source="onebot"
        )
        fb = ChatArchive(
            group_openid=str(group),
            messages=messages,
            fetched_at=int(time.time()),
        )
        if archive is not None:
            fb.group_name, fb.member_count = archive.group_name, archive.member_count
        return fb

    # 官方通道
    archive = ChatArchive(
        group_openid=str(group), fetched_at=int(time.time())
    )
    archive.member_count, archive.group_name = await _fetch_group_meta_official(
        bot, str(group)
    )
    archive.messages = await store.fetch_messages(
        str(group), start_ts, int(time.time()), limit=50000, source="qq"
    )
    await _enrich_nicknames(bot, str(group), archive.messages)
    return archive


# ── 主流程 ─────────────────────────────────────────────────────


def _restore_analysis(analysis: dict[str, Any], code_to_name: dict[str, str]) -> None:
    """把分析结果各字段里的代号还原成昵称（原位修改）。"""
    if not code_to_name:
        return

    def _fix(value: Any) -> Any:
        return restore_nicknames(str(value), code_to_name) if value else value

    analysis["summary"] = _fix(analysis.get("summary"))
    analysis["mood"] = _fix(analysis.get("mood"))

    topics = analysis.get("topics")
    if isinstance(topics, list):
        for t in topics:
            if isinstance(t, dict):
                t["title"] = _fix(t.get("title"))
                t["detail"] = _fix(t.get("detail"))
                if isinstance(t.get("users"), list):
                    t["users"] = [_fix(u) for u in t["users"]]

    quotes = analysis.get("quotes")
    if isinstance(quotes, list):
        for q in quotes:
            if isinstance(q, dict):
                q["user"] = _fix(q.get("user"))

    titles = analysis.get("titles")
    if isinstance(titles, list):
        for t in titles:
            if isinstance(t, dict):
                t["user"] = _fix(t.get("user"))

    quality = analysis.get("quality")
    if isinstance(quality, dict):
        quality["summary"] = _fix(quality.get("summary"))
        dims = quality.get("dimensions")
        if isinstance(dims, list):
            for d in dims:
                if isinstance(d, dict):
                    d["comment"] = _fix(d.get("comment"))


# ── 漫画（生图）─────────────────────────────────────────────────

#: 分镜提示词解析失败后的重试次数（独立于生图 API 自身的重试）
_STORYBOARD_RETRIES = 1

_FENCE_RE = re.compile(r"^```[A-Za-z]*\s*\n(.*?)\n?```\s*$", re.DOTALL)

# 限制漫画生成并发，避免同时打爆生图服务
_COMIC_SEM = asyncio.Semaphore(2)


def _strip_code_fence(text: str) -> str:
    """剥掉模型输出外层的 ``` 代码块包裹（改写结果没按 JSON 输出时用）。"""
    match = _FENCE_RE.match(text.strip())
    return match.group(1).strip() if match else text.strip()


async def _force_rewrite(
    scene: str, text_model: TextModel, error_info: str | None = None
) -> str | None:
    """生图服务报拒绝（内容审查等原因）后的**强制**重新生成。

    与分镜解析重试的区别：不依赖本地命中——把**被拒绝的原提示词和服务的
    拒绝原因原文**打包给文字模型，让它据此重新生成（参考上游 astrbot 的
    analyze_retry_prompt）。

    Args:
        scene: 被服务端拒绝的原提示词。
        text_model: 文字模型客户端。
        error_info: 生图服务返回的错误原文（供模型定位问题）。

    Returns:
        重新生成的提示词；失败时返回 ``None``。
    """
    system, user = build_scene_rewrite_prompt(scene, error_info=error_info)
    try:
        raw = await text_model.complete(system, user)
    except Exception as e:
        logger.warning(f"[群日报] 被拒后请求文字模型重新生成提示词失败: {e!s}")
        return None
    rewritten = extract_scene(raw)
    if rewritten is None or not rewritten.strip():
        rewritten = _strip_code_fence(strip_reasoning(raw))
    if not rewritten or rewritten == scene:
        logger.warning("[群日报] 提示词重新生成未产出可用内容")
        return None
    return rewritten


async def _run_comic(
    model: ComicImageModel,
    analysis: dict[str, Any],
    config: Config,
    text_model: TextModel,
    notices: list[str],
) -> bytes | None:
    """生成漫画图片（单独包一层便于限流与异常隔离）。

    链路按 bison5 移植：**每个话题强制一格**的分镜提示词（气泡中文台词
    + 底部字幕条 + 脱敏后的 Background Context）→ 结构化解析出 ``scene``
    （解析失败自动重试，绝不让模型的自由文本直接进生图 API）→
    生图客户端出图；被服务端风控拦截（``400
    InputTextSensitiveContentDetected``）时再做一次事后改写 + 末次出图，
    仍不行则向群里明示「内容涉敏」而不是笼统的「生成失败」。

    只依赖 ``analysis`` 里的 topics，因此海报和漫画可以共用**同一次**文字模型
    分析——「群日报整体」不会为了多出这张图把整段聊天记录再喂一遍。
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

        async def storyboard(include_context: bool) -> str | None:
            """跑一轮分镜生成（含解析重试），产出 scene。"""
            system, user = build_storyboard_prompt(
                topics, config, include_context=include_context
            )
            for attempt in range(_STORYBOARD_RETRIES + 1):
                raw = await text_model.complete(system, user)
                scene = extract_scene(raw)
                if scene and scene.strip():
                    return scene
                logger.warning(
                    f"[群日报] 漫画分镜解析失败（第 {attempt + 1} 次），"
                    f"原始输出前 200 字: {raw[:200]}"
                )
                if attempt < _STORYBOARD_RETRIES:
                    user = (
                        f"{user}\n\n【重要】上一次输出无法解析出 scene 字段。"
                        "请严格按 JSON 格式重新输出，scene 里必须内联全部 "
                        f"{len(topics)} 个 Panel 的完整描述。"
                    )
            return None

        scene = await storyboard(include_context=True)
        if not scene:
            logger.error("[群日报] 漫画分镜多次解析失败，放弃生图")
            return None

        logger.info(f"[群日报] 漫画分镜 scene（前 300 字）: {scene[:300]}")
        comic = await model.generate(scene)
        if comic:
            return comic
        if model.last_error_kind != "sensitive":
            notices.append("漫画生成失败（详见日志）")
            return None

        # 事后重新生成（上游 astrbot 同款兜底）：生图 API 报内容审查拒绝时，
        # 把**被拒绝的原提示词 + 拒绝原因原文**打包给文字模型重新生成，
        # 再试最后一次。
        logger.warning("[群日报] 生图服务拒绝内容，请求文字模型重新生成提示词")
        sanitized = await _force_rewrite(
            scene, text_model, error_info=model.last_error_text
        )
        comic = await model.generate(sanitized) if sanitized else None
        if comic:
            return comic
        notices.append(
            "本次漫画内容涉敏，已被生图服务拦截，已跳过"
            if model.last_error_kind == "sensitive"
            else "漫画生成失败（详见日志）"
        )
        return None
    except Exception as e:
        logger.error(f"[群日报] 漫画生成失败: {e!s}")
        return None


# ── 主流程 ──────────────────────────────────────────────────────

async def generate_daily_report(
    bot: Any,
    channel: str,
    group: str | int,
    config: Config,
    text_model: TextModel,
    *,
    image_model: ComicImageModel | None = None,
    with_comic: bool = False,
    comic_only: bool = False,
    notify: bool = False,
) -> Report:
    """生成一份群日报（通道无关产物）。

    Args:
        bot: 对应通道的 Bot 实例。
        channel: ``"onebot"`` / ``"qq"``。
        group: OneBot 群号（int）或官方群 openid（str）。
        config: 插件配置。
        text_model: 文字总结模型客户端。
        image_model: 漫画生图客户端；None 或未配 Key 时跳过漫画。
        with_comic: 是否顺带生成漫画（海报 + 漫画都出）。
        comic_only: 只出漫画，不出海报与文字（「群日报漫画」指令）。
        notify: 数据不足等情况下是否生成提示消息（手动指令为 True，
            定时推送为 False）。
    """
    start_ts, end_ts, window_label = resolve_window(config.gdrq_window)
    archive = await _collect(bot, channel, group, start_ts, config)
    archive.window_label = window_label
    archive.filter_window(start_ts, end_ts)

    notices: list[str] = []
    if archive.messages:
        first = archive.first_time or 0
        if first > start_ts + 300:
            archive.truncated = True
            notices.append(
                "⚠️ 机器人离线时段的消息无法回补，本报告仅覆盖 "
                f"{archive.time_range_text.split(' ~ ')[0]} 之后的消息。"
            )

    min_count = config.gdrq_window_min_messages
    if len(archive.messages) < min_count:
        logger.info(
            f"[群日报] {channel} 群 {group} 窗口内仅 {len(archive.messages)} 条"
            f"（< {min_count}），跳过日报"
        )
        if notify:
            notices.append(
                f"本群 {window_label}消息不足（{len(archive.messages)} 条，"
                f"至少需要 {min_count} 条），暂不生成日报。"
            )
        return Report(notices=notices, text=None, images=[])

    stats = analyze(archive)
    chat_lines, code_to_name = compress_nicknames(
        archive.recent(config.gdrq_max_messages)
    )
    system, user = build_analysis_prompt(
        stats, chat_lines, code_to_name, config, window_label
    )
    analysis = await text_model.complete_json(system, user)
    _restore_analysis(analysis, code_to_name)

    images: list[bytes] = []
    if not comic_only:
        if config.gdrq_split_report:
            split = await render_report_image_split(
                archive, stats, analysis,
                width=config.gdrq_report_width, style=config.gdrq_report_style,
            )
            if split:
                images = split
        if not images:
            single = await render_report_image(
                archive, stats, analysis,
                width=config.gdrq_report_width, style=config.gdrq_report_style,
            )
            if single:
                images = [single]

        text = build_text_summary(analysis, stats, window_label)
        if not images:
            logger.warning("[群日报] 海报不可用，降级为纯文字版日报")
    else:
        text = None

    # 漫画：海报和漫画共用同一次文字模型分析（analysis）
    if with_comic or comic_only:
        if image_model is None or not image_model.available:
            if notify:
                notices.append("未配置 GDRQ_IMAGE_API_KEYS，漫画已跳过")
            else:
                logger.warning("[群日报] 未配置 GDRQ_IMAGE_API_KEYS，跳过漫画（不打扰群里）")
        else:
            async with _COMIC_SEM:
                comic = await _run_comic(image_model, analysis, config, text_model, notices)
            if comic:
                images.append(comic)
                logger.info(f"[群日报] 漫画已生成（{len(comic) // 1024}KB）")

    if comic_only and not images and not notices and notify:
        notices.append("未能生成漫画，可能是群消息过少或模型调用失败。")

    return Report(
        notices=notices,
        text=text,
        images=images,
        send_text=config.gdrq_send_text,
    )

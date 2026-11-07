# -*- coding: utf-8 -*-
"""日报主流程编排：按群所属适配器选择采集通道。

- OneBot 群：历史接口按时间回溯（可回补离线时段），接口拿不到时回落落库
- 官方群：实时落库查询（无历史接口），空昵称走成员接口补全

产物为通道无关的 :class:`~.send.Report`（notices / text / images），
发送格式由 :mod:`.send` 按通道处理。
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from nonebot.log import logger

from . import store
from .archive import ChatArchive, ChatMessage, resolve_window
from .config import Config
from .llm import TextModel, build_analysis_prompt
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


async def generate_daily_report(
    bot: Any,
    channel: str,
    group: str | int,
    config: Config,
    text_model: TextModel,
    *,
    notify: bool = False,
) -> Report:
    """生成一份群日报（通道无关产物）。

    Args:
        bot: 对应通道的 Bot 实例。
        channel: ``"onebot"`` / ``"qq"``。
        group: OneBot 群号（int）或官方群 openid（str）。
        config: 插件配置。
        text_model: 文字总结模型客户端。
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
    return Report(
        notices=notices,
        text=text,
        images=images,
        send_text=config.gdrq_send_text,
    )

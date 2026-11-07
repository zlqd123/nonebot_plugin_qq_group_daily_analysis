# -*- coding: utf-8 -*-
"""日报主流程编排：查库 → 统计 → LLM 分析 → 渲染海报 → 分片上传。

与参考插件（bison5 OneBot 版）的差异：

- 采集来自实时落库（官方机器人没有历史消息接口）；
- 海报以 PNG 字节产出后走 ``sd_media`` 的分片上传通道换 raw_url，
  再拼成 markdown 卡片；渲染/上传失败降级纯文字版；
- 不再有漫画与敏感词链路。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from nonebot.log import logger

from . import store
from .archive import ChatArchive, ChatMessage, resolve_window
from .config import Config
from .llm import TextModel, build_analysis_prompt
from .render import build_text_summary, render_report_image, render_report_image_split
from .stats import analyze, compress_nicknames, restore_nicknames

API = "https://api.sgroup.qq.com"

_META_CACHE_TTL = 21600
"""群名/成员数缓存时长（秒）。"""

_NICKNAME_LOOKUP_CAP = 30
"""单次报告最多补查多少个成员昵称（成员信息接口有频控）。"""


@dataclass(slots=True)
class Payload:
    """一份待发送的内容段。"""

    kind: str
    """``markdown`` / ``text``。"""
    content: str
    notice: bool = False
    """告警信息：不受 ``gdrq_send_text`` 开关影响，始终发送。"""


@dataclass(slots=True)
class ReportResult:
    """一次日报生成的结果。"""

    payloads: list[Payload] = field(default_factory=list)
    stats: Any | None = None
    archive: ChatArchive | None = None


async def _fetch_group_meta(bot: Any, group_openid: str) -> tuple[int, str]:
    """取群成员数与群名：落库缓存优先，过期才打 API。"""
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
                logger.warning(
                    f"[群日报] 群信息接口 {r.status_code}: {r.text[:120]}"
                )
    except Exception as e:
        logger.warning(f"[群日报] 获取群信息失败: {e!s}")

    await store.save_group_meta(group_openid, group_name, member_count)
    return member_count, group_name


async def _enrich_nicknames(
    bot: Any, group_openid: str, messages: list[ChatMessage]
) -> None:
    """补全空昵称：成员缓存回填 → 成员信息接口逐个补查 → openid 尾号占位。"""
    missing = {m.member_openid for m in messages if not m.nickname}
    if not missing:
        return

    # 1) 成员缓存回填（顺带把历史落库行的空昵称补掉）
    updated = await store.backfill_nicknames(group_openid)
    if updated:
        logger.info(f"[群日报] 用成员缓存回填了 {updated} 条消息的空昵称")
        cached = await store.load_members(group_openid)
        for m in messages:
            if not m.nickname and m.member_openid in cached:
                m.nickname = cached[m.member_openid]
        missing = {m.member_openid for m in messages if not m.nickname}

    # 2) 成员信息接口补查（有频控，逐个 try，限量）
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

    # 3) 仍拿不到的用 openid 尾号占位，避免日报里出现空名
    for m in messages:
        if not m.nickname:
            m.nickname = f"用户{m.member_openid[-6:]}"


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


async def _render_and_upload(
    bot: Any,
    group_openid: str,
    archive: ChatArchive,
    stats: Any,
    analysis: dict[str, Any],
    config: Config,
) -> list[Payload]:
    """渲染海报并分片上传，返回 markdown 载荷；失败返回空列表。"""
    try:
        from extra_plugins.sd_media import upload_bytes
    except Exception as e:
        logger.warning(f"[群日报] sd_media 不可用，跳过分片上传: {e!s}")
        return []

    token = await bot.get_access_token()

    async def _to_md(data: bytes, name: str) -> str | None:
        url = await upload_bytes(token, data, name)
        if not url:
            return None
        return f"![t {config.gdrq_img_size}]({url})"

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

    payloads: list[Payload] = []
    for i, data in enumerate(images, 1):
        md = await _to_md(data, f"daily_report_{i}.png")
        if not md:
            logger.warning(f"[群日报] 第 {i} 张海报上传失败")
            continue
        if not payloads:
            # 首图（长日报海报）在上，标题即卡片名；后续图（漫画等）依次排下
            md = f"# 群日报\n\n{md}"
        payloads.append(Payload(kind="markdown", content=md))
    return payloads


async def generate_daily_report(
    bot: Any,
    group_openid: str,
    config: Config,
    text_model: TextModel,
    *,
    notify: bool = False,
) -> ReportResult:
    """生成一份群日报。

    Args:
        bot: QQ 官方适配器的 Bot 实例。
        group_openid: 群 openid。
        config: 插件配置。
        text_model: 文字总结模型客户端。
        notify: 数据不足等情况下是否生成提示消息（手动指令为 True，
            定时推送为 False——那个时间点没人在看）。

    Returns:
        :class:`ReportResult`。
    """
    start_ts, end_ts, window_label = resolve_window(config.gdrq_window)
    archive = ChatArchive(
        group_openid=group_openid,
        fetched_at=int(time.time()),
        window_label=window_label,
    )

    archive.member_count, archive.group_name = await _fetch_group_meta(bot, group_openid)
    archive.messages = await store.fetch_messages(
        group_openid, start_ts, end_ts, limit=50000
    )
    await _enrich_nicknames(bot, group_openid, archive.messages)
    dropped = archive.filter_window(start_ts, end_ts)
    if dropped:
        logger.debug(f"[群日报] 群 {group_openid[:12]}… 窗口外剔除 {dropped} 条")

    result = ReportResult(archive=archive)

    # 覆盖检查：最早一条消息没到窗口起点，说明机器人有离线时段
    if archive.messages:
        first = archive.first_time or 0
        if first > start_ts + 300:
            archive.truncated = True
            result.payloads.append(
                Payload(
                    kind="text",
                    content=(
                        f"⚠️ 机器人离线时段的消息无法回补，本报告仅覆盖 "
                        f"{archive.time_range_text.split(' ~ ')[0]} 之后的消息。"
                    ),
                    notice=True,
                )
            )

    min_count = config.gdrq_window_min_messages
    if len(archive.messages) < min_count:
        logger.info(
            f"[群日报] 群 {group_openid[:12]}… 窗口内仅 {len(archive.messages)} 条"
            f"（< {min_count}），跳过日报"
        )
        if notify:
            result.payloads.append(
                Payload(
                    kind="text",
                    content=(
                        f"本群 {window_label}消息不足（{len(archive.messages)} 条，"
                        f"至少需要 {min_count} 条），暂不生成日报。"
                    ),
                    notice=True,
                )
            )
        return result

    stats = analyze(archive)
    result.stats = stats

    chat_lines, code_to_name = compress_nicknames(
        archive.recent(config.gdrq_max_messages)
    )
    system, user = build_analysis_prompt(
        stats, chat_lines, code_to_name, config, window_label
    )
    analysis = await text_model.complete_json(system, user)
    _restore_analysis(analysis, code_to_name)

    payloads = await _render_and_upload(
        bot, group_openid, archive, stats, analysis, config
    )
    if payloads:
        result.payloads.extend(payloads)
        if config.gdrq_send_text:
            result.payloads.append(
                Payload(
                    kind="text",
                    content=build_text_summary(analysis, stats, window_label),
                )
            )
        return result

    # 海报渲染/上传失败：纯文字版兜底
    logger.warning("[群日报] 海报不可用，降级为纯文字版日报")
    result.payloads.append(
        Payload(kind="text", content=build_text_summary(analysis, stats, window_label))
    )
    return result

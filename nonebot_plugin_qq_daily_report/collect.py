# -*- coding: utf-8 -*-
"""OneBot v11 群消息采集：历史接口回溯（可回补离线时段）。

移植自 bison5 的 ``collect.py``，砍掉 chatrecorder 兜底（本插件的落库
就是兜底）。QQ 官方机器人没有历史接口，这条路径只在检测到 OneBot Bot
时可用——采集层"能用哪版用哪版"的其中一版。
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from nonebot.log import logger

from .archive import ChatArchive, ChatMessage

# QQ 表情的 CQ 码类型：普通 / 商城 / 超级 / 小表情
EMOJI_CQ_TYPES = ("face", "mface", "bface", "sface")
_CQ_RE = re.compile(r"\[CQ:([^,\]]+)(?:,[^\]]*)?\]")

FORWARD_SEG_TYPES = frozenset({"forward", "json", "file"})
"""合并转发 / structmsg / 文件分享：整段丢弃（data.content 可能是
字符串化的节点列表，进正文会变成一坨 JSON）。"""

_FORWARD_TEXT_RE = re.compile(
    r'"(?:self_id|user_id|message_seq)"\s*:|"structmsg"|"com\.tencent\.structmsg"'
)


def _looks_like_forward(text: str) -> bool:
    """正文里混进了转发内容（部分客户端把节点列表塞进 text 段）。"""
    return bool(_FORWARD_TEXT_RE.search(text))


def _sender_name(sender: dict) -> str:
    """取展示名，优先群名片。"""
    return (
        sender.get("card")
        or sender.get("nickname")
        or sender.get("role")
        or str(sender.get("user_id", ""))
    )


def _extract_text(message: dict) -> tuple[str, int, int, dict[str, int], int]:
    """提取正文文本 + 图片数/回复数/表情分布/合并消息数。只保留纯文字。"""
    raw = message.get("message") or message.get("raw_message")
    texts: list[str] = []
    images = replies = forwards = 0
    emojis: dict[str, int] = {}

    if isinstance(raw, str):
        def _count(m: re.Match[str]) -> str:
            t = m.group(1).lower()
            if t == "image":
                nonlocal images
                images += 1
            elif t == "reply":
                nonlocal replies
                replies += 1
            elif t in FORWARD_SEG_TYPES:
                nonlocal forwards
                forwards += 1
            elif t in EMOJI_CQ_TYPES:
                emojis[t] = emojis.get(t, 0) + 1
            return " "

        stripped = _CQ_RE.sub(_count, raw).strip()
        if stripped and not _looks_like_forward(stripped):
            texts.append(stripped)
    elif isinstance(raw, list):
        for seg in raw:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type")
            data = seg.get("data") or {}
            if stype == "text":
                content = (data.get("text") or "").strip()
                if content and not _looks_like_forward(content):
                    texts.append(content)
            elif stype == "image":
                images += 1
            elif stype == "reply":
                replies += 1
            elif stype in FORWARD_SEG_TYPES:
                forwards += 1
            elif stype in EMOJI_CQ_TYPES:
                emojis[stype] = emojis.get(stype, 0) + 1

    return (
        re.sub(r"[ \t]+", " ", " ".join(texts)).strip(),
        images,
        replies,
        emojis,
        forwards,
    )


class _Dropped:
    """被「只分析文字」规则剔除的消息标记。"""

    reason: str

    def __init__(self, reason: str) -> None:
        self.reason = reason


def _parse_message(msg: dict, self_id: str | None) -> ChatMessage | _Dropped | None:
    """把单条 OneBot 消息转成 ChatMessage；纯图/纯转发/自身消息剔除。"""
    try:
        user_id = int((msg.get("sender") or {}).get("user_id") or 0)
    except (TypeError, ValueError):
        return None
    if not user_id or (self_id is not None and str(user_id) == str(self_id)):
        return None

    text, images, replies, emojis, forwards = _extract_text(msg)
    if not text:
        if forwards and not images:
            return _Dropped("forward")
        if images:
            return _Dropped("image")
        return None

    return ChatMessage(
        member_openid=str(user_id),
        nickname=_sender_name(msg.get("sender") or {}),
        text=text,
        timestamp=int(msg.get("time") or 0),
        emoji_count=sum(emojis.values()),
    )


async def fetch_group_archive(
    bot: Any,
    group_id: int,
    *,
    since_ts: int = 0,
    history_lens: int = 20000,
    page_size: int = 5000,
    max_pages: int = 1,
    interval: float = 5.0,
) -> ChatArchive:
    """通过 OneBot ``get_group_msg_history`` 按时间回溯采集。

    以「回溯到 ``since_ts``」为终止条件，保证覆盖完整时间窗口；
    翻页数与总条数只是安全上限。采集不落库（历史接口本身就是来源）。

    Args:
        bot: OneBot v11 Bot 实例。
        group_id: 群号。
        since_ts: 需要回溯到的时间戳，0 表示不限。
        history_lens: 最多取多少条（安全上限）。
        page_size: 单次请求条数。
        max_pages: 最大翻页数（NapCat 的 message_seq 游标往回翻不可靠，
            单次拉大 + 1 页是实测最优解）。
        interval: 翻页间隔（秒），连续快速调用会触发限流。
    """
    archive = ChatArchive(
        group_openid=str(group_id),
        fetched_at=int(time.time()),
    )
    archive.member_count, archive.group_name = await _get_group_meta(bot, group_id)

    collected: dict[int, ChatMessage] = {}
    dropped = 0
    message_seq: int | None = None
    last_newest_time = 0
    pages = 0
    truncated = False
    self_id = getattr(bot, "self_id", None)

    while pages < max_pages and len(collected) + dropped < history_lens:
        if pages > 0 and interval > 0:
            await asyncio.sleep(interval)
        pages += 1
        try:
            params: dict[str, Any] = {"group_id": group_id, "count": page_size}
            if message_seq is not None:
                params["message_seq"] = message_seq
            resp = await bot.get_group_msg_history(**params)
        except Exception as e:
            logger.warning(
                f"[群日报] 拉取群 {group_id} 历史消息失败（第 {pages} 页）: {e!s}"
            )
            break

        raw_messages = (
            resp.get("messages") if isinstance(resp, dict) else None
        ) or (resp.get("data", {}).get("messages") if isinstance(resp, dict) else None) or []
        if not raw_messages:
            break

        for msg in raw_messages:
            try:
                msg_id = int(msg.get("message_id") or 0)
            except (TypeError, ValueError):
                continue
            if msg_id in collected:
                continue
            parsed = _parse_message(msg, str(self_id) if self_id else None)
            if isinstance(parsed, ChatMessage):
                collected[msg_id] = parsed
            elif isinstance(parsed, _Dropped):
                dropped += 1

        # NapCat 的 message_id 是随机值不随时间递增，游标必须取时间上
        # 最老的一条，否则翻页原地打转（实测只能拿到 76~100 条）。
        oldest = min(raw_messages, key=lambda m: int(m.get("time") or 0))
        oldest_time = int(oldest.get("time") or 0)
        newest_time = max(int(m.get("time") or 0) for m in raw_messages)

        if pages > 1 and newest_time >= last_newest_time:
            logger.debug(f"[群日报] 群 {group_id} 第 {pages} 页未取到更早数据，停止翻页")
            break
        last_newest_time = newest_time
        message_seq = int(oldest.get("message_id") or 0)

        if since_ts and oldest_time <= since_ts:
            logger.info(
                f"[群日报] 群 {group_id} 已回溯到 {oldest_time}，覆盖目标窗口"
            )
            break

    archive.messages = sorted(collected.values(), key=lambda m: m.timestamp)
    oldest_collected = min((m.timestamp for m in archive.messages), default=0)
    if since_ts and oldest_collected and oldest_collected > since_ts:
        truncated = True
        logger.warning(
            f"[群日报] 群 {group_id} 历史只回溯到 "
            f"{oldest_collected}（{len(archive.messages)} 条），未达到窗口起点"
        )
    logger.info(
        f"[群日报] OneBot 采集群 {group_id}: {len(archive.messages)} 条，"
        f"成员 {archive.member_count}，剔除纯图/转发 {dropped}"
    )
    archive.truncated = truncated
    return archive


async def _get_group_meta(bot: Any, group_id: int) -> tuple[int, str]:
    """取群成员数与群名，失败返回 0 / 空串。"""
    member_count, group_name = 0, ""
    try:
        members = await bot.get_group_member_list(group_id=group_id)
        member_count = len(members)
    except Exception as e:
        logger.warning(f"[群日报] 获取群 {group_id} 成员列表失败: {e!s}")
    try:
        info = await bot.get_group_info(group_id=group_id)
        name = info.get("group_name")
        if isinstance(name, str):
            group_name = name.strip()
    except Exception as e:
        logger.warning(f"[群日报] 获取群 {group_id} 信息失败: {e!s}")
    return member_count, group_name

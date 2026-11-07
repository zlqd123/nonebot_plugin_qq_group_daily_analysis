# -*- coding: utf-8 -*-
"""OneBot v11 群消息实时落库（兜底层）。

OneBot 群的日报主来源是历史接口回溯（:mod:`.collect`），这里把实时收到的
消息也写进 SQLite：接口抖动时兜底，且顺带获得离线容忍。表情计数按 CQ
类型统计后并入总量。
"""

from __future__ import annotations

import time

from nonebot import get_plugin_config, on_message
from nonebot.adapters import Event
from nonebot.log import logger

from .config import Config
from . import store

plugin_config = get_plugin_config(Config)

try:  # OneBot 适配器是可选依赖（官方机器人环境可能没装）
    from nonebot.adapters.onebot.v11 import GroupMessageEvent as Ob11GroupEvent
except ImportError:  # pragma: no cover
    Ob11GroupEvent = None  # type: ignore[assignment,misc]

_CQ_TYPES = ("face", "mface", "bface", "sface")


def _count_emojis(event: Event) -> int:
    """统计 OneBot 消息里的表情段数量（段对象或 CQ 码两种形态）。"""
    message = event.get_message()
    n = 0
    for seg in message:
        if getattr(seg, "type", "") in _CQ_TYPES:
            n += 1
    return n


async def _record(event: Event) -> None:
    text = event.get_message().extract_plain_text().strip()
    if not text:
        return
    sender = getattr(event, "sender", None)
    nickname = (
        getattr(sender, "card", "")
        or getattr(sender, "nickname", "")
        or ""
    )
    try:
        await store.insert_message(
            group_openid=str(event.group_id),
            member_openid=str(event.user_id),
            nickname=str(nickname).strip(),
            text=text,
            emoji_count=_count_emojis(event),
            ts=int(getattr(event, "time", 0) or time.time()),
            source="onebot",
        )
    except Exception:
        logger.exception("[群日报] OneBot 消息落库失败")


async def _is_ob11_group(event: Event) -> bool:
    return Ob11GroupEvent is not None and isinstance(event, Ob11GroupEvent)


_ob11_ingest = on_message(rule=_is_ob11_group, priority=1, block=False)


@_ob11_ingest.handle()
async def _(event: Event):
    await _record(event)

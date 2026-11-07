# -*- coding: utf-8 -*-
"""全量群消息实时监听入库。

官方机器人在 QQ 侧开启「接收所有消息」后，群里每条消息都会以
``GROUP_MESSAGE_CREATE`` 事件推送（含 @机器人 的消息，其事件类
``GroupAtMessageCreateEvent`` 是本类的子类）。这里只做一件事：
提取正文与表情计数写入 SQLite，供日报查询。

**只存文本**：图片/文件/富媒体消息没有正文，直接丢弃，与参考插件
的「只分析文字」口径一致。
"""

from __future__ import annotations

from datetime import datetime, timezone

from nonebot import get_plugin_config, on_message
from nonebot.adapters import Event
from nonebot.adapters.qq import GroupMessageCreateEvent
from nonebot.log import logger

from .config import Config
from . import store

plugin_config = get_plugin_config(Config)


def _parse_ts(event: GroupMessageCreateEvent) -> int:
    """把事件自带的 ISO 时间戳转成秒级；解析失败退回当前时间。"""
    raw = event.timestamp or ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return 0


async def _record(event: GroupMessageCreateEvent) -> None:
    if event.author.bot:
        return
    message = event.get_message()
    text = message.extract_plain_text().strip()
    if not text:
        return
    emoji_count = sum(1 for seg in message if seg.type == "emoji")

    ts = _parse_ts(event) or int(datetime.now().timestamp())
    nickname = (event.author.username or "").strip()
    try:
        await store.insert_message(
            group_openid=event.group_openid,
            member_openid=event.author.member_openid,
            nickname=nickname,
            text=text,
            emoji_count=emoji_count,
            ts=ts,
            source="qq",
        )
    except Exception:
        logger.exception("[群日报] 消息落库失败")


async def _check(event: Event) -> bool:
    return isinstance(event, GroupMessageCreateEvent)


ingest_matcher = on_message(rule=_check, priority=1, block=False)


@ingest_matcher.handle()
async def _(event: GroupMessageCreateEvent):
    await _record(event)

# -*- coding: utf-8 -*-
"""清洗后的消息结构与时间窗口解析。

移植自 bison5 的 ``collect.py``，采集来源从 OneBot 历史接口换成了
实时落库（官方机器人没有历史消息接口，只能收到什么存什么）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

EMPTY_NAME = "?"
"""落库昵称为空时的占位；出报告前会被成员信息接口补全。"""


@dataclass(slots=True)
class ChatMessage:
    """一条清洗后的群消息（官方机器人版，user_id 是 openid 字符串）。"""

    member_openid: str
    nickname: str
    text: str
    timestamp: int
    emoji_count: int = 0

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp)

    @property
    def length(self) -> int:
        """消息正文字数。"""
        return len(self.text)

    # stats.py 沿用 OneBot 版的字段名
    @property
    def user_id(self) -> str:
        return self.member_openid

    @property
    def image_count(self) -> int:
        return 0

    @property
    def reply_count(self) -> int:
        return 0

    @property
    def emoji_types(self) -> dict[str, int]:
        return {"emoji": self.emoji_count} if self.emoji_count else {}


@dataclass(slots=True)
class ChatArchive:
    """一次查询的结果。"""

    group_openid: str
    messages: list[ChatMessage] = field(default_factory=list)
    member_count: int = 0
    group_name: str = ""
    fetched_at: int = 0
    window_label: str = "今日"
    truncated: bool = False
    """机器人离线等原因导致窗口覆盖不完整时置位（由查询方估计）。"""

    @property
    def group_id(self) -> str:
        return self.group_openid

    @property
    def first_time(self) -> int | None:
        return self.messages[0].timestamp if self.messages else None

    @property
    def last_time(self) -> int | None:
        return self.messages[-1].timestamp if self.messages else None

    @property
    def time_range_text(self) -> str:
        """采集时间范围的可读文本。"""
        if not self.messages:
            return "未知"
        return (
            f"{self.messages[0].dt:%m-%d %H:%M} ~ {self.messages[-1].dt:%m-%d %H:%M}"
        )

    @property
    def dt_text(self) -> str:
        """报告生成时间的可读文本。"""
        import time

        return datetime.fromtimestamp(self.fetched_at or time.time()).strftime(
            "%Y-%m-%d %H:%M"
        )

    def filter_window(self, start_ts: int, end_ts: int) -> int:
        """只保留落在时间窗口内的消息，返回被丢弃的条数。"""
        before = len(self.messages)
        self.messages = [
            m for m in self.messages if start_ts <= m.timestamp <= end_ts
        ]
        return before - len(self.messages)

    def recent(self, limit: int) -> list[ChatMessage]:
        """取最近 ``limit`` 条消息（不足则全取）。"""
        if limit <= 0:
            return self.messages
        return self.messages[-limit:]


def resolve_window(mode: str, now: datetime | None = None) -> tuple[int, int, str]:
    """解析日报要覆盖的时间窗口。

    Args:
        mode: ``today`` / ``24h`` / ``yesterday`` / ``hours:N``。
        now: 当前时间，默认取本地时间。

    Returns:
        ``(起始时间戳, 结束时间戳, 可读标签)``；无法解析时回落到今天。
    """
    now = now or datetime.now()
    end = int(now.timestamp())
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if mode == "yesterday":
        y_start = day_start - timedelta(days=1)
        return int(y_start.timestamp()), int(day_start.timestamp()), "昨日"

    if mode == "24h":
        return end - 86400, end, "最近 24 小时"

    if mode.startswith("hours:"):
        try:
            hours = int(mode.split(":", 1)[1])
            if hours > 0:
                return end - hours * 3600, end, f"最近 {hours} 小时"
        except ValueError:
            pass

    # 默认：今天 0 点至今
    return int(day_start.timestamp()), end, "今日"

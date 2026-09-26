"""群聊统计分析。

基于 :class:`ChatArchive` 计算活跃度、时段分布、用户贡献榜等指标，
全部为纯函数，不依赖 nonebot，可独立测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .collect import ChatArchive, ChatMessage


@dataclass(slots=True)
class EmojiStats:
    """表情使用统计，按 QQ 的表情分类拆分。

    QQ 的表情分四类：普通表情 face、商城表情 mface、超级表情 bface、
    扩展表情 sface。分类信息都在 CQ 码的 type 上。
    """

    face: int = 0
    mface: int = 0
    bface: int = 0
    sface: int = 0
    other: int = 0

    @property
    def total(self) -> int:
        """表情总数。"""
        return self.face + self.mface + self.bface + self.sface + self.other

    def as_pairs(self) -> list[tuple[str, int]]:
        """按数量降序返回 ``(名称, 数量)``，跳过零项。"""
        pairs = [
            ("普通表情", self.face),
            ("商城表情", self.mface),
            ("超级表情", self.bface),
            ("小表情", self.sface),
            ("其他", self.other),
        ]
        return [(n, c) for n, c in pairs if c]


@dataclass(slots=True)
class UserStat:
    """单个用户的发言统计。"""

    user_id: int
    nickname: str
    count: int = 0
    char_count: int = 0
    image_count: int = 0
    reply_count: int = 0
    emoji_count: int = 0

    @property
    def avg_length(self) -> float:
        """平均每条消息字数。"""
        return self.char_count / self.count if self.count else 0.0


@dataclass(slots=True)
class ChatStats:
    """一次采集的完整统计结果。"""

    total_messages: int = 0
    participant_count: int = 0
    member_count: int = 0
    total_chars: int = 0
    total_images: int = 0
    total_replies: int = 0
    avg_length: float = 0.0
    active_ratio: float = 0.0
    first_time: int | None = None
    last_time: int | None = None
    hourly: list[int] = field(default_factory=lambda: [0] * 24)
    top_users: list[UserStat] = field(default_factory=list)
    users: dict[int, UserStat] = field(default_factory=dict)
    longest_message: ChatMessage | None = None
    emoji: EmojiStats = field(default_factory=EmojiStats)

    @property
    def duration_hours(self) -> float:
        """采集覆盖的时间跨度（小时）。"""
        if self.first_time is None or self.last_time is None:
            return 0.0
        return max(0.0, (self.last_time - self.first_time) / 3600)

    @property
    def messages_per_hour(self) -> float:
        """每小时消息数。"""
        h = self.duration_hours
        return self.total_messages / h if h > 0 else float(self.total_messages)

    @property
    def peak_hour(self) -> int:
        """最活跃的小时，0-23；无数据时返回 0。"""
        if not any(self.hourly):
            return 0
        return self.hourly.index(max(self.hourly))

    @property
    def time_range_text(self) -> str:
        """时间范围的可读文本。"""
        if self.first_time is None or self.last_time is None:
            return "未知"
        fmt = "%m-%d %H:%M"
        return (
            f"{datetime.fromtimestamp(self.first_time):{fmt}}"
            f" ~ {datetime.fromtimestamp(self.last_time):{fmt}}"
        )

    @property
    def top_user_text(self) -> str:
        """贡献榜前三的可读文本。"""
        if not self.top_users:
            return "无"
        return "、".join(f"{u.nickname}({u.count})" for u in self.top_users[:3])


def analyze(archive: ChatArchive) -> ChatStats:
    """计算归档的统计指标。

    Args:
        archive: 消息采集结果。

    Returns:
        统计结果 :class:`ChatStats`。
    """
    messages = archive.messages
    stats = ChatStats(
        total_messages=len(messages),
        member_count=archive.member_count,
        first_time=archive.first_time,
        last_time=archive.last_time,
    )
    if not messages:
        return stats

    users: dict[int, UserStat] = {}
    for msg in messages:
        stats.total_chars += msg.length
        stats.total_images += msg.image_count
        stats.total_replies += msg.reply_count
        stats.hourly[msg.dt.hour] += 1

        if stats.longest_message is None or msg.length > stats.longest_message.length:
            stats.longest_message = msg

        stat = users.get(msg.user_id)
        if stat is None:
            stat = UserStat(user_id=msg.user_id, nickname=msg.nickname)
            users[msg.user_id] = stat
        stat.count += 1
        stat.char_count += msg.length
        stat.image_count += msg.image_count
        stat.reply_count += msg.reply_count
        stat.emoji_count += sum(msg.emoji_types.values())

    stats.avg_length = stats.total_chars / stats.total_messages
    stats.users = users
    stats.participant_count = len(users)
    stats.top_users = sorted(users.values(), key=lambda u: (-u.count, u.user_id))[:20]
    if archive.member_count > 0:
        stats.active_ratio = min(1.0, stats.participant_count / archive.member_count)

    for msg in messages:
        for name, n in msg.emoji_types.items():
            if name == "face":
                stats.emoji.face += n
            elif name == "mface":
                stats.emoji.mface += n
            elif name == "bface":
                stats.emoji.bface += n
            elif name == "sface":
                stats.emoji.sface += n
            else:
                stats.emoji.other += n
    return stats


def compress_nicknames(messages: list[ChatMessage]) -> tuple[list[str], dict[str, str]]:
    """把长昵称替换为短代号，显著降低送入模型的 token 消耗。

    代号用**三位小写十六进制**而非十进制：三位十六进制可表示 4096 个用户，
    而三位十进制只有 1000 个，成员更多的群会溢出成四位、白白多占 token。

    Args:
        messages: 消息列表。

    Returns:
        ``(压缩后的文本行列表, 代号到昵称的映射)``。
    """
    name_to_code: dict[str, str] = {}
    code_to_name: dict[str, str] = {}
    lines: list[str] = []
    for msg in messages:
        if msg.nickname not in name_to_code:
            code = f"{len(name_to_code):03x}"  # 十六进制：000~fff 共 4096 个
            name_to_code[msg.nickname] = code
            code_to_name[code] = msg.nickname
        lines.append(f"{name_to_code[msg.nickname]}: {msg.text}")
    return lines, code_to_name


def restore_nicknames(text: str, code_to_name: dict[str, str]) -> str:
    """把总结文本中的代号还原为真实昵称。

    十六进制代号里含 ``a``~``f``，而聊天正文里 ``abc``、``face`` 这类英文词
    非常常见，所以边界必须收紧到「前后都不是字母数字」——只还原独立成词的
    代号，否则会把正常单词误换成昵称。

    Args:
        text: 模型输出文本。
        code_to_name: 代号到昵称的映射。

    Returns:
        还原后的文本。
    """
    if not code_to_name or not text:
        return text

    # 代号长的排前面，避免 3 位代号抢先匹配长代号的前缀
    codes = sorted(code_to_name, key=len, reverse=True)
    pattern = re.compile(
        r"(?<![0-9A-Za-z])(" + "|".join(re.escape(c) for c in codes) + r")(?![0-9A-Za-z])"
    )
    return pattern.sub(lambda m: code_to_name[m.group(1)], text)

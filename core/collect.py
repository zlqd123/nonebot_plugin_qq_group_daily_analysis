"""群消息采集与清洗（MaiBot 适配层）。

移植自 nonebot 版：数据结构与清洗规则（只保留纯文字、剔机器人自身消息、
纯图片/纯合并转发整条剔除、CQ 码表情统计）**原样保留**；采集来源从
「OneBot get_group_msg_history + chatrecorder 落库」换成 MaiBot 宿主的
消息接口（``ctx.message``）与 ChatHistory 库（``ctx.db``）——两者都由
plugin.py 注入的 ``host`` 对象封装，本模块只消费规整后的消息 dict。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .log import logger

# QQ 表情的 CQ 码类型：普通 / 商城 / 超级 / 小表情
EMOJI_CQ_TYPES = ("face", "mface", "bface", "sface")
# MaiBot 宿主把表情包组件统一序列化为 ``emoji`` 段
EMOJI_HOST_TYPES = EMOJI_CQ_TYPES + ("emoji",)
# 匹配任意 CQ 码，如 [CQ:image,file=xxx] / [CQ:face,id=1]
_CQ_RE = re.compile(r"\[CQ:([^,\]]+)(?:,[^\]]*)?\]")


@dataclass(slots=True)
class ChatMessage:
    """一条清洗后的群消息。"""

    message_id: int
    user_id: int
    nickname: str
    text: str
    timestamp: int
    msg_type: str = "text"
    image_count: int = 0
    reply_count: int = 0
    emoji_types: dict[str, int] = field(default_factory=dict)
    """按段类型统计的表情数量，如 ``{"face": 3, "bface": 1}``。"""
    forward_count: int = 0
    """本条消息里被丢弃的合并转发/文件段数量。"""
    image_count_dropped: int = 0
    """仅图片、无正文而被整条丢弃的消息里，其图片数。用于统计口径说明。"""

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp)

    @property
    def length(self) -> int:
        """消息正文字数，由 :attr:`text` 推导，避免与文本失同步。"""
        return len(self.text)


@dataclass(slots=True)
class ChatArchive:
    """一次采集的结果。"""

    group_id: int
    messages: list[ChatMessage] = field(default_factory=list)
    member_count: int = 0
    group_name: str = ""
    fetched_at: int = 0
    window_label: str = "今日"
    truncated: bool = False
    """是否未能回溯覆盖完整的时间窗口。"""
    dropped_image_only: int = 0
    """因只有图片、没有正文而被整条剔除的消息数。"""
    dropped_forward_only: int = 0
    """因只有合并转发、没有正文而被整条剔除的消息数。"""

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
        return datetime.fromtimestamp(self.fetched_at or time.time()).strftime(
            "%Y-%m-%d %H:%M"
        )

    def filter_window(self, start_ts: int, end_ts: int) -> int:
        """只保留落在时间窗口内的消息。

        采集阶段为了少打接口会多取一些消息，真正决定日报范围的是这一步。

        Args:
            start_ts: 起始时间戳（含）。
            end_ts: 结束时间戳（含）。

        Returns:
            被丢弃的消息条数。
        """
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


def _count_types(segments: Any) -> dict[str, int]:
    """统计消息段数组里各表情类型的数量。"""
    out: dict[str, int] = {}
    for s in segments or []:
        if isinstance(s, dict) and s.get("type") in EMOJI_HOST_TYPES:
            out[s["type"]] = out.get(s["type"], 0) + 1
    return out


FORWARD_SEG_TYPES = frozenset({"forward", "json", "file"})
"""需要整段丢弃的段类型。

``forward`` 是合并转发，``json`` 是协议端的 structmsg（内部同样承载
聊天记录），``file`` 是文件分享。它们的 ``data.content`` 可能是字符串化的
节点列表，直接进正文会变成一大坨 JSON。
"""

_FORWARD_TEXT_RE = re.compile(
    r'"(?:self_id|user_id|message_seq)"\s*:|"structmsg"|"com\.tencent\.structmsg"'
)
"""正文里出现这些片段，说明客户端把转发内容塞进了 text 段，需要整段丢弃。"""


def _looks_like_forward(text: str) -> bool:
    """判断一段正文是否其实是合并转发的内容。

    多数实现把转发放在独立的 forward/json 段里，但也有客户端图省事，
    把节点列表当普通文本发过来。不拦的话这些 JSON 会直接污染日报正文。

    Args:
        text: 待判断的正文。

    Returns:
        是转发内容时返回 ``True``。
    """
    return bool(_FORWARD_TEXT_RE.search(text))


def _extract_text_from_cq(raw: str) -> tuple[str, int, int, dict[str, int], int]:
    """从 CQ 码字符串正文里提取文本与计数（兜底路径）。

    Returns:
        ``(文本, 图片数, 回复数, 表情类型字典, 合并消息数)``。
    """
    images = replies = forwards = 0
    emojis: dict[str, int] = {}

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
        return ""

    # 用空格而不是空串替换：直接删会让 CQ 码两侧的文字粘成一个词
    stripped = _CQ_RE.sub(lambda _m: _count(_m) or " ", raw).strip()
    if stripped and not _looks_like_forward(stripped):
        text = re.sub(r"[ \t]+", " ", stripped).strip()
        return text, images, replies, emojis, forwards
    return "", images, replies, emojis, forwards


def _extract_text(message: dict) -> tuple[str, int, int, dict[str, int], int]:
    """从宿主消息 dict 里提取正文文本，附带图片数、回复数、表情分布与合并消息数。

    **只保留纯文字**：图片不做识别也不描述，合并转发整段丢弃，
    图片/转发消息若没有正文则不计入统计。

    Args:
        message: 宿主消息 dict（结构经 :func:`_normalize_message` 兜底）。

    Returns:
        ``(文本, 图片数, 回复数, 表情类型字典, 合并消息数)``。
    """
    segments = message.get("_segments")
    texts: list[str] = []
    images = replies = forwards = 0
    emojis: dict[str, int] = {}

    if isinstance(segments, list) and segments:
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            stype = str(seg.get("type") or "")
            data = seg.get("data")
            if stype == "text":
                # 宿主序列化里 text 段的 data 就是纯字符串；兼容 {"text": ...}
                content = (
                    data.strip()
                    if isinstance(data, str)
                    else str((data or {}).get("text") or "").strip() if isinstance(data, dict) else ""
                )
                # 转发内容被塞进 text 段的情况同样要拦
                if content and not _looks_like_forward(content):
                    texts.append(content)
            elif stype == "image":
                images += 1
            elif stype == "reply":
                replies += 1
            elif stype in FORWARD_SEG_TYPES:
                forwards += 1
            elif stype in EMOJI_HOST_TYPES:
                emojis[stype] = emojis.get(stype, 0) + 1
    else:
        # 段结构缺失：按 CQ 码字符串兜底解析
        raw = str(message.get("_raw") or "")
        if raw:
            return _extract_text_from_cq(raw)

    return (
        re.sub(r"[ \t]+", " ", " ".join(texts)).strip(),
        images,
        replies,
        emojis,
        forwards,
    )


def _dig(obj: Any, *paths: str) -> Any:
    """按顺序尝试多条点路径，返回第一个非空值。"""
    for path in paths:
        cur: Any = obj
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                cur = getattr(cur, part, None)
            if cur is None:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return None


def _normalize_message(msg: dict) -> dict:
    """把宿主消息 dict 规整成统一的内部形状。

    MaiBot 不同版本/不同读取途径（ctx.message、ctx.db）返回的消息结构
    不完全一致，这里做多形状兜底，抽出：正文、发送者、时间戳、段列表。

    Args:
        msg: 宿主返回的单条消息。

    Returns:
        带以下键的 dict：``_user_id`` / ``_nickname`` / ``_time`` /
        ``_message_id`` / ``_segments`` / ``_raw``。
    """
    if not isinstance(msg, dict):
        return {}

    user_info = msg.get("user_info") if isinstance(msg.get("user_info"), dict) else {}
    sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}

    # 宿主 1.3.x 的形状：message_info.user_info.user_id（三层嵌套，值是字符串）
    user_id = _dig(
        msg,
        "message_info.user_info.user_id",
        "user_info.user_id",
        "message_info.user_id",
        "user_id",
        "sender.user_id",
    )
    try:
        user_id = int(user_id or 0)
    except (TypeError, ValueError):
        user_id = 0

    nickname = _dig(
        msg,
        "message_info.user_info.user_cardname",
        "message_info.user_info.user_nickname",
        "user_info.user_cardname",
        "user_info.user_nickname",
        "user_info.nickname",
        "user_nickname",
        "nickname",
        "sender.card",
        "sender.nickname",
    )
    nickname = str(nickname).strip() if isinstance(nickname, str) else ""
    if not nickname:
        fallback = _dig(sender, "card", "nickname", "role")
        nickname = str(fallback).strip() if isinstance(fallback, str) and fallback.strip() else ""

    mtime = _dig(msg, "message_info.time", "message_info.timestamp", "time", "timestamp", "create_time")
    try:
        ts = int(float(mtime or 0))
    except (TypeError, ValueError):
        ts = 0
    # 毫秒时间戳兜底（MaiBot 库里部分列存毫秒）
    if ts > 10_000_000_000:
        ts //= 1000

    message_id = _dig(msg, "message_info.message_id", "message_id", "id", "message_seq")
    try:
        message_id = int(message_id or 0)
    except (TypeError, ValueError):
        message_id = 0

    # 段结构：宿主 raw_message / message_segments / segments / message
    segments = _dig(
        msg, "message_segments", "segments", "message", "raw_message",
        "message_info.message_segments",
    )
    if not isinstance(segments, list):
        segments = None

    # 原始正文（CQ 码字符串或纯文本）：宿主 processed_plain_text / plain_text …
    raw = _dig(
        msg,
        "processed_plain_text",
        "plain_text",
        "processed_text",
        "raw_message_str",
        "message_str",
        "text",
        "content",
    )
    raw = raw if isinstance(raw, str) else ""

    return {
        "_user_id": user_id,
        "_nickname": nickname,
        "_time": ts,
        "_message_id": message_id,
        "_segments": segments,
        "_raw": raw,
    }


def resolve_window(
    mode: str, now: datetime | None = None
) -> tuple[int, int, str]:
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


async def fetch_group_archive(
    host: Any,
    group_id: int,
    *,
    history_lens: int,
    source: str = "auto",
    member_cache_ttl: int = 21600,
    self_id: int | None = None,
    since_ts: int = 0,
    end_ts: int | None = None,
    member_counts: dict[int, int] | None = None,
) -> ChatArchive:
    """采集群消息并清洗。

    来源优先级：**宿主消息接口优先，ChatHistory 库只做兜底**（``auto``）。

    - ``message_api``：调宿主 ``ctx.message``（按时间窗口取该群消息），
      覆盖的是完整时间窗口。日报默认走这条。
    - ``db``：经宿主 ``ctx.db`` 直查 ChatHistory。零接口调用，但只覆盖
      机器人在线期间的消息，且字段要经 :func:`_normalize_message` 反推。
      **仅作兜底**。

    Args:
        host: 宿主适配对象（plugin.py 注入，见 :class:`plugin.MaiBotHost`）。
        group_id: 群号。
        history_lens: 最多取多少条（安全上限）。
        source: ``auto`` / ``message_api`` / ``db``。
        member_cache_ttl: 群元信息缓存时长（秒）。
        self_id: 机器人自身 QQ 号，用于过滤自身消息。
        since_ts: 需要覆盖到的时间戳（窗口起点），0 表示不限。
        end_ts: 窗口终点时间戳；``None`` 表示现在。
        member_counts: 群成员数覆盖表（宿主取不到人数时兜底）。

    Returns:
        采集结果 :class:`ChatArchive`。
    """
    archive = ChatArchive(group_id=group_id, fetched_at=int(time.time()))
    archive.member_count, archive.group_name = await _get_group_meta(
        host, group_id, member_cache_ttl, member_counts
    )

    collected: dict[int, ChatMessage] = {}
    dropped: dict[int, str] = {}
    used = "message_api"

    if source in ("message_api", "auto"):
        raw_messages, used = await _collect_from_host(
            host, group_id, history_lens, since_ts, end_ts, "message_api"
        )
        collected, dropped = _parse_all(raw_messages, self_id)

    if not collected and source in ("auto", "db"):
        if source == "auto":
            logger.warning(
                f"[群日报] 群 {group_id} 宿主消息接口未取到消息，回落到 ChatHistory 库"
            )
        raw_messages, used = await _collect_from_host(
            host, group_id, history_lens, since_ts, end_ts, "db"
        )
        collected, dropped = _parse_all(raw_messages, self_id)

    archive.dropped_image_only = sum(1 for v in dropped.values() if v == "image")
    archive.dropped_forward_only = sum(1 for v in dropped.values() if v == "forward")
    archive.messages = sorted(collected.values(), key=lambda m: m.timestamp)
    if archive.dropped_image_only or archive.dropped_forward_only:
        logger.info(
            f"[群日报] 群 {group_id} 按「只分析文字」剔除："
            f"纯图片 {archive.dropped_image_only} 条、"
            f"纯合并转发 {archive.dropped_forward_only} 条"
        )
    logger.info(
        f"[群日报] 群 {group_id} 采集到 {len(archive.messages)} 条消息"
        f"（目标 {history_lens}，来源 {used}），成员 {archive.member_count} 人"
    )
    _warn_if_nickname_missing(group_id, archive)

    oldest_collected = min((m.timestamp for m in archive.messages), default=0)
    if since_ts and oldest_collected > since_ts:
        archive.truncated = True
        logger.warning(
            f"[群日报] 群 {group_id} 历史只回溯到 {_fmt(oldest_collected)}，"
            f"未达到目标窗口起点 {_fmt(since_ts)}。"
            f"MaiBot 只记录机器人在线期间的消息，离线时段无法补齐"
        )
    return archive


def _parse_all(
    raw_messages: list[dict], self_id: int | None
) -> tuple[dict[int, ChatMessage], dict[int, str]]:
    """把一批宿主消息 dict 清洗成 ``ChatMessage``。"""
    collected: dict[int, ChatMessage] = {}
    dropped: dict[int, str] = {}
    for msg in raw_messages:
        if not isinstance(msg, dict):
            continue
        norm = _normalize_message(msg)
        parsed = _parse_message(norm, self_id)
        if isinstance(parsed, ChatMessage):
            key = parsed.message_id or f"n{len(collected)}{parsed.timestamp}"
            collected[key] = parsed
        elif isinstance(parsed, _Dropped):
            key = norm.get("_message_id") or 0
            dropped[key] = parsed.reason
    if raw_messages and not collected and not dropped:
        # 一条都没解析出来：打出首条原始消息样本，便于对齐字段路径
        sample = raw_messages[0]
        logger.warning(
            f"[群日报] 收到 {len(raw_messages)} 条原始消息但全部解析失败，"
            f"首条样本（截断）: {str(sample)[:600]}"
        )
    return collected, dropped


def _warn_if_nickname_missing(group_id: int, archive: ChatArchive) -> None:
    """昵称大面积退化成 QQ 号时给出告警。

    Args:
        group_id: 群号。
        archive: 采集结果。
    """
    if not archive.messages:
        return
    degraded = sum(1 for m in archive.messages if m.nickname == str(m.user_id))
    if degraded / len(archive.messages) <= 0.5:
        return
    logger.warning(
        f"[群日报] 群 {group_id} 有 {degraded}/{len(archive.messages)} 条消息的昵称"
        f"退化为 QQ 号，请检查宿主消息接口返回的 user_info 字段"
    )


async def _collect_from_host(
    host: Any,
    group_id: int,
    history_lens: int,
    since_ts: int,
    end_ts: int | None,
    source: str,
) -> tuple[list[dict], str]:
    """经宿主适配层拉取一个时间窗口内的消息。

    Args:
        host: 宿主适配对象。
        group_id: 群号。
        history_lens: 条数上限。
        since_ts: 窗口起点。
        end_ts: 窗口终点，``None`` 表示现在。
        source: ``message_api`` / ``db``。

    Returns:
        ``(消息 dict 列表, 实际使用的来源名)``；失败时返回 ``([], source)``。
    """
    try:
        messages = await host.get_group_messages(
            group_id,
            start_ts=since_ts or 0,
            end_ts=end_ts or int(time.time()),
            limit=history_lens,
            source=source,
        )
        return list(messages or []), source
    except Exception as e:
        logger.warning(f"[群日报] 群 {group_id} 经宿主采集消息失败（{source}）: {e!s}")
        return [], source


def _fmt(ts: int) -> str:
    """时间戳转可读文本。"""
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "未知"


# ── 群元信息缓存（成员数接口开销不小，不宜频繁调用）──────────
_META_CACHE: dict[int, tuple[float, int, str]] = {}


async def _get_group_meta(
    host: Any, group_id: int, ttl: int, member_counts: dict[int, int] | None = None
) -> tuple[int, str]:
    """取群成员数与群名，带 TTL 缓存；配置覆盖表优先。

    Args:
        host: 宿主适配对象。
        group_id: 群号。
        ttl: 缓存时长（秒）。
        member_counts: 群成员数覆盖表。

    Returns:
        ``(成员数, 群名)``；失败时成员数为 0、群名为空串。
    """
    override = (member_counts or {}).get(group_id)
    now = time.time()
    hit = _META_CACHE.get(group_id)
    if hit and ttl > 0 and now - hit[0] < ttl:
        members, name = hit[1], hit[2]
        return override if override is not None else members, name

    try:
        members, name = await host.get_group_meta(group_id)
    except Exception as e:
        logger.warning(f"[群日报] 获取群 {group_id} 元信息失败: {e!s}")
        members, name = 0, ""
    if override is not None:
        members = override
    _META_CACHE[group_id] = (now, members, name)
    return members, name


@dataclass(slots=True)
class _Dropped:
    """被「只分析文字」规则剔除的消息标记。"""

    reason: str


def _parse_message(norm: dict, self_id: int | None) -> ChatMessage | _Dropped | None:
    """把规整后的消息 dict 转成 :class:`ChatMessage`。

    **只保留纯文字**：图片不做识别也不描述，合并转发整段丢弃；
    没有正文的消息返回 :class:`_Dropped`（注明剔除原因）或 ``None``（无效消息）。
    """
    user_id = int(norm.get("_user_id") or 0)
    if not user_id or (self_id is not None and user_id == self_id):
        return None

    text, images, replies, emojis, forwards = _extract_text(norm)
    # 只分析文字：没有正文的消息（纯图片 / 纯合并转发 / 纯表情）不参与统计。
    if not text:
        if forwards and not images:
            return _Dropped("forward")
        if images:
            return _Dropped("image")
        return None

    nickname = str(norm.get("_nickname") or "").strip() or str(user_id)
    return ChatMessage(
        message_id=int(norm.get("_message_id") or 0),
        user_id=user_id,
        nickname=nickname,
        text=text,
        timestamp=int(norm.get("_time") or 0),
        msg_type="group",
        image_count=images,
        reply_count=replies,
        emoji_types=emojis,
        forward_count=forwards,
    )

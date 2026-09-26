"""群消息采集与清洗。

通过 OneBot v11 的 `get_group_msg_history` 分页回溯拉取群内消息，
清洗成结构化的 :class:`ChatArchive` 供统计与总结使用。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from nonebot.log import logger

# QQ 表情的 CQ 码类型：普通 / 商城 / 超级 / 小表情
EMOJI_CQ_TYPES = ("face", "mface", "bface", "sface")
# 匹配任意 CQ 码，如 [CQ:image,file=xxx] / [CQ:face,id=1]
_CQ_RE = re.compile(r"\[CQ:([^,\]]+)(?:,[^\]]*)?\]")

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot


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
    """按 CQ 码类型统计的表情数量，如 ``{"face": 3, "bface": 1}``。"""
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

        采集阶段为了少打 API 会多取一些消息，真正决定日报范围的是这一步。

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
    """统计 OneBot 段数组里各表情类型的数量。"""
    out: dict[str, int] = {}
    for s in segments or []:
        if isinstance(s, dict) and s.get("type") in EMOJI_CQ_TYPES:
            out[s["type"]] = out.get(s["type"], 0) + 1
    return out


def _sender_name(sender: dict) -> str:
    """从 OneBot 的 sender 结构中取展示名，优先群名片。"""
    return (
        sender.get("card")
        or sender.get("nickname")
        or sender.get("role")
        or str(sender.get("user_id", ""))
    )


FORWARD_SEG_TYPES = frozenset({"forward", "json", "file"})
"""需要整段丢弃的段类型。

``forward`` 是合并转发，``json`` 是 NapCat/QQ 的 structmsg（内部同样承载
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


def _extract_text(message: dict) -> tuple[str, int, int, dict[str, int], int]:
    """提取消息正文文本，附带图片数、回复数、表情分布与合并消息数。

    **只保留纯文字**：图片不做识别也不描述，合并转发整段丢弃，
    图片/转发消息若没有正文则不计入统计。

    Args:
        message: OneBot v11 的原始消息结构。

    Returns:
        ``(文本, 图片数, 回复数, 表情类型字典, 合并消息数)``。
    """
    raw = message.get("message")
    if not raw:
        # 部分实现只给字符串形式的 raw_message，或 message 段数组为空
        raw = message.get("raw_message")
    texts: list[str] = []
    images = 0
    replies = 0
    forwards = 0
    emojis: dict[str, int] = {}

    if isinstance(raw, str):
        # 字符串形式可能夹带 CQ 码。用通用正则一次处理所有类型——
        # 之前只剥离 image/reply/at，表情码会直接漏进日报正文。
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
        # （"前面的话[CQ:forward]后面的话" → "前面的话后面的话"）。
        # 多余空白由结尾的 re.sub 统一收敛。
        stripped = _CQ_RE.sub(lambda _m: _count(_m) or " ", raw).strip()
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
                # 转发内容被塞进 text 段的情况同样要拦
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
    bot: Bot,
    group_id: int,
    *,
    history_lens: int,
    source: str = "auto",
    page_size: int = 500,
    max_pages: int = 40,
    interval: float = 5.0,
    member_cache_ttl: int = 21600,
    self_id: int | None = None,
    since_ts: int = 0,
) -> ChatArchive:
    """采集群消息并清洗。

    两种来源：

    - ``chatrecorder``：读 nonebot-plugin-chatrecorder 的落库记录。
      **不产生任何 OneBot API 调用**，没有风控风险，是推荐路径。
      局限：只覆盖机器人在线期间收到的消息。
    - ``api``：调 OneBot v11 的 ``get_group_msg_history`` 分页回溯。
      以「回溯到 ``since_ts``」为终止条件，因此覆盖的是**完整时间窗口**，
      而不是「抓到 N 条为止」。

    ``auto`` 会先试落库，库里没有数据再回落 API。

    Args:
        bot: OneBot v11 机器人实例。
        group_id: 群号。
        history_lens: 最多取多少条（安全上限，非终止条件）。
        source: ``auto`` / ``chatrecorder`` / ``api``。
        page_size: api 模式单次请求条数。
        max_pages: api 模式最大翻页数。
        interval: api 模式翻页间隔（秒）。
        member_cache_ttl: 群成员数缓存时长（秒）。
        self_id: 机器人自身 QQ 号，用于过滤自身消息。
        since_ts: 需要回溯到的时间戳，0 表示不限。

    Returns:
        采集结果 :class:`ChatArchive`。
    """
    archive = ChatArchive(group_id=group_id, fetched_at=int(time.time()))
    archive.member_count, archive.group_name = await _get_group_meta(
        bot, group_id, member_cache_ttl
    )

    collected: dict[int, ChatMessage] = {}
    dropped: dict[int, str] = {}
    used = "api"

    if source in ("auto", "chatrecorder"):
        collected, dropped = await _collect_from_db(group_id, history_lens, self_id)
        used = "chatrecorder"

    if not collected and source in ("auto", "api"):
        if used == "chatrecorder":
            logger.info(f"[群日报] 群 {group_id} 落库无记录，回落到 API 拉取")
        collected, archive.truncated, dropped = await _collect_from_api(
            bot,
            group_id,
            history_lens,
            page_size,
            max_pages,
            interval,
            self_id,
            since_ts,
        )
        used = "api"

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
    return archive


def _chatrecorder_loaded() -> bool:
    """判断 chatrecorder 是否已作为插件启用。

    装了包不等于启用了插件：未启用时不会建表，落库必然为空。

    Returns:
        已启用返回 ``True``。
    """
    try:
        from nonebot import get_plugin

        return get_plugin("chatrecorder") is not None
    except Exception:
        return False


async def _collect_from_db(
    group_id: int, history_lens: int, self_id: int | None
) -> dict[int, ChatMessage]:
    """从 chatrecorder 落库读取消息，插件不可用时返回空字典。

    这里不复用 ``get_message_records``，因为它只返回 ``MessageRecord``，
    不含发送者信息；昵称与 user_id 需要 join ``SessionModel``/``UserModel`` 才能取到。

    Args:
        group_id: 群号。
        history_lens: 最多取多少条。

    Returns:
        ``({message_id: ChatMessage}, {message_id: 剔除原因})``。
        self_id: 机器人自身 QQ 号，用于过滤自身消息。

    Returns:
        ``{message_id: ChatMessage}``。
    """
    # 包在 site-packages 里「装了就可导入」，但没在 plugins 列表里启用时
    # 并没有建表、也没有数据。此时直接跳过，免得每次都白查一次再打警告。
    if not _chatrecorder_loaded():
        return {}, {}

    try:
        from nonebot_plugin_chatrecorder.model import MessageRecord
        from nonebot_plugin_orm import get_session
        from nonebot_plugin_uninfo.model import SceneType
        from nonebot_plugin_uninfo.orm import SceneModel, SessionModel, UserModel
        from sqlalchemy import select
    except ImportError:
        return {}, {}

    try:
        async with get_session() as db:
            stmt = (
                select(
                    MessageRecord.message_id,
                    MessageRecord.time,
                    MessageRecord.plain_text,
                    MessageRecord.message,
                    UserModel.user_id,
                    SessionModel.member_data,
                    UserModel.user_data,
                )
                .join(SessionModel, SessionModel.id == MessageRecord.session_persist_id)
                .join(UserModel, UserModel.id == SessionModel.user_persist_id)
                # scene_id / scene_type 挂在 SceneModel 上，SessionModel 只有外键
                .join(SceneModel, SceneModel.id == SessionModel.scene_persist_id)
                .where(MessageRecord.type == "message")
                .where(SceneModel.scene_id == str(group_id))
                .where(SceneModel.scene_type == int(SceneType.GROUP))
                .order_by(MessageRecord.id.desc())
                .limit(history_lens)
            )
            rows = (await db.execute(stmt)).all()
    except Exception as e:
        logger.warning(f"[群日报] 读取 chatrecorder 落库失败: {e!s}")
        return {}, {}

    collected: dict[int, ChatMessage] = {}
    dropped: dict[int, str] = {}
    for message_id, mtime, plain_text, raw_segments, user_id, member_data, user_data in rows:
        text = (plain_text or "").strip()
        if not text or not user_id:
            continue
        if self_id is not None and str(user_id) == str(self_id):
            continue

        member = member_data if isinstance(member_data, dict) else {}
        user = user_data if isinstance(user_data, dict) else {}
        nickname = member.get("card") or user.get("nickname") or str(user_id)
        segments = raw_segments if isinstance(raw_segments, list) else []
        mid = int(message_id) if str(message_id).lstrip("-").isdigit() else 0

        n_img = sum(1 for s in segments if isinstance(s, dict) and s.get("type") == "image")
        n_fwd = sum(
            1 for s in segments
            if isinstance(s, dict) and s.get("type") in FORWARD_SEG_TYPES
        )
        # 只分析文字：落库的 plain_text 若本身是转发内容，要丢掉
        if _looks_like_forward(text):
            dropped[mid] = "forward"
            continue

        collected[mid] = ChatMessage(
            message_id=mid,
            user_id=int(user_id),
            nickname=str(nickname),
            text=text,
            # 落库 time 为 UTC 无时区时间
            timestamp=int(mtime.replace(tzinfo=timezone.utc).timestamp()),
            image_count=n_img,
            reply_count=sum(1 for s in segments if isinstance(s, dict) and s.get("type") == "reply"),
            emoji_types=_count_types(segments),
            forward_count=n_fwd,
        )
    return collected, dropped


async def _collect_from_api(
    bot: Bot,
    group_id: int,
    history_lens: int,
    page_size: int,
    max_pages: int,
    interval: float,
    self_id: int | None,
    since_ts: int = 0,
) -> tuple[dict[int, ChatMessage], bool]:
    """通过 OneBot API 分页回溯拉取消息。

    以「回溯到 ``since_ts``」为终止条件，从而保证覆盖完整的时间窗口；
    翻页数与总条数都只是安全上限。

    Args:
        bot: OneBot v11 机器人实例。
        group_id: 群号。
        history_lens: 最多取多少条（安全上限）。
        page_size: 单次请求条数。
        max_pages: 最大翻页数。
        interval: 翻页间隔（秒）。
        self_id: 机器人自身 QQ 号。
        since_ts: 需要回溯到的时间戳，0 表示不限。

    Returns:
        ``({message_id: ChatMessage}, 是否未能覆盖时间窗口, {message_id: 剔除原因})``。
        第三个返回值里 ``image`` 表示纯图片无正文，``forward`` 表示纯合并转发。
    """
    collected: dict[int, ChatMessage] = {}
    dropped: dict[int, str] = {}
    message_seq: int | None = None
    last_newest_time = 0  # 首屏从 0 起，第 2 页起才开始比较
    pages = 0
    truncated = False

    # 停止条件：拿到目标条数、翻满页数上限、或**时间已覆盖到窗口起点**。
    # 按时间驱动才能保证「过去 24 小时」一定抓全，而不是抓到 N 条就停。
    # 纯图片/纯合并转发的消息不计入 collected，它们另有 dropped 计数。
    while pages < max_pages and (len(collected) + len(dropped)) < history_lens:
        if pages > 0 and interval > 0:
            await asyncio.sleep(interval)
        pages += 1

        try:
            # 首屏不能传 message_seq=None：NapCat 等实现的 schema 不接受显式 null，
            # 会直接报 "Schema compilation error: Expected string"。
            params: dict[str, Any] = {"group_id": group_id, "count": page_size}
            if message_seq is not None:
                params["message_seq"] = message_seq
            resp = await bot.get_group_msg_history(**params)
        except Exception as e:
            logger.warning(f"[群日报] 拉取群 {group_id} 历史消息失败（第 {pages} 页）: {e!s}")
            break

        raw_messages = resp.get("messages") or resp.get("data", {}).get("messages") or []
        if not raw_messages:
            break

        batch_added = 0
        for msg in raw_messages:
            msg_id = int(msg.get("message_id") or 0)
            if msg_id in collected or msg_id in dropped:
                continue
            parsed = _parse_message(msg, self_id)
            if isinstance(parsed, ChatMessage):
                collected[msg_id] = parsed
                batch_added += 1
            elif isinstance(parsed, _Dropped):
                # 记下来，避免翻页时反复解析同一条
                dropped[msg_id] = parsed.reason
            # None = 无效消息（机器人自己发的等），直接忽略

        if batch_added == 0:
            break

        # ── 游标推进 ──────────────────────────────────────
        # 关键：NapCat 的 message_id 是**随机**生成的，不随时间递增。
        # 实测 100 条消息里 message_id 有 48% 的相邻对是逆序的，
        # 所以绝不能用 min(message_id) 当游标——那等于锚定到时间轴
        # 中间某条随机消息上，翻页会原地打转（实测只能拿到 76~100 条）。
        # 正确的做法是取**时间上最老**的那条作为下一次游标。
        oldest_by_time = min(raw_messages, key=lambda m: int(m.get("time") or 0))
        oldest_id = int(oldest_by_time.get("message_id") or 0)
        oldest_time = int(oldest_by_time.get("time") or 0)
        newest_time = max(int(m.get("time") or 0) for m in raw_messages)

        # 用时间判断是否有进展：翻页是往**更早**的方向走，所以 newest_time
        # 应当递减；若没有变早（甚至变新），说明服务端不再给更早的数据了。
        if pages > 1 and newest_time >= last_newest_time:
            logger.debug(
                f"[群日报] 群 {group_id} 第 {pages} 页未取到更早的数据，停止翻页"
            )
            break
        last_newest_time = newest_time
        message_seq = oldest_id

        # 时间已经覆盖到窗口起点就够了，不必再翻——这是「按时间抓」的核心
        if since_ts and oldest_time <= since_ts:
            logger.info(
                f"[群日报] 群 {group_id} 已回溯到 {_fmt(oldest_time)}，覆盖目标窗口，停止翻页"
            )
            break

    oldest_collected = min((m.timestamp for m in collected.values()), default=0)
    if since_ts and oldest_collected > since_ts:
        truncated = True
        covered = (last_newest_time - oldest_collected) / 3600
        logger.warning(
            f"[群日报] 群 {group_id} 历史只回溯到 {_fmt(oldest_collected)}"
            f"（覆盖 {covered:.1f} 小时，{len(collected)} 条），"
            f"未达到目标窗口起点 {_fmt(since_ts)}。"
            f"可调大 gdr_fetch_page_size；连续快速调用该接口会触发 NapCat 限流，"
            f"反而拿到更少的数据"
        )
    elif pages >= max_pages and len(collected) < history_lens:
        truncated = True
        logger.warning(
            f"[群日报] 群 {group_id} 已达翻页上限 {max_pages}，"
            f"仅取到 {len(collected)}/{history_lens} 条"
        )
    return collected, truncated, dropped


def _fmt(ts: int) -> str:
    """时间戳转可读文本。"""
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "未知"


# ── 群元信息缓存（成员数接口返回体极大，不宜频繁调用）──────────
_META_CACHE: dict[int, tuple[float, int, str]] = {}


async def _get_group_meta(
    bot: Bot, group_id: int, ttl: int
) -> tuple[int, str]:
    """取群成员数与群名，带 TTL 缓存。

    Args:
        bot: OneBot v11 机器人实例。
        group_id: 群号。
        ttl: 缓存时长（秒）。

    Returns:
        ``(成员数, 群名)``；失败时成员数为 0、群名为空串。
    """
    now = time.time()
    hit = _META_CACHE.get(group_id)
    if hit and ttl > 0 and now - hit[0] < ttl:
        return hit[1], hit[2]

    members = await _fetch_member_count(bot, group_id)
    name = await _fetch_group_name(bot, group_id)
    _META_CACHE[group_id] = (now, members, name)
    return members, name


@dataclass(slots=True)
class _Dropped:
    """被「只分析文字」规则剔除的消息标记。"""

    reason: str


def _parse_message(msg: dict, self_id: int | None) -> ChatMessage | _Dropped | None:
    """把单条 OneBot 消息转成 :class:`ChatMessage`。

    **只保留纯文字**：图片不做识别也不描述，合并转发整段丢弃；
    没有正文的消息返回 :class:`_Dropped`（注明剔除原因）或 ``None``（无效消息）。
    """
    try:
        user_id = int((msg.get("sender") or {}).get("user_id") or 0)
    except (TypeError, ValueError):
        return None
    if not user_id or (self_id is not None and user_id == self_id):
        return None

    text, images, replies, emojis, forwards = _extract_text(msg)
    # 只分析文字：没有正文的消息（纯图片 / 纯合并转发 / 纯表情）不参与统计。
    # 这不是「恰好没落进分支」，而是显式规则——图片不做识别，合并转发整段丢弃。
    if not text:
        if forwards and not images:
            return _Dropped("forward")
        if images:
            return _Dropped("image")
        return None

    nickname = _sender_name(msg.get("sender") or {})
    return ChatMessage(
        message_id=int(msg.get("message_id") or 0),
        user_id=user_id,
        nickname=nickname,
        text=text,
        timestamp=int(msg.get("time") or 0),
        msg_type=str(msg.get("message_type") or "group"),
        image_count=images,
        reply_count=replies,
        emoji_types=emojis,
        forward_count=forwards,
    )


async def _fetch_member_count(bot: Bot, group_id: int) -> int:
    """获取群成员数，失败返回 0。"""
    try:
        members = await bot.get_group_member_list(group_id=group_id)
        return len(members)
    except Exception as e:
        logger.warning(f"[群日报] 获取群 {group_id} 成员列表失败: {e!s}")
        return 0


async def _fetch_group_name(bot: Bot, group_id: int) -> str:
    """获取群名称，失败返回空串。"""
    try:
        info = await bot.get_group_info(group_id=group_id)
        name = info.get("group_name")
        if isinstance(name, str):
            return name.strip()
    except Exception as e:
        logger.warning(f"[群日报] 获取群 {group_id} 信息失败: {e!s}")
    return ""

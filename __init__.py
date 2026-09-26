"""群日报插件。

- 指令：``/群日报`` 出海报、``/群日报漫画`` 出漫画、``/群日报整体`` 两样都出
- 定时：按 cron 推送到 ``gdr_group_list`` 中的群，默认连带漫画
- 海报复用 astrbot 原插件的 6 套主题模板与素材
- 漫画由独立的生图模型生成（``gdr_image_*``），与文字总结模型（``gdr_text_*``）分开配置
"""

from __future__ import annotations

import asyncio

from nonebot import get_bots, get_plugin_config, on_command, require
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.log import logger
from nonebot.plugin import PluginMetadata
from nonebot.rule import is_type

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler  # noqa: E402

from .cleanup import cleanup_record_db
from .config import Config
from .imagegen import ImageModel
from .llm import TextModel
from .pipeline import generate_daily_report

__plugin_meta__ = PluginMetadata(
    name="群日报",
    description="生成群聊日报：统计分析 + 话题总结 + 海报图片，可顺带生成漫画并定时推送。",
    usage=(
        "发送指令「/群日报」生成日报海报。\n"
        "「/群日报漫画」只生成漫画，「/群日报整体」海报和漫画一起发。\n"
        "三条指令共用同一次文字模型分析，不会为多出的产物重复调用。\n"
        "定时任务按 gdr_push_cron 推送到 gdr_group_list 中的群。\n"
        "配置全部通过 .env 中的 gdr_ 前缀变量设置。"
    ),
    type="application",
    homepage="https://github.com/local/nonebot-plugin-group-daily-report",
    config=Config,
    supported_adapters={"~onebot.v11"},
)

config = get_plugin_config(Config)
text_model = TextModel(config)
image_model = ImageModel(config)

if not text_model.available:
    logger.warning("[群日报] 未配置 gdr_text_api_keys，插件将无法生成日报")

report_cmd = on_command(
    "群日报",
    # 不注册「日报」别名：COMMAND_START 含空前缀，指令按「句首最长前缀」匹配，
    # 「日报今天真无聊」这类正常聊天会被误触发。
    aliases={"群聊日报"},
    priority=5,
    rule=is_type(GroupMessageEvent),
    block=True,
)

comic_cmd = on_command(
    "群日报漫画",
    # 三条指令必须各自 on_command，不能互相写成别名：别名在同一条匹配里
    # 竞争，「群日报」比「群日报整体」短，会先把整个前缀吃掉。
    aliases={"群聊日报漫画"},
    priority=5,
    rule=is_type(GroupMessageEvent),
    block=True,
)

full_cmd = on_command(
    "群日报整体",
    aliases={"群聊日报整体"},
    priority=5,
    rule=is_type(GroupMessageEvent),
    block=True,
)

#: 每群一条日报/漫画任务，避免连发指令把生图 API 重复调用一遍遍扣钱。
#: astrbot 用 _comic_group_tasks 做同样的事，这里用锁实现，更轻。
_GROUP_LOCKS: dict[int, asyncio.Lock] = {}


def _group_lock(group_id: int) -> asyncio.Lock:
    """取某个群的日报锁，首次见到时创建。

    Args:
        group_id: 群号。

    Returns:
        该群对应的锁。
    """
    lock = _GROUP_LOCKS.get(group_id)
    if lock is None:
        lock = _GROUP_LOCKS[group_id] = asyncio.Lock()
    return lock


async def _generate(
    bot: Bot,
    group_id: int,
    *,
    with_comic: bool,
    comic_only: bool,
) -> list[dict] | None:
    """跑一遍采集与分析，返回待发送的消息段。

    Args:
        bot: OneBot v11 机器人实例。
        group_id: 群号。
        with_comic: 是否额外产出漫画。
        comic_only: 只产出漫画，不渲染海报也不出正文。

    Returns:
        消息段列表；出错时返回 ``None``。
    """
    try:
        segments, _, _ = await generate_daily_report(
            bot,
            group_id,
            config,
            text_model=text_model,
            image_model=image_model,
            with_comic=with_comic,
            comic_only=comic_only,
            # 用户主动敲的指令才值得告诉他「为什么没漫画」；
            # 缺 key 这类问题记日志即可，别拿告警刷群
            notify_missing_comic=comic_only or with_comic,
        )
        return segments
    except Exception as e:
        logger.error(f"[群日报] 指令执行失败 (comic_only={comic_only}): {e!s}")
        return None


async def _handle(
    bot: Bot, group_id: int, *, with_comic: bool, comic_only: bool, empty: str
) -> None:
    """三条指令的公共流程：加锁生成 → 发送 → 回一句确认。

    ``with_comic`` 和 ``comic_only`` 决定了产出什么，但**三者共用同一次文字
    模型分析**——漫画提示词是从这份 analysis 的 summary/topics 派生的，所以
    「群日报整体」不会为了多出这张图而再调一次文字模型。

    **开始时不发「请稍候」**：生图 + 渲染要几十秒，中途插话会盖在群消息流里，
    反而像刷屏。改成全部发完后再补一句「群日报已发送，请查收」作为收尾。

    Args:
        bot: OneBot v11 机器人实例。
        group_id: 群号。
        with_comic: 是否额外产出漫画。
        comic_only: 只产出漫画。
        empty: 没有产出时的提示语。
    """
    if comic_only and not image_model.available:
        await bot.send_group_msg(group_id=group_id, message="未配置 gdr_image_api_keys，无法生成漫画。")
        return

    lock = _group_lock(group_id)
    if lock.locked():
        await bot.send_group_msg(group_id=group_id, message="本群已有日报任务在进行中，请稍候…")
        return

    async with lock:
        segments = await _generate(bot, group_id, with_comic=with_comic, comic_only=comic_only)
        if not segments:
            await bot.send_group_msg(group_id=group_id, message=empty)
            return
        await send_segments(bot, group_id, segments)
        await bot.send_group_msg(group_id=group_id, message="群日报已发送，请查收")


@report_cmd.handle()
async def handle_report(bot: Bot, event: GroupMessageEvent) -> None:
    """处理 ``/群日报``：只出海报。"""
    await _handle(
        bot,
        event.group_id,
        with_comic=False,
        comic_only=False,
        empty="未能生成日报，可能是群消息过少或模型调用失败。",
    )


@comic_cmd.handle()
async def handle_comic(bot: Bot, event: GroupMessageEvent) -> None:
    """处理 ``/群日报漫画``：只出漫画，不渲染海报。"""
    await _handle(
        bot,
        event.group_id,
        with_comic=True,
        comic_only=True,
        empty="未能生成漫画，可能是群消息过少或模型调用失败。",
    )


@full_cmd.handle()
async def handle_full(bot: Bot, event: GroupMessageEvent) -> None:
    """处理 ``/群日报整体``：海报 + 漫画，两样都发。"""
    await _handle(
        bot,
        event.group_id,
        with_comic=True,
        comic_only=False,
        empty="未能生成日报，可能是群消息过少或模型调用失败。",
    )


async def send_segments(bot: Bot, group_id: int, segments: list[dict]) -> None:
    """逐条发送日报内容。

    **不要用合并转发**：NapCat 在 forward 节点里发 base64 图片时经常失败，
    群里只会显示「该消息类型暂不支持查看」。图片单独发一条。

    默认只发海报、不发正文（``gdr_send_text=false``），因为海报里已经有全部
    内容，再发一遍文字是重复且刷屏。但两种例外必须保留：

    * 告警段（``notice=True``）始终发送——否则海报渲染失败时群里一条都收不到，
      会变成「插件没反应」；
    * 一张图都没渲染出来时，退回发文字版，保证日报不为空。

    Args:
        bot: OneBot v11 机器人实例。
        group_id: 群号。
        segments: 要发送的消息段。
    """
    images = [s for s in segments if s.get("type") == "image"]
    notices = "\n".join(
        str((s.get("data") or {}).get("text") or "")
        for s in segments
        if s.get("type") != "image" and s.get("notice")
    ).strip()
    body = "\n".join(
        str((s.get("data") or {}).get("text") or "")
        for s in segments
        if s.get("type") != "image" and not s.get("notice")
    ).strip()

    texts = body if (config.gdr_send_text or not images) else ""
    parts = [t for t in (notices, texts) if t]

    if parts:
        await bot.send_group_msg(group_id=group_id, message="\n".join(parts))
    elif not images:
        await bot.send_group_msg(group_id=group_id, message="（日报内容为空）")

    for i, img in enumerate(images):
        try:
            await bot.send_group_msg(group_id=group_id, message=[img])
        except Exception as e:
            logger.error(f"[群日报] 第 {i + 1} 张图片发送失败: {e!s}")
        # 多张图之间留一点间隔，避免触发发送频率限制
        if i < len(images) - 1:
            await asyncio.sleep(1)


def _parse_cron(cron: str) -> dict[str, str]:
    """把 5 段 cron 表达式转成 APScheduler 的关键字参数。"""
    fields = cron.split()
    if len(fields) != 5:
        raise ValueError(f"无效的 cron 表达式（需 5 段）: {cron}")
    minute, hour, day, month, day_of_week = fields
    return {
        "minute": minute,
        "hour": hour,
        "day": day,
        "month": month,
        "day_of_week": day_of_week,
    }


def _tzinfo(name: str):
    """把时区名转成 tzinfo，取不到时退回服务器本地时区。

    Args:
        name: 时区名，如 ``Asia/Shanghai`` 或 ``Local``。

    Returns:
        tzinfo 对象。
    """
    if not name or name.lower() == "local":
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as e:
        logger.warning(f"[群日报] 时区 {name!r} 无效，退回服务器本地时区: {e!s}")
        return None


async def _push_one(bot: Bot, group_id: int) -> None:
    """向单个群生成并推送日报（海报 + 漫画）。

    Args:
        bot: OneBot v11 机器人实例。
        group_id: 群号。
    """
    try:
        segments, stats, _ = await generate_daily_report(
            bot,
            group_id,
            config,
            text_model=text_model,
            image_model=image_model,
            with_comic=config.gdr_enable_comic,
        )
        if not segments:
            await bot.send_group_msg(
                group_id=group_id, message="日报生成失败：无可用消息或模型异常。"
            )
            return
        await send_segments(bot, group_id, segments)
        logger.info(
            f"[群日报] 已推送群 {group_id} 日报"
            f"（{stats.total_messages if stats else 0} 条消息，"
            f"风格={config.gdr_report_style}，漫画={config.gdr_enable_comic}）"
        )
    except Exception as e:
        logger.error(f"[群日报] 定时推送群 {group_id} 失败: {e!s}")


def _make_pusher(group_ids: list[int]):
    """构造一个推送指定若干群的协程。

    Args:
        group_ids: 目标群号列表。

    Returns:
        可交给 APScheduler 的异步协程。
    """

    async def _run() -> None:
        if not text_model.available:
            logger.warning("[群日报] 未配置文字模型 Key，跳过定时推送")
            return
        for bot in get_bots().values():
            if not isinstance(bot, Bot):
                continue
            for gid in group_ids:
                await _push_one(bot, gid)
                # 群之间留间隔，连续调 API 容易触发 NapCat 限流
                await asyncio.sleep(3)

    return _run


def _register_schedules() -> None:
    """按配置注册定时任务：每群独立时刻优先，其次是全局 cron。"""
    tz = _tzinfo(config.gdr_timezone)

    for item in config.gdr_group_schedules:
        if not item.enable:
            continue
        hour, minute = (int(x) for x in item.time.split(":"))
        scheduler.add_job(
            _make_pusher([item.group_id]),
            "cron",
            hour=hour,
            minute=minute,
            id=f"group_daily_report_{item.group_id}",
            replace_existing=True,
            misfire_grace_time=1800,
            timezone=tz,
        )
        logger.info(
            f"[群日报] 已注册群 {item.group_id} 定时推送："
            f"每天 {item.time}（{config.gdr_timezone}）"
        )

    # 独立时刻优先；没配才用全局 cron + 群列表，两者可共存
    if config.gdr_group_list and config.gdr_push_cron:
        scheduler.add_job(
            _make_pusher(config.gdr_group_list),
            "cron",
            **_parse_cron(config.gdr_push_cron),
            id="group_daily_report_push",
            replace_existing=True,
            misfire_grace_time=1800,
            timezone=tz,
        )
        logger.info(
            f"[群日报] 已注册全局定时推送：{config.gdr_push_cron} "
            f"→ 群 {config.gdr_group_list}（{config.gdr_timezone}）"
        )

    if not config.gdr_group_schedules and not config.gdr_group_list:
        logger.info("[群日报] 未配置任何定时推送群（gdr_group_schedules / gdr_group_list）")


_register_schedules()


if config.gdr_cleanup_enabled:
    _cleanup_kwargs = _parse_cron(config.gdr_cleanup_cron)

    @scheduler.scheduled_job(
        "cron",        id="group_daily_report_cleanup",
        replace_existing=True,
        **_cleanup_kwargs,
    )
    async def cleanup_record_store() -> None:
        """定期清理 chatrecorder 落库的旧消息，避免数据库无限膨胀。"""
        try:
            await cleanup_record_db(
                retention_days=config.gdr_retention_days,
                max_mb=config.gdr_db_max_mb,
                batch=config.gdr_cleanup_batch,
            )
        except Exception as e:
            logger.error(f"[群日报] 清理任务异常: {e!s}")

    logger.info(
        f"[群日报] 已启用落库清理：cron={config.gdr_cleanup_cron}，"
        f"保留 {config.gdr_retention_days} 天 / 上限 {config.gdr_db_max_mb}MB"
    )

# -*- coding: utf-8 -*-
"""群聊日报插件（二合一：OneBot v11 / QQ 官方机器人，自动检测）。

同一份代码，按群所属适配器自动选择通道：

- **OneBot v11**（NapCat 等）：``get_group_msg_history`` 按时间回溯采集
  （可回补离线时段），海报 base64 段直接发送；实时消息也落库兜底。
- **QQ 官方机器人**（adapter-qq）：无历史接口，全量群消息
  （GROUP_MESSAGE_CREATE）实时落库为唯一来源；海报经分片上传
  （raw_url）拼 markdown 卡片发送。

渲染通道自动选择：htmlrender 的 ``template_to_pic``（0.7.x，bison5 类
环境）→ 失败回落直连 chromium CDP（bison2 环境，``SD_CDP_HOST/PORT``）。

指令：/群日报（海报）、/群日报漫画（漫画）、/群日报整体（两样都出）。

移植自 bison5 的 ``nonebot_plugin_qq_group_daily_analysis``（OneBot 版）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from nonebot import get_bots, get_driver, get_plugin_config, on_command
from nonebot.adapters import Event
from nonebot.log import logger
from nonebot.rule import Rule

from . import store
from .comicgen import ComicImageModel
from .config import Config
from .llm import TextModel
from .pipeline import generate_daily_report
from .send import Report, send_official, send_onebot

try:  # OneBot 适配器是可选依赖
    from nonebot.adapters.onebot.v11 import Bot as Ob11Bot
    from nonebot.adapters.onebot.v11 import GroupMessageEvent as Ob11GroupEvent
except ImportError:  # pragma: no cover
    Ob11Bot = None  # type: ignore[assignment,misc]
    Ob11GroupEvent = None  # type: ignore[assignment,misc]

from nonebot.adapters.qq import Bot as QqBot  # noqa: E402
from nonebot.adapters.qq import GroupMessageCreateEvent  # noqa: E402

plugin_config = get_plugin_config(Config)
text_model = TextModel(plugin_config)
# 漫画生图客户端（创建一次；未配 Key 时 available=False，自动跳过漫画）
image_model = ComicImageModel(plugin_config)

driver = get_driver()


@driver.on_startup
async def _init_store() -> None:
    path = store.init(plugin_config.gdrq_cache_dir)
    logger.info(f"[群日报] 落库已就绪: {path}")


# 消息采集：官方 WS 全量 + OneBot 实时兜底（导入即注册）
from . import ingest as _ingest  # noqa: E402,F401
from . import ingest_ob11 as _ingest_ob11  # noqa: E402,F401


# ── Bot 查找 ───────────────────────────────────────────────────


def _find_ob11_bot() -> Any | None:
    if Ob11Bot is None:
        return None
    for b in get_bots().values():
        if isinstance(b, Ob11Bot):
            return b
    return None


def _find_qq_bot() -> QqBot | None:
    for b in get_bots().values():
        if isinstance(b, QqBot):
            return b
    return None


# ── 发送 ───────────────────────────────────────────────────────


async def deliver(
    channel: str, bot: Any, group: str | int, report: Report, msg_id: str | None = None
) -> None:
    """按通道发送日报。"""
    if channel == "onebot":
        await send_onebot(bot, int(group), report)  # type: ignore[arg-type]
    else:
        await send_official(
            bot, str(group), report, msg_id=msg_id, img_size=plugin_config.gdrq_img_size
        )


# ── 指令 ───────────────────────────────────────────────────────

_GROUP_LOCKS: dict[str, asyncio.Lock] = {}


def _group_lock(key: str) -> asyncio.Lock:
    if key not in _GROUP_LOCKS:
        _GROUP_LOCKS[key] = asyncio.Lock()
    return _GROUP_LOCKS[key]


async def _is_group_message(event: Event) -> bool:
    if GroupMessageCreateEvent is not None and isinstance(
        event, GroupMessageCreateEvent
    ):
        return True
    return Ob11GroupEvent is not None and isinstance(event, Ob11GroupEvent)


daily_cmd = on_command("群日报", rule=Rule(_is_group_message), priority=5, block=True)
# 三条指令各自独立注册，不互为别名（互为别名会在同一条匹配里互相抢）
comic_cmd = on_command("群日报漫画", rule=Rule(_is_group_message), priority=5, block=True)
full_cmd = on_command("群日报整体", rule=Rule(_is_group_message), priority=5, block=True)


@daily_cmd.handle()
async def _handle_daily(event: Event):
    await _handle_report(event, with_comic=False, comic_only=False)


@comic_cmd.handle()
async def _handle_comic(event: Event):
    await _handle_report(event, with_comic=True, comic_only=True)


@full_cmd.handle()
async def _handle_full(event: Event):
    await _handle_report(event, with_comic=True, comic_only=False)


async def _handle_report(
    event: Event, *, with_comic: bool, comic_only: bool
) -> None:
    if isinstance(event, GroupMessageCreateEvent):
        channel, group, msg_id = "qq", event.group_openid, event.id
        bot = _find_qq_bot()
    elif Ob11GroupEvent is not None and isinstance(event, Ob11GroupEvent):
        channel, group, msg_id = "onebot", event.group_id, None
        bot = _find_ob11_bot()
    else:
        return
    if bot is None:
        logger.warning("[群日报] 没有可用的 Bot 实例")
        return

    key = f"{channel}:{group}"
    lock = _group_lock(key)
    if lock.locked():
        await deliver(
            channel, bot, group,
            Report(notices=["本群已有日报任务在进行中，请稍候…"], text=None, images=[]),
            msg_id=msg_id,
        )
        return

    async with lock:
        try:
            report = await generate_daily_report(
                bot, channel, group, plugin_config, text_model,
                image_model=image_model,
                with_comic=with_comic,
                comic_only=comic_only,
                notify=True,
            )
        except RuntimeError as e:
            logger.warning(f"[群日报] {key} 生成失败: {e!s}")
            await deliver(
                channel, bot, group,
                Report(notices=[f"群日报生成失败：{e!s}"], text=None, images=[]),
                msg_id=msg_id,
            )
            return
        except Exception:
            logger.exception("[群日报] 生成异常")
            await deliver(
                channel, bot, group,
                Report(notices=["群日报生成失败，详见服务端日志。"], text=None, images=[]),
                msg_id=msg_id,
            )
            return

    if not (report.notices or report.text or report.images):
        return
    try:
        await deliver(channel, bot, group, report, msg_id=msg_id)
    except Exception:
        logger.exception(f"[群日报] {key} 发送失败")


# ── 定时推送 ───────────────────────────────────────────────────


def _make_pusher(channel: str, group: str | int):
    async def _push() -> None:
        bot = _find_ob11_bot() if channel == "onebot" else _find_qq_bot()
        if bot is None:
            logger.warning(f"[群日报] 定时推送时 {channel} 通道没有在线 Bot")
            return
        key = f"{channel}:{group}"
        lock = _group_lock(key)
        if lock.locked():
            logger.info(f"[群日报] {key} 任务进行中，跳过定时推送")
            return
        async with lock:
            try:
                report = await generate_daily_report(
                    bot, channel, group, plugin_config, text_model,
                    image_model=image_model,
                    with_comic=plugin_config.gdrq_enable_comic,
                    notify=False,
                )
            except Exception:
                logger.exception(f"[群日报] {key} 定时日报生成失败")
                return
        if report.notices or report.text or report.images:
            try:
                await deliver(channel, bot, group, report)
            except Exception:
                logger.exception(f"[群日报] {key} 定时日报发送失败")

    return _push


@driver.on_startup
async def _register_jobs() -> None:
    from nonebot_plugin_apscheduler import scheduler

    scheduled: list[str] = []
    for item in plugin_config.gdrq_group_schedules:
        if not item.enable:
            continue
        if item.group_openid:
            channel, group = "qq", item.group_openid
        elif item.group_id is not None:
            channel, group = "onebot", item.group_id
        else:
            logger.warning("[群日报] 定时配置缺少 group_openid / group_id，跳过")
            continue

        parts = (item.time or "").strip().split(":")
        if len(parts) != 2:
            logger.warning(f"[群日报] 无效时刻 {item.time!r}，跳过 {channel}:{group}")
            continue
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError:
            logger.warning(f"[群日报] 无效时刻 {item.time!r}，跳过 {channel}:{group}")
            continue

        job_kwargs: dict[str, Any] = {}
        note = ""
        if item.start_date.strip():
            try:
                start = datetime.strptime(item.start_date.strip(), "%Y-%m-%d").date()
                job_kwargs["start_date"] = datetime(
                    start.year, start.month, start.day, hour, minute
                )
                note = f"（{start} 起生效）"
            except ValueError:
                logger.warning(
                    f"[群日报] 无效 start_date {item.start_date!r}（应为 YYYY-MM-DD），"
                    f"{channel}:{group} 立即生效"
                )

        scheduler.add_job(
            _make_pusher(channel, group),
            "cron",
            hour=hour,
            minute=minute,
            id=f"qq_daily_report_{channel}_{group}",
            replace_existing=True,
            misfire_grace_time=1800,
            **job_kwargs,
        )
        scheduled.append(f"{channel}:{group}@{item.time}{note}")

    scheduler.add_job(
        store.cleanup,
        "cron",
        hour=4,
        minute=30,
        args=(plugin_config.gdrq_retention_hours, plugin_config.gdrq_cleanup_batch),
        id="qq_daily_report_cleanup",
        replace_existing=True,
    )

    logger.info(
        f"[群日报] 定时任务注册完成: 推送 {len(scheduled)} 群 "
        f"({', '.join(scheduled) if scheduled else '未配置'}), "
        f"清理 04:30（保留 {plugin_config.gdrq_retention_hours}h）"
    )

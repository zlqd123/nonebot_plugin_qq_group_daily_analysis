# -*- coding: utf-8 -*-
"""QQ 官方机器人群聊日报插件。

官方机器人没有群历史消息接口，本插件以「实时落库」为数据来源：
群内全量消息（GROUP_MESSAGE_CREATE）实时写入 SQLite（仅文本，
默认保留 72h），日报生成时查库 → LLM 结构化分析 → scrapbook 主题
海报渲染 → sd_media 分片上传 → markdown 卡片发送。

移植自 bison5 的 ``nonebot_plugin_qq_group_daily_analysis``（OneBot v11 版）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import httpx
from nonebot import get_bots, get_driver, get_plugin_config, on_command
from nonebot.adapters import Event
from nonebot.adapters.qq import Bot as QqBot
from nonebot.adapters.qq import GroupMessageCreateEvent
from nonebot.log import logger
from nonebot.rule import Rule

from . import store
from .config import Config
from .llm import TextModel
from .pipeline import Payload, generate_daily_report

plugin_config = get_plugin_config(Config)
text_model = TextModel(plugin_config)

API = "https://api.sgroup.qq.com"

driver = get_driver()


@driver.on_startup
async def _init_store() -> None:
    path = store.init(plugin_config.gdrq_cache_dir)
    logger.info(f"[群日报] 落库已就绪: {path}")


# 消息采集（导入即注册监听器）
from . import ingest as _ingest  # noqa: E402,F401


# ── 发送 ───────────────────────────────────────────────────────


async def _post_group_message(
    bot: QqBot, group_openid: str, body: dict[str, Any]
) -> None:
    """裸 HTTP 发群消息。

    不走 adapter 的 ``send_to_group``：bison_qq_patch 的实测表明，
    adapter 会强附 ``timestamp`` 字段触发 40034024，而裸 HTTP + token
    （``bot.get_access_token()`` 自带缓存续期）是已验证的稳定通道。
    """
    token = await bot.get_access_token()
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{API}/v2/groups/{group_openid}/messages",
            json=body,
            headers={"Authorization": f"QQBot {token}"},
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"群消息发送失败 {r.status_code}: {r.text[:150]}"
            )


async def send_payloads(
    bot: QqBot,
    group_openid: str,
    payloads: list[Payload],
    *,
    msg_id: str | None = None,
) -> None:
    """逐条发送日报内容段；被动回复（有 msg_id）时 msg_seq 递增。"""
    seq = 0
    for p in payloads:
        seq += 1
        if p.kind == "markdown":
            body: dict[str, Any] = {
                "msg_type": 2,
                "markdown": {"content": p.content},
            }
        else:
            body = {"msg_type": 0, "content": p.content}
        if msg_id:
            body["msg_id"] = msg_id
            body["msg_seq"] = seq
        try:
            await _post_group_message(bot, group_openid, body)
        except Exception as e:
            logger.warning(f"[群日报] 内容段发送失败: {e!s}")
        if len(payloads) > 1:
            await asyncio.sleep(1)


async def _get_qq_bot() -> QqBot | None:
    for b in get_bots().values():
        if isinstance(b, QqBot):
            return b
    return None


# ── 指令 ───────────────────────────────────────────────────────

_GROUP_LOCKS: dict[str, asyncio.Lock] = {}


def _group_lock(group_openid: str) -> asyncio.Lock:
    if group_openid not in _GROUP_LOCKS:
        _GROUP_LOCKS[group_openid] = asyncio.Lock()
    return _GROUP_LOCKS[group_openid]


async def _is_group_message(event: Event) -> bool:
    return isinstance(event, GroupMessageCreateEvent)


daily_cmd = on_command("群日报", rule=Rule(_is_group_message), priority=5, block=True)


@daily_cmd.handle()
async def _(bot: QqBot, event: GroupMessageCreateEvent):
    group_openid = event.group_openid
    lock = _group_lock(group_openid)
    if lock.locked():
        await send_payloads(
            bot,
            group_openid,
            [Payload(kind="text", content="本群已有日报任务在进行中，请稍候…")],
            msg_id=event.id,
        )
        return

    async with lock:
        try:
            result = await generate_daily_report(
                bot, group_openid, plugin_config, text_model, notify=True
            )
        except RuntimeError as e:
            logger.warning(f"[群日报] 群 {group_openid[:12]}… 生成失败: {e!s}")
            await send_payloads(
                bot,
                group_openid,
                [Payload(kind="text", content=f"群日报生成失败：{e!s}")],
                msg_id=event.id,
            )
            return
        except Exception:
            logger.exception("[群日报] 生成异常")
            await send_payloads(
                bot,
                group_openid,
                [Payload(kind="text", content="群日报生成失败，详见服务端日志。")],
                msg_id=event.id,
            )
            return

    if not result.payloads:
        return
    await send_payloads(bot, group_openid, result.payloads, msg_id=event.id)


# ── 定时推送 ───────────────────────────────────────────────────


def _make_pusher(group_openids: list[str]):
    """构造一次多群推送协程。"""

    async def _push() -> None:
        bot = await _get_qq_bot()
        if bot is None:
            logger.warning("[群日报] 定时推送时没有在线的官方 QQ 机器人")
            return
        for group_openid in group_openids:
            lock = _group_lock(group_openid)
            if lock.locked():
                logger.info(f"[群日报] 群 {group_openid[:12]}… 任务进行中，跳过定时推送")
                continue
            async with lock:
                try:
                    result = await generate_daily_report(
                        bot, group_openid, plugin_config, text_model, notify=False
                    )
                    payloads = [p for p in result.payloads if p]
                except Exception:
                    logger.exception(
                        f"[群日报] 群 {group_openid[:12]}… 定时日报生成失败"
                    )
                    payloads = []
            if payloads:
                try:
                    await send_payloads(bot, group_openid, payloads)
                except Exception:
                    logger.exception(
                        f"[群日报] 群 {group_openid[:12]}… 定时日报发送失败"
                    )
            await asyncio.sleep(3)

    return _push


@driver.on_startup
async def _register_jobs() -> None:
    from nonebot_plugin_apscheduler import scheduler

    scheduled: list[str] = []
    for item in plugin_config.gdrq_group_schedules:
        if not item.enable:
            continue
        parts = (item.time or "").strip().split(":")
        if len(parts) != 2:
            logger.warning(f"[群日报] 无效时刻 {item.time!r}，跳过 {item.group_openid[:12]}…")
            continue
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError:
            logger.warning(f"[群日报] 无效时刻 {item.time!r}，跳过 {item.group_openid[:12]}…")
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
                    f"{item.group_openid[:12]}… 立即生效"
                )
        scheduler.add_job(
            _make_pusher([item.group_openid]),
            "cron",
            hour=hour,
            minute=minute,
            id=f"qq_daily_report_{item.group_openid}",
            replace_existing=True,
            misfire_grace_time=1800,
            **job_kwargs,
        )
        scheduled.append(f"{item.group_openid[:12]}…@{item.time}{note}")

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

"""定时任务注册（APScheduler）。

移植自 nonebot 版 ``__init__.py`` 的调度逻辑：MaiBot 没有内置定时调度 API，
按官方模式在 ``requirements.txt`` 引入 ``apscheduler``，由插件自建
``AsyncIOScheduler``（on_load 启动、on_unload 关闭），cron 语义与
misfire 宽限期、提前采集前移等行为与 nonebot 版完全一致：

- **每群独立时刻优先**（``group_schedules``）；
- **全局 cron 兜底**（``push_cron`` + ``group_list``），两者可共存；
- 启用提前采集时，任务本身注册在「发送时刻 − 提前量」，任务内部再等
  到发送时刻才发；
- cron 前移必须能精确还原（笛卡尔积校验），跨天/区间/通配一律放弃前移。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Awaitable, Callable

from .log import logger
from .settings import GdrConfig


def parse_cron(cron: str) -> dict[str, str]:
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


def tzinfo(name: str):
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


def _int_list(field: str) -> set[int] | None:
    """把 cron 的 hour/minute 字段解析成整数集合。

    只认纯数字和逗号列表（``"14"`` / ``"14,22"``）。区间、通配、步长一律拒绝，
    因为它们没法和「前移若干分钟」做可靠换算。

    Args:
        field: cron 字段值。

    Returns:
        整数集合；无法解析时返回 ``None``。
    """
    parts = field.split(",")
    out: set[int] = set()
    for p in parts:
        if not p.isdigit():
            return None
        out.add(int(p))
    return out or None


def shift_cron_back(fields: dict[str, str], lead: int) -> dict[str, str] | None:
    """把 cron 的「时:分」整体前移 ``lead`` 分钟。

    APScheduler 的 hour/minute 是**独立字段**，所以 ``0 14,22 * * *`` 前移 8 分钟
    得到 ``52 13,21 * * *``，恰好是 13:52 与 21:52。

    换算后必须能用新的 minute 集合 × hour 集合**精确还原**出所有时刻，否则宁可
    放弃提前采集——把定时任务挪到错误的时间点比不挪更糟。典型放弃场景是
    ``0 0 * * *``（午夜）：前移 8 分钟应是前一天 23:52，cron 表达不了。

    Args:
        fields: :func:`parse_cron` 的结果。
        lead: 提前分钟数。

    Returns:
        前移后的 cron 字段；无法安全前移时返回 ``None``。
    """
    if lead <= 0:
        return None
    minutes = _int_list(fields["minute"])
    hours = _int_list(fields["hour"])
    if not minutes or not hours:
        return None

    original = {(m, h) for m in minutes for h in hours}
    shifted: set[tuple[int, int]] = set()
    for m, h in original:
        total = h * 60 + m - lead
        if total < 0:
            return None  # 跨到前一天，cron 表达不了
        shifted.add((total % 60, total // 60))

    new_minutes = {m for m, _ in shifted}
    new_hours = {h for _, h in shifted}
    # 还原校验：新字段的笛卡尔积必须和换算结果逐个相等
    if {(m, h) for m in new_minutes for h in new_hours} != shifted:
        return None

    out = dict(fields)
    out["minute"] = ",".join(str(m) for m in sorted(new_minutes))
    out["hour"] = ",".join(str(h) for h in sorted(new_hours))
    return out


def prefetch_lead(config: GdrConfig) -> int:
    """提前采集的分钟数，负数按 0 处理。

    Returns:
        ``fetch.prefetch_minutes``，钳制到 [0, 55] 的整数。
    """
    try:
        lead = int(config.gdr_push_prefetch_minutes)
    except (TypeError, ValueError):
        lead = 0
    return max(0, min(lead, 55))


PusherFactory = Callable[[list[int], int, list[tuple[int, int]] | None], Awaitable[None]]
"""推送协程工厂： ``(group_ids, lead, send_hours) -> 协程``。"""


def register_schedules(
    scheduler: Any,
    config: GdrConfig,
    pusher_factory: PusherFactory,
    cleanup_coro: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """按配置注册定时任务：每群独立时刻优先，其次是全局 cron。

    Args:
        scheduler: APScheduler ``AsyncIOScheduler`` 实例（须已 start）。
        config: ``gdr_*`` 扁平配置视图。
        pusher_factory: 推送协程工厂，签名 ``(group_ids, lead, send_hours)``。
        cleanup_coro: 清理协程（``cleanup.enabled`` 时按 ``cleanup.cron`` 注册）。
    """
    tz = tzinfo(config.gdr_timezone)
    lead = prefetch_lead(config)

    for item in config.gdr_group_schedules:
        if not item["enable"]:
            continue
        raw_time = str(item["time"] or "").strip()
        try:
            hour, minute = (int(x) for x in raw_time.split(":"))
        except ValueError:
            logger.warning(
                f"[群日报] 群 {item['group_id']} 的时刻 {raw_time!r} 非法，"
                f"跳过该条定时（应为 HH:MM）"
            )
            continue
        job_hour, job_minute = hour, minute
        if lead:
            total = hour * 60 + minute - lead
            if total < 0:
                logger.warning(
                    f"[群日报] 群 {item['group_id']} 的 {item['time']} 减去 {lead} 分钟"
                    f"会跨到前一天，cron 表达不了，本次不提前采集"
                )
                lead_for_this = 0
            else:
                job_hour, job_minute = total // 60, total % 60
                lead_for_this = lead
        else:
            lead_for_this = 0

        scheduler.add_job(
            pusher_factory([item["group_id"]], lead_for_this, [(hour, minute)]),
            "cron",
            hour=job_hour,
            minute=job_minute,
            id=f"group_daily_report_{item['group_id']}",
            replace_existing=True,
            misfire_grace_time=1800,
            timezone=tz,
        )
        if lead_for_this:
            logger.info(
                f"[群日报] 已注册群 {item['group_id']} 定时推送："
                f"每天 {job_hour:02d}:{job_minute:02d} 开始采集，"
                f"{item['time']} 发送（{config.gdr_timezone}）"
            )
        else:
            logger.info(
                f"[群日报] 已注册群 {item['group_id']} 定时推送："
                f"每天 {item['time']}（{config.gdr_timezone}）"
            )

    # 独立时刻优先；没配才用全局 cron + 群列表，两者可共存
    if config.gdr_group_list and config.gdr_push_cron:
        fields = parse_cron(config.gdr_push_cron)
        shifted = shift_cron_back(fields, lead) if lead else None
        if lead and shifted is None:
            logger.warning(
                f"[群日报] 全局 cron「{config.gdr_push_cron}」无法安全前移 "
                f"{lead} 分钟，本次不提前采集"
            )
            lead_for_cron = 0
        else:
            lead_for_cron = lead if shifted else 0

        scheduler.add_job(
            pusher_factory(config.gdr_group_list, lead_for_cron, None),
            "cron",
            **(shifted or fields),
            id="group_daily_report_push",
            replace_existing=True,
            misfire_grace_time=1800,
            timezone=tz,
        )
        when = (
            f"{shifted['hour']}:{shifted['minute']}（提前 {lead} 分钟采集，原 "
            f"{config.gdr_push_cron}）"
            if shifted
            else config.gdr_push_cron
        )
        logger.info(
            f"[群日报] 已注册全局定时推送：{when} "
            f"→ 群 {config.gdr_group_list}（{config.gdr_timezone}）"
        )

    if not config.gdr_group_schedules and not config.gdr_group_list:
        logger.info("[群日报] 未配置任何定时推送群（group_schedules / group_list）")

    if cleanup_coro is not None and config.gdr_cleanup_enabled:
        try:
            cleanup_fields = parse_cron(config.gdr_cleanup_cron)
        except ValueError as e:
            logger.warning(f"[群日报] 清理 cron 非法，跳过注册: {e!s}")
            return
        scheduler.add_job(
            cleanup_coro,
            "cron",
            **cleanup_fields,
            id="group_daily_report_cleanup",
            replace_existing=True,
            misfire_grace_time=1800,
            timezone=tz,
        )
        logger.info(
            f"[群日报] 已启用缓存清理：cron={config.gdr_cleanup_cron}，"
            f"保留 {config.gdr_retention_days} 天"
        )


async def wait_until(hour: int, minute: int, tz) -> None:
    """等到今天的 ``hour:minute``；已经过了就直接返回。

    Args:
        hour: 目标小时（24 小时制）。
        minute: 目标分钟。
        tz: 时区；``None`` 表示用服务器本地时间。
    """
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    wait = (target - now).total_seconds()
    if wait <= 0:
        return
    logger.info(f"[群日报] 已提前采集，等待 {wait / 60:.1f} 分钟到点发送")
    await asyncio.sleep(wait)

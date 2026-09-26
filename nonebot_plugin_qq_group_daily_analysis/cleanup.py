"""chatrecorder 落库的定期清理。

``nonebot-plugin-chatrecorder`` 本身没有任何保留策略，消息表会无限增长。
本模块在低峰期定期清理，**只删 chatrecorder 自己的表**，
绝不触碰 nonebot-bison 共用同一个 ``db.sqlite3`` 里的业务表。

清理策略（两者都可用，都为 0 时关闭）：

1. 按保留天数删除：``gdr_retention_days``
2. 按库体积删除：``gdr_db_max_mb``，超限就从最旧开始删
3. 删除后执行 ``VACUUM`` 真正归还磁盘（SQLite 删行不会自动缩小文件）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from nonebot.log import logger

# 单次清理的删除量硬顶，防止极端情况下长时间锁库
_MAX_DELETE_PER_RUN = 500_000


def find_db_file() -> Path | None:
    """定位 orm 的 sqlite 文件。

    Returns:
        数据库文件路径；orm 未启用、配置了外部数据库、或文件不存在时返回 ``None``。
    """
    # 包「装了就可导入」，但没启用时不会建表，没必要做清理
    try:
        from nonebot import get_plugin

        if get_plugin("chatrecorder") is None:
            return None
    except Exception:
        return None

    try:
        from nonebot_plugin_localstore import get_plugin_data_dir
        from nonebot_plugin_orm import plugin_config
    except Exception:
        return None

    # 显式配置了外部数据库（非 sqlite）时不处理
    url = plugin_config.sqlalchemy_database_url
    if url and "sqlite" not in str(url).lower():
        logger.debug("[群日报] orm 使用外部数据库，跳过落库清理")
        return None

    try:
        candidate = get_plugin_data_dir() / "db.sqlite3"
    except Exception:
        return None
    return candidate if candidate.exists() else None


def db_size_mb(path: Path) -> float:
    """返回数据库大小（MB），含 wal / shm 附属文件。"""
    total = sum(
        f.stat().st_size for f in (Path(f"{path}{s}") for s in ("", "-wal", "-shm")) if f.exists()
    )
    return total / 1024 / 1024


async def cleanup_record_db(
    *,
    retention_days: int = 7,
    max_mb: int = 100,
    batch: int = 5000,
) -> int:
    """执行一次清理。

    Args:
        retention_days: 消息保留天数，0 表示不按时间删。
        max_mb: 库体积上限（MB），0 表示不按体积删。
        batch: 每批删除条数，避免长事务锁库。

    Returns:
        共删除的消息条数；orm/chatrecorder 未启用或出错时返回 0。
    """
    # 这些库都在插件启用后才可用，必须惰性导入，否则本插件会加载失败
    try:
        from sqlalchemy import delete, func, not_, select, text

        from nonebot_plugin_chatrecorder.model import MessageRecord
        from nonebot_plugin_orm import get_session
        from nonebot_plugin_uninfo.orm import SessionModel, UserModel
    except ImportError as e:
        logger.debug(f"[群日报] 落库清理不可用（未启用 chatrecorder/orm）: {e!s}")
        return 0

    path = find_db_file()
    if path is None:
        return 0

    ctx = _Ctx(delete, func, not_, select, text, MessageRecord, SessionModel, UserModel)
    before = db_size_mb(path)
    total_deleted = 0
    try:
        async with get_session() as db:
            if retention_days > 0:
                cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
                    days=retention_days
                )
                total_deleted += await _delete_before(ctx, db, cutoff, batch)

            if max_mb > 0 and db_size_mb(path) > max_mb:
                total_deleted += await _shrink_to(ctx, db, path, max_mb, batch)

            if total_deleted:
                await _purge_orphans(ctx, db)
                # SQLite 删行不会缩小文件，必须 VACUUM 才真正归还磁盘
                await db.execute(text("VACUUM"))
                await db.commit()
    except Exception as e:
        logger.error(f"[群日报] 清理落库失败: {e!s}")
        return 0

    after = db_size_mb(path)
    if total_deleted:
        logger.info(
            f"[群日报] 清理完成：删除 {total_deleted} 条消息，"
            f"库体积 {before:.1f}MB → {after:.1f}MB"
        )
    else:
        logger.info(f"[群日报] 清理检查：无需删除（当前 {after:.1f}MB）")
    return total_deleted


class _Ctx:
    """把惰性导入的符号打成一包，省得层层传参。"""

    __slots__ = (
        "MessageRecord",
        "SessionModel",
        "UserModel",
        "delete",
        "func",
        "not_",
        "select",
        "text",
    )

    def __init__(self, delete, func, not_, select, text, MessageRecord, SessionModel, UserModel) -> None:
        self.delete = delete
        self.func = func
        self.not_ = not_
        self.select = select
        self.text = text
        self.MessageRecord = MessageRecord
        self.SessionModel = SessionModel
        self.UserModel = UserModel


async def _delete_before(ctx: _Ctx, db, cutoff: datetime, batch: int) -> int:
    """分批删除 ``time`` 早于 ``cutoff`` 的消息。"""
    total = 0
    while total < _MAX_DELETE_PER_RUN:
        ids = (
            (
                await db.execute(
                    ctx.select(ctx.MessageRecord.id)
                    .where(ctx.MessageRecord.time < cutoff)
                    .limit(batch)
                )
            )
            .scalars()
            .all()
        )
        if not ids:
            break
        await db.execute(ctx.delete(ctx.MessageRecord).where(ctx.MessageRecord.id.in_(ids)))
        await db.commit()
        total += len(ids)
        if len(ids) < batch:
            break
    if total >= _MAX_DELETE_PER_RUN:
        logger.warning("[群日报] 按天清理达到单次上限，剩余部分留到下次")
    return total


async def _shrink_to(ctx: _Ctx, db, path: Path, max_mb: int, batch: int) -> int:
    """库体积超限时，从最旧的消息开始删，直到降到阈值的 90%。"""
    total = 0
    target = max_mb * 0.9
    while db_size_mb(path) > target and total < _MAX_DELETE_PER_RUN:
        ids = (
            (
                await db.execute(
                    ctx.select(ctx.MessageRecord.id)
                    .order_by(ctx.MessageRecord.id.asc())
                    .limit(batch)
                )
            )
            .scalars()
            .all()
        )
        if not ids:
            logger.warning("[群日报] 消息表已空但库体积仍超限，可能存在其他大表")
            break
        await db.execute(ctx.delete(ctx.MessageRecord).where(ctx.MessageRecord.id.in_(ids)))
        await db.commit()
        total += len(ids)
    if total >= _MAX_DELETE_PER_RUN:
        logger.warning("[群日报] 按体积清理达到单次上限，剩余部分留到下次")
    return total


async def _purge_orphans(ctx: _Ctx, db) -> None:
    """删除不再被任何消息引用的 uninfo 会话与用户记录。"""
    kept_sessions = ctx.select(ctx.func.distinct(ctx.MessageRecord.session_persist_id))
    res = await db.execute(
        ctx.delete(ctx.SessionModel).where(
            ctx.not_(ctx.SessionModel.id.in_(kept_sessions))
        )
    )
    sessions = res.rowcount or 0
    await db.commit()

    kept_users = ctx.select(ctx.func.distinct(ctx.SessionModel.user_persist_id))
    res = await db.execute(
        ctx.delete(ctx.UserModel).where(ctx.not_(ctx.UserModel.id.in_(kept_users)))
    )
    users = res.rowcount or 0
    await db.commit()

    if sessions or users:
        logger.info(f"[群日报] 清理孤儿会话 {sessions} 条、孤儿用户 {users} 条")

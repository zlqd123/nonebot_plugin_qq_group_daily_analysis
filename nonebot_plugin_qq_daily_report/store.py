# -*- coding: utf-8 -*-
"""聊天记录落库：SQLite，仅文本，保留时长由 ``gdrq_retention_hours`` 控制。

官方机器人没有群历史消息接口，这里是唯一的数据来源——机器人在线期间
通过 WS 推送收到的消息。库文件放在 ``/app/cache/qq_daily_report/``。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import aiosqlite
from nonebot.log import logger

from .config import Config
from .archive import ChatMessage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    group_openid  TEXT NOT NULL,
    member_openid TEXT NOT NULL,
    nickname      TEXT NOT NULL DEFAULT '',
    text          TEXT NOT NULL,
    emoji_count   INTEGER NOT NULL DEFAULT 0,
    ts            INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_group_ts
    ON messages(group_openid, ts);
CREATE TABLE IF NOT EXISTS members (
    group_openid  TEXT NOT NULL,
    member_openid TEXT NOT NULL,
    username      TEXT NOT NULL,
    updated_at    INTEGER NOT NULL,
    PRIMARY KEY (group_openid, member_openid)
);
CREATE TABLE IF NOT EXISTS groups (
    openid       TEXT PRIMARY KEY,
    group_name   TEXT NOT NULL DEFAULT '',
    member_count INTEGER NOT NULL DEFAULT 0,
    updated_at   INTEGER NOT NULL DEFAULT 0
);
"""


def _migrate(conn: Any) -> None:
    """升级旧库：补 source 列（'qq' = 官方机器人实时落库，
    'onebot' = OneBot 实时落库；历史接口采集不落库）。"""
    cursor = conn.execute("PRAGMA table_info(messages)")
    cols = {row[1] for row in cursor.fetchall()}
    if "source" not in cols:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN source TEXT NOT NULL DEFAULT 'qq'"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_source_ts ON messages(source, ts)")

_db_path: Path | None = None
_lock = asyncio.Lock()


def init(cache_dir: str) -> Path:
    """初始化库路径并建表（同步 sqlite3 建一次即可，之后全部 aiosqlite）。"""
    global _db_path
    d = Path(cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    _db_path = d / "messages.db"
    import sqlite3

    with sqlite3.connect(str(_db_path)) as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)
    return _db_path


def _require_path() -> Path:
    if _db_path is None:
        raise RuntimeError("store 未初始化：请先调用 store.init()")
    return _db_path


async def insert_message(
    group_openid: str,
    member_openid: str,
    nickname: str,
    text: str,
    emoji_count: int,
    ts: int,
    source: str = "qq",
) -> None:
    """写入一条消息。source: 'qq' 官方机器人 / 'onebot' OneBot 实时落库。"""
    async with _lock:
        async with aiosqlite.connect(str(_require_path())) as db:
            await db.execute(
                "INSERT INTO messages(group_openid, member_openid, nickname,"
                " text, emoji_count, ts, source) VALUES (?,?,?,?,?,?,?)",
                (group_openid, member_openid, nickname, text, emoji_count, ts, source),
            )
            await db.commit()


async def fetch_messages(
    group_openid: str,
    start_ts: int,
    end_ts: int,
    limit: int = 20000,
    source: str = "qq",
) -> list[ChatMessage]:
    """取窗口内的消息，按时间升序。"""
    async with aiosqlite.connect(str(_require_path())) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT member_openid, nickname, text, emoji_count, ts"
            " FROM messages WHERE group_openid = ? AND source = ?"
            " AND ts BETWEEN ? AND ?"
            " ORDER BY ts ASC LIMIT ?",
            (group_openid, source, start_ts, end_ts, limit),
        )
        rows = await cursor.fetchall()
    return [
        ChatMessage(
            member_openid=r["member_openid"],
            nickname=r["nickname"],
            text=r["text"],
            emoji_count=int(r["emoji_count"]),
            timestamp=int(r["ts"]),
        )
        for r in rows
    ]


async def count_messages(
    group_openid: str, start_ts: int, end_ts: int, source: str = "qq"
) -> int:
    """窗口内消息总数（含空昵称等，用于数据量判断）。"""
    async with aiosqlite.connect(str(_require_path())) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM messages"
            " WHERE group_openid = ? AND source = ? AND ts BETWEEN ? AND ?",
            (group_openid, source, start_ts, end_ts),
        )
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


# ── 成员昵称缓存 ────────────────────────────────────────────────


async def load_members(group_openid: str) -> dict[str, str]:
    """取该群已缓存的 openid → 昵称。"""
    async with aiosqlite.connect(str(_require_path())) as db:
        cursor = await db.execute(
            "SELECT member_openid, username FROM members WHERE group_openid = ?",
            (group_openid,),
        )
        rows = await cursor.fetchall()
    return {r[0]: r[1] for r in rows if r[1]}


async def save_member(group_openid: str, member_openid: str, username: str) -> None:
    """缓存一个成员昵称。"""
    if not username:
        return
    async with _lock:
        async with aiosqlite.connect(str(_require_path())) as db:
            await db.execute(
                "INSERT INTO members(group_openid, member_openid, username,"
                " updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(group_openid, member_openid)"
                " DO UPDATE SET username = excluded.username,"
                " updated_at = excluded.updated_at",
                (group_openid, member_openid, username, int(time.time())),
            )
            await db.commit()


async def backfill_nicknames(group_openid: str, source: str = "qq") -> int:
    """用成员缓存回填历史消息里的空昵称，返回更新的行数。"""
    async with _lock:
        async with aiosqlite.connect(str(_require_path())) as db:
            cursor = await db.execute(
                "UPDATE messages SET nickname = ("
                "  SELECT m.username FROM members m"
                "  WHERE m.group_openid = messages.group_openid"
                "    AND m.member_openid = messages.member_openid)"
                " WHERE group_openid = ? AND source = ? AND nickname = ''"
                "   AND EXISTS (SELECT 1 FROM members m"
                "              WHERE m.group_openid = messages.group_openid"
                "                AND m.member_openid = messages.member_openid)",
                (group_openid, source),
            )
            await db.commit()
            return cursor.rowcount or 0


# ── 群元信息缓存（群名/成员数）────────────────────────────────


async def load_group_meta(group_openid: str, ttl: int) -> dict[str, Any] | None:
    """取缓存的群名与成员数，过期或缺失返回 ``None``。"""
    async with aiosqlite.connect(str(_require_path())) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT group_name, member_count, updated_at FROM groups WHERE openid = ?",
            (group_openid,),
        )
        row = await cursor.fetchone()
    if not row or ttl <= 0 or time.time() - row["updated_at"] >= ttl:
        return None
    return {"group_name": row["group_name"], "member_count": row["member_count"]}


async def save_group_meta(group_openid: str, group_name: str, member_count: int) -> None:
    """写入群元信息缓存。"""
    async with _lock:
        async with aiosqlite.connect(str(_require_path())) as db:
            await db.execute(
                "INSERT INTO groups(openid, group_name, member_count, updated_at)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(openid) DO UPDATE SET group_name = excluded.group_name,"
                " member_count = excluded.member_count,"
                " updated_at = excluded.updated_at",
                (group_openid, group_name, member_count, int(time.time())),
            )
            await db.commit()


# ── 清理 ───────────────────────────────────────────────────────


async def cleanup(retention_hours: int, batch: int) -> int:
    """删除超过保留时长的消息，分批执行，返回删除总数。"""
    if retention_hours <= 0:
        return 0
    cutoff = int(time.time()) - retention_hours * 3600
    total = 0
    async with _lock:
        async with aiosqlite.connect(str(_require_path())) as db:
            while True:
                cursor = await db.execute(
                    "DELETE FROM messages WHERE id IN ("
                    "  SELECT id FROM messages WHERE ts < ? LIMIT ?)",
                    (cutoff, batch),
                )
                await db.commit()
                removed = cursor.rowcount or 0
                total += removed
                if removed < batch:
                    break
            await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await db.commit()
    if total:
        logger.info(f"[群日报] 已清理 {total} 条过期消息（保留 {retention_hours}h）")
    return total

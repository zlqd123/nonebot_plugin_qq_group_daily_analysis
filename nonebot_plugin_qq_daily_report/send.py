# -*- coding: utf-8 -*-
"""双通道发送：按群所属适配器选择 OneBot 或 QQ 官方机器人。

- OneBot：文字合并一条，图片逐张 base64 段发送（不走合并转发）
- 官方：文字裸 HTTP（绕 adapter timestamp 坑），图片分片上传后拼
  markdown 卡片（首图带 ``# 群日报`` 标题）
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
from nonebot.log import logger

API = "https://api.sgroup.qq.com"
AUTH = "https://bots.qq.com/app/getAppAccessToken"


@dataclass(slots=True)
class Notice:
    """告警信息：任何通道都始终发送。"""

    text: str


@dataclass(slots=True)
class Report:
    """一次日报生成的产物（通道无关）。"""

    notices: list[str]
    text: str | None
    """纯文字版（渲染失败兜底 / send_text 标记开启时发送）。"""
    images: list[bytes]
    """海报 PNG 字节列表（长日报在上，漫画等依次排下）。"""
    send_text: bool = False
    """有图时是否仍额外发文字版（gdrq_send_text）。"""


# ── 官方机器人通道 ─────────────────────────────────────────────


async def _post_group_message(token: str, group_openid: str, body: dict) -> None:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{API}/v2/groups/{group_openid}/messages",
            json=body,
            headers={"Authorization": f"QQBot {token}"},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"群消息发送失败 {r.status_code}: {r.text[:150]}")


async def _get_token(bot) -> str:
    return await bot.get_access_token()


async def send_official(
    bot, group_openid: str, report: Report, *, msg_id: str | None = None,
    img_size: str = "#600px #600px",
) -> None:
    """官方机器人发送：图片分片上传 → markdown；文字裸 HTTP。"""
    from .upload import upload_bytes

    token = await _get_token(bot)
    seq = 0

    for notice in report.notices:
        seq += 1
        body = {"msg_type": 0, "content": notice}
        if msg_id:
            body["msg_id"] = msg_id
            body["msg_seq"] = seq
        await _post_group_message(token, group_openid, body)
        await asyncio.sleep(1)

    urls: list[str] = []
    for i, data in enumerate(report.images, 1):
        url = await upload_bytes(token, data, f"daily_report_{i}.png")
        if url:
            urls.append(url)
        else:
            logger.warning(f"[群日报] 第 {i} 张海报上传失败")

    if urls:
        seq += 1
        parts = [f"![t {img_size}]({u})" for u in urls]
        body = {"msg_type": 2, "markdown": {"content": "# 群日报\n\n" + "\n\n".join(parts)}}
        if msg_id:
            body["msg_id"] = msg_id
            body["msg_seq"] = seq
        await _post_group_message(token, group_openid, body)
        if report.text and report.send_text:
            seq += 1
            await _post_group_message(
                token, group_openid, {"msg_type": 0, "content": report.text}
            )
    elif report.text:
        # 海报全灭：文字版兜底
        seq += 1
        body = {"msg_type": 0, "content": report.text}
        if msg_id:
            body["msg_id"] = msg_id
            body["msg_seq"] = seq
        await _post_group_message(token, group_openid, body)
    elif not report.notices:
        raise RuntimeError("无可发送内容（渲染失败且无文字版）")


# ── OneBot 通道 ───────────────────────────────────────────────


async def send_onebot(bot, group_id: int, report: Report) -> None:
    """OneBot 发送：文字合并一条，图片逐张 base64 段。"""
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    for notice in report.notices:
        await bot.send_group_msg(group_id=group_id, message=Message(notice.text))
        await asyncio.sleep(1)

    if report.images:
        for data in report.images:
            await bot.send_group_msg(
                group_id=group_id, message=Message(MessageSegment.image(data))
            )
            await asyncio.sleep(1)
        if report.text and report.send_text:
            await bot.send_group_msg(group_id=group_id, message=Message(report.text))
    elif report.text:
        await bot.send_group_msg(group_id=group_id, message=Message(report.text))
    elif not report.notices:
        raise RuntimeError("无可发送内容（渲染失败且无文字版）")

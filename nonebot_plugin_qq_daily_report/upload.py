# -*- coding: utf-8 -*-
"""QQ 官方机器人图片分片上传。

优先复用宿主环境已有的 ``extra_plugins.nonebot_plugin_bison_qqbot.sd_media``（bison2 部署，
共享其 raw_url 缓存与锚点群配置）；不可用时用内置实现，行为一致：
``upload_prepare``（三哈希）→ 逐片 PUT → ``upload_part_finish`` →
``files`` 合并，拿 ``raw_url``（有效期 1 天）并按 md5 缓存。
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import time

import httpx
from nonebot.log import logger

API = "https://api.sgroup.qq.com"
AUTH = "https://bots.qq.com/app/getAppAccessToken"
#: 分片通道硬限 200MB，留余量
MAX_BYTES = 190 * 1024 * 1024

_CACHE_FILE = pathlib.Path(
    os.environ.get("GDRQ_CACHE_DIR", "/app/extra_plugins/nonebot_plugin_qq_daily_report/cache")
) / "upload_cache.json"
def _groups_file() -> pathlib.Path:
    """群册文件路径（懒加载：避免在插件装载期触发目标插件包的 __init__）。"""
    from extra_plugins.nonebot_plugin_bison_qqbot.qqbot_paths import GROUPS_FILE
    return GROUPS_FILE


def _load_cache() -> dict:
    try:
        return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(d: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[群日报] 上传缓存写入失败: {e}")


def _anchor_group() -> str:
    """分片上传的锚点群（raw_url 跨群可用）：SD_UPLOAD_GROUP 优先。"""
    gid = os.environ.get("SD_UPLOAD_GROUP", "").strip()
    if gid:
        return gid
    try:
        groups = json.loads(_groups_file().read_text(encoding="utf-8"))
        if groups:
            return next(iter(groups))
    except Exception:
        pass
    return ""


def _ctype(data: bytes) -> tuple[str, str]:
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "gif"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    return "image/jpeg", "jpg"


async def is_alive(token: str, url: str) -> bool:
    """探测 raw_url 是否仍可用（COS 预签名 URL 带 ttl）。"""
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url, headers={"Range": "bytes=0-1"})
            return r.status_code < 400
    except Exception:
        return False


async def _upload_slices(token: str, data: bytes, name: str, ctype: str) -> str:
    """分片上传，返回 raw_url；失败返回空串。"""
    gid = _anchor_group()
    if not gid:
        logger.warning("[群日报] 无锚点群（SD_UPLOAD_GROUP），无法分片上传")
        return ""
    headers = {"Authorization": f"QQBot {token}"}
    body = {
        "file_type": 1,
        "file_name": name,
        "file_size": len(data),
        "md5": hashlib.md5(data).hexdigest(),
        "sha1": hashlib.sha1(data).hexdigest(),
        # 缺了它服务端退回整文件一片模式且上限掉到 20MB
        "md5_10m": hashlib.md5(data[: 10 * 1024 * 1024]).hexdigest(),
    }
    try:
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(f"{API}/v2/groups/{gid}/upload_prepare",
                             json=body, headers=headers)
            if r.status_code >= 400:
                logger.warning(f"[群日报] upload_prepare {r.status_code} {r.text[:120]}")
                return ""
            prep = r.json()
            upload_id = prep["upload_id"]
            parts = prep.get("parts") or []
            block = int(prep.get("block_size") or len(data))

            for part in parts:
                chunk = data[part["index"] * block:(part["index"] + 1) * block]
                put = await c.put(part["presigned_url"], content=chunk,
                                  headers={"Content-Type": ctype})
                if put.status_code >= 400:
                    logger.warning(f"[群日报] PUT part{part['index']} {put.status_code}")
                    return ""
                fin = await c.post(
                    f"{API}/v2/groups/{gid}/upload_part_finish",
                    json={"upload_id": upload_id, "part_index": part["index"],
                          "block_size": len(chunk),
                          "md5": hashlib.md5(chunk).hexdigest()},
                    headers=headers,
                )
                if fin.status_code >= 400:
                    logger.warning(f"[群日报] part_finish {fin.status_code}")
                    return ""

            done = await c.post(
                f"{API}/v2/groups/{gid}/files",
                json={"file_type": 1, "file_name": name,
                      "upload_id": upload_id, "srv_send_msg": False},
                headers=headers,
            )
            if done.status_code >= 400:
                logger.warning(f"[群日报] files {done.status_code} {done.text[:120]}")
                return ""
            url = done.json().get("raw_url") or ""
            logger.info(f"[群日报] 分片上传 {len(data)/1048576:.2f}MB（{len(parts)} 片）")
            return url
    except Exception as e:
        logger.warning(f"[群日报] 分片上传异常: {e!s}")
        return ""


async def upload_bytes(token: str, data: bytes, name: str = "img.jpg") -> str:
    """上传图片字节，返回可显示的 raw_url（优先复用 sd_media 与其缓存）。"""
    if not data:
        return ""
    if len(data) > MAX_BYTES:
        logger.warning(f"[群日报] 图 {len(data)/1048576:.1f}MB 超过分片通道上限")
        return ""

    try:  # bison2 部署：复用 sd_media（共享锚点群与素材缓存）
        from extra_plugins.nonebot_plugin_bison_qqbot.sd_media import upload_bytes as _sd_upload

        return await _sd_upload(token, data, name)
    except Exception:
        pass

    md5 = hashlib.md5(data).hexdigest()
    cache = _load_cache()
    if md5 in cache and await is_alive(token, cache[md5]["url"]):
        return cache[md5]["url"]

    ctype, ext = _ctype(data)
    if "." not in name:
        name = f"img.{ext}"
    url = await _upload_slices(token, data, name, ctype)
    if not url:
        return ""
    cache[md5] = {"url": url, "ts": time.time(), "size": len(data)}
    _save_cache(cache)
    return url

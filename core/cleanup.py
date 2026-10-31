"""插件数据目录的定期清理。

nonebot 版清理的是 chatrecorder 落库的 SQLite 消息表；MaiBot 插件无权
（也不应该）清理宿主的消息数据库，所以本模块改为清理**插件自己的
数据目录**：渲染过程中的临时 HTML/图片、按天留存的历史海报缓存等。

清理策略：

1. 按保留天数删除：``retention_days`` 天内未修改的文件；
2. 根目录本身的 ``sensitive_lexicon.txt``、``config.toml`` 等配置/词库
   文件永不删除，只清 ``cache/`` 子目录下的内容。

删除后无需 VACUUM——没有数据库，删文件就是归还磁盘。
"""

from __future__ import annotations

import time
from pathlib import Path

from .log import logger

# 单次清理的删除量硬顶，防止极端情况卡住事件循环
_MAX_DELETE_PER_RUN = 50_000

#: 永不清理的文件名（相对 data_dir 根）
_PROTECTED_NAMES = frozenset({"sensitive_lexicon.txt", "config.toml"})


def cleanup_data_dir(data_dir: Path, *, retention_days: int = 7) -> int:
    """执行一次数据目录清理（同步文件操作，量级小、可接受）。

    Args:
        data_dir: 插件数据目录（``ctx.paths.data_dir``）。
        retention_days: 文件保留天数，0 表示不清理。

    Returns:
        删除的文件数。
    """
    if retention_days <= 0:
        logger.info("[群日报] 清理检查：retention_days=0，跳过")
        return 0

    root = Path(data_dir)
    if not root.exists():
        return 0

    cutoff = time.time() - retention_days * 86400
    deleted = 0
    # 只清「可清理」范围：cache/ 子目录，以及根目录下超过保留期的
    # 临时产物（report_*.html / *.png），词库与配置文件永不删除。
    candidates: list[Path] = list((root / "cache").rglob("*")) if (root / "cache").exists() else []
    candidates.extend(root.glob("report_*.html"))
    candidates.extend(root.glob("report_*.png"))
    candidates.extend(root.glob("*.tmp"))

    for path in candidates:
        if deleted >= _MAX_DELETE_PER_RUN:
            logger.warning("[群日报] 清理达到单次上限，剩余部分留到下次")
            break
        try:
            if not path.is_file():
                continue
            if path.name in _PROTECTED_NAMES:
                continue
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            deleted += 1
        except OSError as e:
            logger.warning(f"[群日报] 清理文件失败 {path}: {e!s}")

    # 顺带清掉 cache/ 下的空目录
    cache_dir = root / "cache"
    if cache_dir.exists():
        for sub in sorted(cache_dir.rglob("*"), reverse=True):
            try:
                if sub.is_dir() and not any(sub.iterdir()):
                    sub.rmdir()
            except OSError:
                pass

    if deleted:
        logger.info(
            f"[群日报] 清理完成：删除 {deleted} 个超过 {retention_days} 天的缓存文件"
        )
    else:
        logger.info(f"[群日报] 清理检查：无过期文件（保留 {retention_days} 天）")
    return deleted

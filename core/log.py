"""插件统一 logger。

移植自 nonebot 版的 ``from nonebot.log import logger``：MaiBot 插件跑在
独立 Runner 子进程里，用标准库 logging 即可，日志经 Runner 汇聚到宿主。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("maibot.plugin.群日报")

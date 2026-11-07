# -*- coding: utf-8 -*-
"""插件配置，全部走 .env 的 ``GDRQ_`` 前缀。

移植自 bison5 的 ``nonebot_plugin_qq_group_daily_analysis``（gdr_ 前缀），
砍掉了历史消息翻页、漫画生图、敏感词防护等与官方机器人无关的配置。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class GroupSchedule(BaseModel):
    """单个群的定时推送配置（二选一：group_id = OneBot 群号，
    group_openid = QQ 官方机器人群 openid）。"""

    group_openid: str = ""
    """官方机器人视角的群 openid。"""

    group_id: int | None = None
    """OneBot 视角的群号。"""

    time: str = "21:00"
    """24 小时制 HH:MM。"""

    enable: bool = True
    """临时停用而不删配置。"""

    start_date: str = ""
    """生效起始日期（YYYY-MM-DD，含当天）。留空 = 立即生效。
    用于"数据从某天才开始攒，之前的推送没意义"的场景：
    设为开始攒数据的那天，之前的定时触发自动跳过。"""


class Config(BaseModel, extra="ignore"):
    # ── 文字总结模型（OpenAI 兼容 / Gemini 双协议）────────────────
    gdrq_text_base_url: str = "https://token-plan-cn.xiaomimimo.com/v1"
    gdrq_text_api_keys: list[str] = Field(default_factory=list)
    gdrq_text_model: str = "mimo-v2.6-flash"
    gdrq_text_proxy: str = ""
    gdrq_text_timeout: int = 300
    gdrq_max_tokens: int = 0
    """输出上限，0 = 不发送该字段由服务端决定。"""

    # ── 分析参数 ────────────────────────────────────────────────
    gdrq_max_topics: int = 12
    gdrq_max_quotes: int = 8
    gdrq_enable_quotes: bool = True
    gdrq_max_titles: int = 5
    gdrq_enable_quality: bool = True

    # ── 窗口与阈值 ──────────────────────────────────────────────
    gdrq_window: str = "today"
    """today / 24h / yesterday / hours:N。"""
    gdrq_window_min_messages: int = 20
    """少于该数视为数据不足，跳过日报。"""
    gdrq_max_messages: int = 4000
    """送模型的消息条数上限（取最近 N 条）。"""

    # ── 输出 ───────────────────────────────────────────────────
    gdrq_report_width: int = 900
    gdrq_report_style: str = "scrapbook"
    """海报主题：scrapbook / atri。"""
    gdrq_render_backend: str = "auto"
    """auto：优先 htmlrender 的 template_to_pic（旧版 0.7.x 可用），
    失败回落直连 chromium CDP（SD_CDP_HOST/PORT）；cdp：强制 CDP；
    htmlrender：强制 htmlrender。"""
    gdrq_split_report: bool = False
    """海报过长时分上下两段发送（各自独立分片上传）。"""
    gdrq_send_text: bool = False
    """出海报时是否额外发一条文字版。"""
    gdrq_img_size: str = "#600px #600px"
    """markdown 图片的尺寸参数（客户端会压成缩略图，点击看原图）。"""

    # ── 提示词语气 ──────────────────────────────────────────────
    gdrq_prompt_style: str = "roast"
    """roast / warm / neutral / custom。"""
    gdrq_prompt_override: str = ""

    # ── 定时推送 ────────────────────────────────────────────────
    gdrq_group_schedules: list[GroupSchedule] = Field(default_factory=list)

    # ── 落库 ────────────────────────────────────────────────────
    gdrq_cache_dir: str = "/app/cache/qq_daily_report"
    gdrq_retention_hours: int = 72
    """消息保留时长，超过即被清理任务删除。"""
    gdrq_cleanup_batch: int = 5000
    """清理任务每批删除条数，避免长事务锁库。"""

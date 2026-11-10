# -*- coding: utf-8 -*-
"""插件配置，全部走 .env 的 ``GDRQ_`` 前缀。

移植自 bison5 的 ``nonebot_plugin_qq_group_daily_analysis``（gdr_ 前缀），
砍掉了历史消息翻页与敏感词防护等与官方机器人无关的配置；
漫画生图（``GDRQ_IMAGE_*``）已移植回来。
"""

from __future__ import annotations

from typing import Literal

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
    gdrq_text_thinking: str = ""
    """思维链开关（mimo 等 OpenAI 兼容接口的 ``thinking`` 参数）：
    空 = 不发送该字段（走模型默认）；``enabled`` / ``disabled`` = 显式指定。
    注意思考模式下 reasoning_content 会作为正文回落来源。"""

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

    # ── 生图模型（漫画）───────────────────────────────────
    # 默认按火山方舟 Seedream 配（provider=doubao）。换服务商时改
    # gdrq_image_provider 并调整 base_url / model / size。
    gdrq_image_provider: Literal["doubao", "images", "google", "chat", "grok"] = "doubao"
    """生图接口协议：doubao=火山方舟 Seedream / images=OpenAI 兼容
    images/generations / google=Gemini / chat=OpenAI 兼容 chat/completions
    多模态生图 / grok=x.ai。"""
    gdrq_image_base_url: str = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
    """生图接口地址，写到 /v1 或完整 .../images/generations 都认。"""
    gdrq_image_api_keys: list[str] = Field(default_factory=list)
    """生图模型 API Key 列表（429 时自动轮换）。"""
    gdrq_image_model: str = "doubao-seedream-5-0-flash-260915"
    gdrq_image_proxy: str = ""
    gdrq_image_timeout: int = 300
    gdrq_image_size_mode: Literal["native", "upscale"] = "native"
    """native=把 gdrq_image_size 原样发给接口（默认）；upscale=先按
    gdrq_image_native_sizes 请求再本地放大（适合 OpenAI 等有原生上限的服务）。"""
    gdrq_image_native_sizes: str = "1024x1024,1024x1536,1536x1024,1792x1024,1024x1792"
    gdrq_image_size: str = "2048x2048"
    """出图尺寸，原样透传给接口。默认 2048x2048（1:1）。
    Seedream 也可用档位 1K / 1.5K / 2K。"""
    gdrq_image_quality: str = ""
    """OpenAI 独有质量档位 low/medium/high/auto，其他服务商留空。"""
    gdrq_image_response_format: Literal["", "url", "b64_json"] = "b64_json"
    gdrq_image_output_format: Literal["", "png", "jpeg"] = ""
    gdrq_image_watermark: bool | None = None
    """是否加水印（Seedream 支持），None=不发送该字段。"""
    gdrq_image_extra: dict = Field(default_factory=dict)
    """追加的原始请求字段，会覆盖上面同名字段，用于适配新参数。"""
    gdrq_comic_prompt: str = ""
    """漫画画面风格提示词（如「日式漫画风格，干净线稿」），追加在分镜规范后。"""
    gdrq_image_aspect_ratio: str = "16:9"
    """google / chat 协议使用；images/doubao 用 gdrq_image_size 控制比例。"""
    gdrq_image_optimize_prompt: Literal["", "off", "fast", "standard"] = ""
    """Seedream 服务端提示词优化：留空=服务端默认；off=显式关闭
    （漫画内容跑偏时可尝试关闭）。"""
    gdrq_image_retries: int = 1
    """生图失败后的重试次数（网络/内容异常均计），0=不重试。"""
    gdrq_image_retry_delay: float = 2.0

    # ── 定时推送 ────────────────────────────────────────────────
    gdrq_group_schedules: list[GroupSchedule] = Field(default_factory=list)
    gdrq_enable_comic: bool = True
    """定时推送日报时是否顺带生成漫画。只管定时；
    「群日报漫画」「群日报整体」是手动指令，不受这个开关影响。"""

    # ── 落库 ────────────────────────────────────────────────────
    gdrq_cache_dir: str = "/app/extra_plugins/nonebot_plugin_qq_daily_report/cache"
    gdrq_retention_hours: int = 72
    """消息保留时长，超过即被清理任务删除。"""
    gdrq_cleanup_batch: int = 5000
    """清理任务每批删除条数，避免长事务锁库。"""

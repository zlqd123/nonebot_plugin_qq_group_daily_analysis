"""群日报插件配置。

配置全部来自 nonebot-bison 的 `.env` / `bot.py` 环境变量，不提供 HTTP 配置页。
文字总结模型与生图模型完全分离，各自拥有独立的 base_url / key / model / proxy。
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Extra, Field, field_validator


class GroupSchedule(BaseModel):
    """单个群的定时推送设置。"""

    group_id: int = Field(description="群号")
    time: str = Field(description="推送时刻，24 小时制「HH:MM」，按 gdr_timezone 时区")
    enable: bool = Field(default=True, description="是否启用该条定时")

    @field_validator("time")
    @classmethod
    def _check_time(cls, v: str) -> str:
        """校验并规整时刻字符串。

        Args:
            v: 原始字符串。

        Returns:
            补零后的 ``HH:MM``。

        Raises:
            ValueError: 格式非法。
        """
        s = str(v).strip()
        if not s:
            raise ValueError("time 不能为空")
        parts = s.split(":")
        if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
            raise ValueError(f"time 必须形如 09:30，收到 {v!r}")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"time 超出范围，收到 {v!r}")
        return f"{hour:02d}:{minute:02d}"


class Config(BaseModel, extra=Extra.ignore):
    """插件配置。"""

    # ── 采集 ──────────────────────────────────────────────
    gdr_source: Literal["auto", "chatrecorder", "api"] = Field(
        default="auto",
        description=(
            "消息来源：chatrecorder=读 nonebot-plugin-chatrecorder 落库（零 API 调用，推荐）；"
            "api=调 get_group_msg_history（有风控风险）；auto=优先读库、库空时回落 API"
        ),
    )
    gdr_history_lens: int = Field(
        default=20000, description="条数安全上限；真正的终止条件是回溯够时间窗口"
    )
    gdr_fetch_page_size: int = Field(
        default=5000,
        description=(
            "单次请求条数。实测 NapCat 基本不截断 count：5000 条可回溯 72 小时，"
            "一次拉满即可覆盖 24 小时日报，无需翻页"
        ),
    )
    gdr_fetch_max_pages: int = Field(
        default=1,
        description=(
            "最大翻页数。NapCat 的 message_seq 游标返回的是该点之后的消息而非之前，"
            "翻页拿不到更早的数据；且连续快速调用会触发限流反而拿更少，保持 1 即可"
        ),
    )
    gdr_fetch_interval: float = Field(
        default=5.0, description="api 模式两次翻页之间的间隔秒数，默认 5 秒以规避风控"
    )
    gdr_member_cache_ttl: int = Field(default=21600, description="群成员数缓存时长（秒），默认 6 小时")
    gdr_max_messages: int = Field(default=4000, description="送入模型的聊天记录条数上限")
    gdr_window: str = Field(
        default="today",
        description=(
            "日报覆盖的时间范围：today=今天0点至今 / 24h=最近24小时 / "
            "yesterday=昨日全天 / hours:N=最近N小时"
        ),
    )
    gdr_window_min_messages: int = Field(
        default=20,
        description="窗口内消息少于该数时视为数据不足（如凌晨运行），直接提示",
    )

    # ── 文字总结模型 ──────────────────────────────────────
    gdr_text_base_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta",
        description="文字总结模型 base_url，兼容 OpenAI / Gemini 两种协议",
    )
    gdr_text_api_keys: List[str] = Field(default=[], description="文字模型 API Key 列表，429 时自动轮换")
    gdr_text_model: str = Field(default="gemini-flash-latest", description="文字总结模型名")
    gdr_text_proxy: str = Field(default="", description="文字模型代理，形如 http://ip:port")
    gdr_text_timeout: int = Field(default=300, description="文字模型请求超时（秒）")
    gdr_text_thinking: Optional[bool] = Field(
        default=None, description="思考模式：None=不传，True=开，False=显式关闭"
    )
    gdr_max_tokens: int = Field(default=5000, description="总结输出的最大字数")
    gdr_max_topics: int = Field(default=12, description="最多归纳的话题数")
    gdr_max_quotes: int = Field(default=8, description="最多提取的金句数")
    gdr_enable_quotes: bool = Field(default=True, description="是否提取群聊金句")
    gdr_send_text: bool = Field(
        default=False,
        description=(
            "是否额外发一条文字版日报。默认关闭——海报里已包含全部内容，"
            "再发文字是重复刷屏。告警信息不受此开关影响，始终会发"
        ),
    )
    gdr_max_titles: int = Field(default=5, description="最多给多少个用户起称号")
    gdr_enable_quality: bool = Field(default=True, description="是否评估群聊质量")

    # ── 生图模型（漫画）───────────────────────────────────
    # 默认值按火山方舟 Seedream 填。换 OpenAI 需同时改 base_url、model 和 size
    # （OpenAI 用具体像素如 1024x1024，没有 "2K" 这种档位）
    gdr_image_base_url: str = Field(
        default="https://ark.cn-beijing.volces.com/api/v3/images/generations",
        description=(
            "生图接口地址。可以写到 /v1，也可以直接写完整的 .../images/generations，"
            "两种都认（后者不会被重复拼接）"
        ),
    )
    gdr_image_api_keys: List[str] = Field(default=[], description="生图模型 API Key 列表")
    gdr_image_model: str = Field(default="doubao-seedream-5-0-flash-260915", description="生图模型名")
    gdr_image_proxy: str = Field(default="", description="生图模型代理")
    gdr_image_timeout: int = Field(default=300, description="生图请求超时（秒）")
    gdr_image_size_mode: Literal["native", "upscale"] = Field(
        default="native",
        description=(
            "native=把 gdr_image_size 原样发给接口（默认，适合本身支持大尺寸的服务商）；"
            "upscale=先按 gdr_image_native_sizes 里的规格请求再本地放大，"
            "仅当 size 能解析成 WxH 时生效，适合 OpenAI 等原生封顶 1536/1792 的服务"
        ),
    )
    gdr_image_native_sizes: str = Field(
        default="1024x1024,1024x1536,1536x1024,1792x1024,1024x1792",
        description=(
            "upscale 模式可选的原生规格，逗号分隔。插件挑宽高比最接近的一个再放大——"
            "**必须保持宽高比**，否则图会被拉变形"
        ),
    )
    gdr_image_size: str = Field(
        default="2048x2048",
        description=(
            "出图尺寸，**原样透传给接口**。两种写法互斥，别混用："
            "① 档位 1K / 1.5K / 2K（Seedream 默认 2K，1.5K 与 1K 同价但效果更好）；"
            "② 宽x高 2048x2048，总像素须在 [921600, 4624220] 且宽高比在 [1/16, 16] 内。"
            "换 OpenAI 只能用 ②，且只认 1024x1024 / 1024x1536 / 1536x1024 这几个值"
        ),
    )
    gdr_image_quality: str = Field(
        default="",
        description=(
            "OpenAI 独有的质量档位 low/medium/high/auto，**与分辨率无关**，"
            "只影响细节、耗时和价格。其他服务商不认这个字段，留空即不发送"
        ),
    )
    gdr_image_response_format: Literal["", "url", "b64_json"] = Field(
        default="b64_json",
        description=(
            "返回形式：b64_json 直接给字节；url 给下载链接（插件会额外下载一次）。"
            '留空则不发送该字段，由服务商决定（Seedream 默认 url）'
        ),
    )
    gdr_image_output_format: Literal["", "png", "jpeg"] = Field(
        default="", description="出图文件格式 png/jpeg，留空则由服务商决定"
    )
    gdr_image_watermark: Optional[bool] = Field(
        default=None, description="是否加水印（Seedream 支持），None=不发送该字段"
    )
    gdr_image_extra: dict = Field(
        default_factory=dict,
        description=(
            "追加的原始请求字段，如 '{\"stream\": false}'。会覆盖上面同名字段，"
            "用于适配官方文档里有、但本插件还没做成配置项的参数"
        ),
    )
    gdr_comic_prompt: str = Field(default="", description="漫画画面风格提示词，留空使用内置默认值")

    # ── 输出 ──────────────────────────────────────────────
    gdr_report_width: int = Field(default=900, description="日报海报渲染宽度（像素）")
    gdr_enable_text_summary: bool = Field(default=True, description="同时推送纯文字总结（合并转发）")
    gdr_split_report: bool = Field(
        default=False,
        description=(
            "是否把过长的海报切成上下两段发送。切点固定在「高亮记忆碎片」"
            "结束之后、「神人名片颁发」之前——避开浏览器整页截图在页面"
            "过高时底部出现空白的问题；话题为空时自动退回整图"
        ),
    )

    # ── 定时 ──────────────────────────────────────────────
    gdr_timezone: str = Field(
        default="Asia/Shanghai",
        description="定时推送使用的时区，默认北京时间；填 Local 用服务器本地时区",
    )
    gdr_push_cron: str = Field(default="0 14,22 * * *", description="全局定时推送的 cron 表达式")
    gdr_group_list: List[int] = Field(default=[], description="参与全局定时推送的群号列表")
    gdr_group_schedules: List[GroupSchedule] = Field(
        default=[],
        description=(
            "每群独立的推送时刻，24 小时制北京时间。"
            '例：[{"group_id": 965148132, "time": "14:30"}, {"group_id": 123, "time": "22:00"}]'
        ),
    )
    gdr_enable_report: bool = Field(default=True, description="定时任务是否生成日报图片")
    gdr_enable_comic: bool = Field(
        default=True,
        description=(
            "定时推送日报时是否顺带生成漫画。只管定时；"
            "「群日报漫画」「群日报整体」是手动指令，不受这个开关影响"
        ),
    )
    gdr_push_prefetch_minutes: int = Field(
        default=8,
        description=(
            "定时推送提前多少分钟开始采集聊天记录，0=不提前（发送时刻才采集）。"
            "get_group_msg_history 有 NapCat 风控：撞上拥塞时会整页卡死到超时，"
            "最终一条都拿不到。提前采集可以避开拥塞点；"
            "只对定时推送生效，手动指令保持原样"
        ),
    )
    gdr_push_fetch_interval: float = Field(
        default=15.0,
        description=(
            "定时推送采集时的翻页间隔秒数，比手动指令的 gdr_fetch_interval 更保守。"
            "只在 gdr_push_prefetch_minutes>0 时生效"
        ),
    )

    gdr_report_mode: Literal["image", "text", "both"] = Field(
        default="both", description="日报输出方式：image=只发图，text=只发文字，both=都发"
    )
    gdr_report_style: str = Field(
        default="scrapbook",
        description=(
            "海报渲染风格，直接对应 astrbot 原插件的 6 套模板："
            "scrapbook 手账 / retro 复古未来 / miku 初音未来 / hack 赛博 / "
            "atri 亚托莉 / nouveau 新艺术运动"
        ),
    )
    gdr_prompt_style: str = Field(
        default="roast",
        description=(
            "模型提示词风格。roast=毒舌锐评（默认）/ warm=温和总结 / neutral=中性客观 / "
            "custom=用 gdr_prompt_override 完全自定义"
        ),
    )
    gdr_prompt_override: str = Field(
        default="", description="自定义提示词，仅 gdr_prompt_style=custom 时生效，留空则追加在默认提示词后"
    )

    # ── chatrecorder 落库清理 ──────────────────────────────
    gdr_cleanup_enabled: bool = Field(
        default=False,
        description=(
            "是否定期清理 chatrecorder 落库的旧消息。"
            "默认关闭：机器人若不在凌晨启动，cron 清理可能永远不触发"
        ),
    )
    gdr_cleanup_cron: str = Field(
        default="30 4 * * *", description="清理任务的 cron（建议放在凌晨低峰期）"
    )
    gdr_retention_days: int = Field(
        default=7, description="消息保留天数，超过则删除；0 表示不按时间清理"
    )
    gdr_db_max_mb: int = Field(
        default=100, description="数据库体积上限（MB），超过则从最旧开始删；0 表示不按体积清理"
    )
    gdr_cleanup_batch: int = Field(
        default=5000, description="每批删除条数，避免长事务锁库"
    )

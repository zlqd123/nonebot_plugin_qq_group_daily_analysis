"""插件配置模型（MaiBot WebUI 配置页据此自动生成）。

移植自 nonebot 版 ``config.py`` 的全部配置项，原 ``gdr_`` 前缀环境变量改为
MaiBot 的分组 pydantic 模型：分组的顶层字段即 WebUI 配置页的分区，``Literal``
字段自动渲染为下拉框。移植模块代码仍按 ``gdr_*`` 扁平名访问配置，映射见
:mod:`core.settings`。
"""

from __future__ import annotations

from typing import Literal

from maibot_sdk import Field, PluginConfigBase


class GroupScheduleItem(PluginConfigBase):
    """单个群的定时推送设置。"""

    __ui_label__ = "群定时项"

    group_id: str = Field(default="", description="群号（纯数字字符串）")
    time: str = Field(default="22:00", description="推送时刻，24 小时制「HH:MM」")
    enable: bool = Field(default=True, description="是否启用该条定时")


class BasicSection(PluginConfigBase):
    """基础与定时。"""

    __ui_label__ = "基础与定时"
    __ui_order__ = 1

    group_schedules: list[GroupScheduleItem] = Field(
        default_factory=list,
        description=(
            "每群独立的推送时刻（推荐）。"
            '例：[{"group_id": "123456789", "time": "14:30"}, {"group_id": "123", "time": "22:00"}]'
        ),
    )
    group_list: list[str] = Field(
        default_factory=list,
        description="参与全局定时推送的群号列表（与 push_cron 配合，独立时刻未覆盖的群走这里）",
    )
    push_cron: str = Field(
        default="0 14,22 * * *", description="全局定时推送的 5 段 cron 表达式"
    )
    timezone: str = Field(
        default="Asia/Shanghai",
        description="定时推送使用的时区，默认北京时间；填 Local 用服务器本地时区",
    )
    enable_report: bool = Field(default=True, description="定时任务是否生成日报海报")
    enable_comic: bool = Field(
        default=True,
        description=(
            "定时推送日报时是否顺带生成漫画。只管定时；"
            "「群日报漫画」「群日报整体」是手动指令，不受这个开关影响"
        ),
    )


class FetchSection(PluginConfigBase):
    """消息采集。"""

    __ui_label__ = "消息采集"
    __ui_order__ = 2

    source: Literal["auto", "message_api", "db"] = Field(
        default="auto",
        description=(
            "消息来源：message_api=优先调宿主消息接口（ctx.message，按时间窗口取）；"
            "db=直查宿主 ChatHistory 库（零接口调用，仅兜底）；auto=先消息接口，"
            "一条都没拿到才回落 db"
        ),
    )
    window: str = Field(
        default="today",
        description=(
            "日报覆盖的时间范围：today=今天0点至今 / 24h=最近24小时 / "
            "yesterday=昨日全天 / hours:N=最近N小时"
        ),
    )
    window_min_messages: int = Field(
        default=1,
        description="窗口内消息少于该数时视为数据不足，直接提示；1=只要有消息就总结（0 条才跳过）",
    )
    history_lens: int = Field(
        default=20000, description="条数安全上限；真正的终止条件是覆盖时间窗口"
    )
    max_messages: int = Field(default=4000, description="送入模型的聊天记录条数上限")
    prefetch_minutes: int = Field(
        default=8,
        description=(
            "定时推送提前多少分钟开始采集聊天记录，0=不提前（发送时刻才采集）。"
            "提前采集可以避开协议端拥塞窗口；只对定时推送生效，手动指令保持原样"
        ),
    )
    push_fetch_interval: float = Field(
        default=15.0,
        description=(
            "定时推送采集时的翻页间隔秒数，比手动指令更保守。只在 prefetch_minutes>0 时生效"
        ),
    )
    fetch_page_size: int = Field(default=5000, description="单次请求条数（消息接口分页用）")
    fetch_max_pages: int = Field(default=1, description="最大翻页数，保持 1 即可")
    fetch_interval: float = Field(default=5.0, description="两次翻页之间的间隔秒数")
    member_cache_ttl: int = Field(default=21600, description="群成员数缓存时长（秒）")
    member_counts: dict = Field(
        default_factory=dict,
        description=(
            '群成员数覆盖表，形如 {"123456789": 50}。宿主取不到群人数时用它兜底，'
            "保证海报上的活跃率有分母"
        ),
    )
    bot_qq: str = Field(
        default="",
        description="机器人自身 QQ 号，用于过滤机器人自己的消息；留空则自动探测，探测不到时不过滤",
    )


class LlmSection(PluginConfigBase):
    """文字总结模型（可单独指定模型）。"""

    __ui_label__ = "文字总结模型"
    __ui_order__ = 3

    base_url: str = Field(
        default="",
        description=(
            "文字总结模型 base_url，兼容 OpenAI / Gemini 两种协议。"
            "**留空则走 MaiBot 宿主模型**（用下面的模型名调 ctx.llm.generate）"
        ),
    )
    api_keys: list[str] = Field(
        default_factory=list,
        description="文字模型 API Key 列表（base_url 非空时必填），429 时自动轮换",
    )
    model: str = Field(
        default="",
        description=(
            "文字总结模型名。base_url 留空时：留空=用 MaiBot 宿主默认模型，"
            "也可填宿主模型列表里的名字指定；base_url 直连时必填"
        ),
    )
    proxy: str = Field(default="", description="文字模型代理，形如 http://ip:port")
    timeout: int = Field(default=300, description="文字模型请求超时（秒）")
    max_tokens: int = Field(
        default=0,
        description=(
            "输出上限（OpenAI 传 max_tokens，Gemini 传 maxOutputTokens）。"
            "0=不发送该字段，由服务端决定（默认，推荐）"
        ),
    )
    max_topics: int = Field(default=12, description="最多归纳的话题数")
    max_quotes: int = Field(default=8, description="最多提取的金句数")
    max_titles: int = Field(default=5, description="最多给多少个用户起称号")
    enable_quotes: bool = Field(default=True, description="是否提取群聊金句")
    enable_quality: bool = Field(default=True, description="是否评估群聊质量")


class ImageSection(PluginConfigBase):
    """生图模型（漫画，可单独指定模型）。"""

    __ui_label__ = "生图模型（漫画）"
    __ui_order__ = 4

    provider: Literal["doubao", "images", "google", "chat", "grok"] = Field(
        default="doubao",
        description=(
            "生图接口协议：doubao=火山方舟 Seedream（默认）/ images=OpenAI 兼容 "
            "images/generations / google=Gemini generateContent 原生接口 / "
            "chat=OpenAI 兼容 chat/completions 多模态生图 / grok=x.ai"
        ),
    )
    base_url: str = Field(
        default="https://ark.cn-beijing.volces.com/api/v3/images/generations",
        description=(
            "生图接口地址。可以写到 /v1，也可以直接写完整的 .../images/generations，"
            "两种都认（后者不会被重复拼接）"
        ),
    )
    api_keys: list[str] = Field(default_factory=list, description="生图模型 API Key 列表")
    model: str = Field(default="doubao-seedream-5-0-flash-260915", description="生图模型名")
    proxy: str = Field(default="", description="生图模型代理")
    timeout: int = Field(default=300, description="生图请求超时（秒）")
    size: str = Field(
        default="2048x1152",
        description=(
            "出图尺寸，原样透传给接口。默认 2048x1152（16:9 横版），适合横向多格漫画条。"
            "两种写法互斥：① 档位 1K / 1.5K / 2K（Seedream）；② 宽x高 2048x1152。"
            "换 OpenAI 只能用 ②，且只认 1024x1024 / 1024x1536 / 1536x1024"
        ),
    )
    size_mode: Literal["native", "upscale"] = Field(
        default="native",
        description=(
            "native=size 原样发给接口（默认）；upscale=先按 native_sizes 里的规格请求"
            "再本地放大，仅当 size 能解析成 WxH 时生效，适合原生封顶 1536/1792 的服务"
        ),
    )
    native_sizes: str = Field(
        default="1024x1024,1024x1536,1536x1024,1792x1024,1024x1792",
        description="upscale 模式可选的原生规格，逗号分隔，必须保持宽高比",
    )
    quality: str = Field(
        default="",
        description=(
            "OpenAI 独有的质量档位 low/medium/high/auto，与分辨率无关。"
            "其他服务商不认这个字段，留空即不发送"
        ),
    )
    response_format: Literal["default", "url", "b64_json"] = Field(
        default="default",
        description=(
            "返回形式：b64_json 直接给字节；url 给下载链接（插件会额外下载一次）。"
            "default 则不发送该字段，由服务商决定"
        ),
    )
    output_format: Literal["default", "png", "jpeg"] = Field(
        default="default", description="出图文件格式 png/jpeg，default 则由服务商决定"
    )
    watermark: Literal["default", "true", "false"] = Field(
        default="default", description="是否加水印（Seedream 支持），default=不发送该字段"
    )
    aspect_ratio: str = Field(
        default="16:9",
        description=(
            "漫画宽高比（如 16:9 / 4:3 / 1:1 / 9:16）。目前 google 与 chat 协议使用；"
            "images/doubao 用 size 控制比例"
        ),
    )
    optimize_prompt: Literal["default", "off", "fast", "standard"] = Field(
        default="default",
        description=(
            "Seedream 服务端提示词优化：default=服务端默认；off=显式关闭"
            "（漫画内容跑偏时可尝试关闭）；fast/standard=显式选择优化档位"
        ),
    )
    extra: dict = Field(
        default_factory=dict,
        description=(
            '追加的原始请求字段，如 {"stream": false}。会覆盖同名字段，'
            "用于适配官方文档里有、但本插件还没做成配置项的参数"
        ),
    )
    comic_prompt: str = Field(
        default="",
        description=(
            "漫画画面风格提示词（如「日式漫画风格，干净线稿」），"
            "作为风格后缀追加在上游分镜规范之后；留空不追加"
        ),
    )
    retries: int = Field(default=1, description="生图失败后的重试次数（网络/内容异常均计），0=不重试")
    retry_delay: float = Field(default=2.0, description="生图重试间隔秒数")


class ReportSection(PluginConfigBase):
    """输出与提示词风格。"""

    __ui_label__ = "输出与提示词"
    __ui_order__ = 5

    mode: Literal["image", "text", "both"] = Field(
        default="both", description="日报输出方式：image=只发图，text=只发文字，both=都发"
    )
    style: Literal["scrapbook", "retro", "miku", "hack", "atri", "nouveau"] = Field(
        default="scrapbook",
        description=(
            "海报渲染风格（astrbot 原插件 6 套主题模板原样搬运）："
            "scrapbook 手账 / retro 复古未来 / miku 初音未来 / hack 赛博 / "
            "atri 亚托莉 / nouveau 新艺术运动"
        ),
    )
    width: int = Field(default=900, description="日报海报渲染宽度（像素）")
    split_report: bool = Field(
        default=False,
        description=(
            "是否把过长的海报切成上下两段发送（切点固定在「高亮记忆碎片」之后、"
            "「神人名片颁发」之前）。依赖宿主渲染器执行页面脚本，"
            "不可用时自动退回整图"
        ),
    )
    enable_text_summary: bool = Field(default=True, description="both 模式下是否额外发纯文字总结")
    send_text: bool = Field(
        default=False,
        description=(
            "是否额外发一条文字版日报。默认关闭——海报里已包含全部内容，"
            "再发文字是重复刷屏。告警信息不受此开关影响，始终会发"
        ),
    )
    prompt_style: Literal["roast", "warm", "neutral", "custom"] = Field(
        default="roast",
        description=(
            "模型提示词风格。roast=毒舌锐评（默认）/ warm=温和总结 / neutral=中性客观 / "
            "custom=用 prompt_override 完全自定义"
        ),
    )
    prompt_override: str = Field(
        default="",
        description="自定义提示词，仅 prompt_style=custom 时生效，留空则追加在默认提示词后",
    )


class SensitiveSection(PluginConfigBase):
    """敏感内容防护（生图链路）。"""

    __ui_label__ = "敏感内容防护"
    __ui_order__ = 6

    filter: bool = Field(
        default=True,
        description=(
            "生图提示词本地敏感词筛查开关。开启后命中词库的提示词会先交给"
            "文字模型脱敏改写，改不掉就降级为无背景上下文的精简分镜"
        ),
    )
    lexicon: str = Field(
        default="",
        description=(
            "附加敏感词库文件路径，逗号分隔（与插件 data/sensitive_lexicon.txt "
            "合并去重，行格式：词条<TAB>类别）。词库自备、可选"
        ),
    )
    rewrites: int = Field(
        default=1,
        description=(
            "生图提示词命中敏感词后，交给文字模型脱敏改写的次数，0=不改写"
            "（命中即降级精简分镜）。改写也会再做本地复查"
        ),
    )


class CleanupSection(PluginConfigBase):
    """插件缓存清理。"""

    __ui_label__ = "缓存清理"
    __ui_order__ = 7

    enabled: bool = Field(
        default=False,
        description="是否定期清理插件数据目录里的临时文件（渲染缓存、历史图片等）",
    )
    cron: str = Field(default="30 4 * * *", description="清理任务的 5 段 cron（建议放在凌晨低峰期）")
    retention_days: int = Field(
        default=7, description="临时文件保留天数，超过则删除；0 表示不按时间清理"
    )


class PluginSection(PluginConfigBase):
    """插件基础配置（Runner 约定必须存在：版本号从这里提取）。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class GroupDailyAnalysisConfig(PluginConfigBase):
    """插件配置根模型。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    basic: BasicSection = Field(default_factory=BasicSection)
    fetch: FetchSection = Field(default_factory=FetchSection)
    llm: LlmSection = Field(default_factory=LlmSection)
    image: ImageSection = Field(default_factory=ImageSection)
    report: ReportSection = Field(default_factory=ReportSection)
    sensitive: SensitiveSection = Field(default_factory=SensitiveSection)
    cleanup: CleanupSection = Field(default_factory=CleanupSection)


# ── 配置页汉化（label/hint）─────────────────────────────────────
# SDK 的 schema 生成器以 json_schema_extra["label"] 为显示名（缺省回退到
# 英文字段名），hint 为输入框下的灰字说明。description 保留作详细文档。


def _apply_zh_labels(model_cls: type, labels: dict[str, tuple[str, str]]) -> None:
    """给模型的字段批量补 ``json_schema_extra.label/hint``。

    Args:
        model_cls: 配置分组类。
        labels: ``字段名 -> (显示名, 提示语)``。
    """
    for name, (label, hint) in labels.items():
        field_info = model_cls.model_fields.get(name)
        if field_info is None:
            continue
        extra = dict(field_info.json_schema_extra or {})
        extra["label"] = label
        extra["hint"] = hint
        field_info.json_schema_extra = extra


_apply_zh_labels(PluginSection, {
    "enabled": ("启用插件", "关闭后插件停止响应指令与定时任务"),
    "config_version": ("配置版本", "结构升级标记，保持默认即可"),
})

_apply_zh_labels(GroupScheduleItem, {
    "group_id": ("群号", "纯数字 QQ 群号，如 123456789"),
    "time": ("推送时刻", "24 小时制 HH:MM，按下方时区设置"),
    "enable": ("启用该条", "临时停用某一群而不删除配置"),
})

_apply_zh_labels(BasicSection, {
    "group_schedules": ("每群独立推送时刻", "推荐用这个；可加多群、每群多时刻"),
    "group_list": ("全局推送群列表", "配合 push_cron 使用；独立时刻未覆盖的群走这里"),
    "push_cron": ("全局推送 cron", "5 段 cron；14,22 表示每天 14 点与 22 点各推一次"),
    "timezone": ("时区", "定时任务使用的时区；填 Local 用服务器本地时区"),
    "enable_report": ("定时出海报", "关闭后定时推送只发文字/漫画；手动指令不受影响"),
    "enable_comic": ("定时连带漫画", "定时推送时是否顺带生成漫画"),
})

_apply_zh_labels(FetchSection, {
    "source": ("消息来源", "auto=宿主消息接口优先、取不到回落数据库"),
    "window": ("统计窗口", "today=今天0点起 / 24h=最近24小时 / yesterday=昨日 / hours:N=最近N小时"),
    "window_min_messages": ("最少消息数", "低于此数不生成日报；1=只要有消息就总结，0 条才跳过"),
    "history_lens": ("采集条数上限", "安全上限；实际以覆盖时间窗口为准"),
    "max_messages": ("送模型条数上限", "超过则只取最近的，控制 token 消耗"),
    "prefetch_minutes": ("提前采集（分钟）", "定时推送提前 N 分钟拉消息，避开协议端拥塞；0=不提前"),
    "push_fetch_interval": ("定时采集间隔（秒）", "提前采集时的保守翻页间隔"),
    "fetch_page_size": ("单页条数", "单次请求的消息条数"),
    "fetch_max_pages": ("最大翻页数", "保持 1 即可"),
    "fetch_interval": ("翻页间隔（秒）", "手动指令采集时的翻页间隔"),
    "member_cache_ttl": ("群信息缓存（秒）", "群名/成员数的缓存时长"),
    "member_counts": ("群人数覆盖表", '宿主取不到群人数时兜底，如 {"123456789": 50}'),
    "bot_qq": ("机器人 QQ 号", "用于过滤机器人自己的消息；留空自动探测"),
})

_apply_zh_labels(LlmSection, {
    "base_url": ("文字模型接口地址", "OpenAI 兼容填到 /v1；Gemini 填 v1beta；留空走 MaiBot 宿主模型"),
    "api_keys": ("文字模型 API Key", "可填多个，429 限流自动轮换"),
    "model": ("文字模型名", "直连必填（如 mimo-v2.6-flash）；走宿主时留空=宿主默认模型"),
    "proxy": ("代理", "形如 http://ip:port，留空直连"),
    "timeout": ("请求超时（秒）", "单次调用超时时间"),
    "max_tokens": ("输出上限", "0=不发送该字段（推荐，避免长 JSON 被截断）"),
    "max_topics": ("话题数上限", "日报最多归纳的话题数"),
    "max_quotes": ("金句数上限", "最多提取的群聊金句数"),
    "max_titles": ("称号数上限", "最多给多少用户起称号"),
    "enable_quotes": ("提取金句", "关闭后输出不再包含金句"),
    "enable_quality": ("聊天质量锐评", "关闭后输出不再包含锐评维度"),
})

_apply_zh_labels(ImageSection, {
    "provider": ("生图协议", "doubao=火山方舟 Seedream / images=OpenAI / google=Gemini / chat=多模态 / grok=x.ai"),
    "base_url": ("生图接口地址", "可写完整地址也可写域名，插件自动补路径"),
    "api_keys": ("生图 API Key", "可填多个，429 自动轮换；留空则漫画跳过"),
    "model": ("生图模型名", "如 doubao-seedream-5-0-flash-260915"),
    "proxy": ("生图代理", "留空直连"),
    "timeout": ("请求超时（秒）", "生图普遍较慢，建议 300 秒以上"),
    "size": ("出图尺寸", "档位 2K 或 宽x高（如 2048x2048）；OpenAI 只认 1024/1536/1792 档"),
    "size_mode": ("尺寸模式", "native=原样发送；upscale=按原生规格出图后本地放大"),
    "native_sizes": ("原生规格列表", "upscale 模式的可选规格，逗号分隔，须保持宽高比"),
    "quality": ("质量档位", "仅 OpenAI 支持 low/medium/high/auto，留空不发送"),
    "response_format": ("返回形式", "b64_json=直接给字节；url=给链接（插件会再下载）；default=服务商决定"),
    "output_format": ("出图格式", "png/jpeg，default=服务商决定"),
    "watermark": ("加水印", "Seedream 支持；default=不发送该字段"),
    "aspect_ratio": ("漫画宽高比", "16:9 / 4:3 / 1:1 / 9:16；google 与 chat 协议使用"),
    "optimize_prompt": ("提示词优化", "Seedream 服务端改写开关；漫画跑偏可试 off"),
    "extra": ("附加请求字段", '透传给接口的额外参数，如 {"stream": false}，可覆盖同名字段'),
    "comic_prompt": ("漫画画面风格", "风格后缀，如「日式漫画风格，干净线稿」；留空不追加"),
    "retries": ("生图重试次数", "网络/内容异常后的重试次数"),
    "retry_delay": ("重试间隔（秒）", "两次生图尝试之间的等待"),
})

_apply_zh_labels(ReportSection, {
    "mode": ("输出方式", "image=只发海报 / text=只发文字 / both=都发"),
    "style": ("海报主题", "6 套 astrbot 原版主题任选"),
    "width": ("海报宽度（像素）", "渲染宽度，默认 900"),
    "split_report": ("分段发送", "海报过长切上下两段；依赖宿主渲染器执行脚本，不可用自动退回整图"),
    "enable_text_summary": ("附带文字总结", "both 模式下海报之外是否再发纯文字版"),
    "send_text": ("额外发文字版", "海报已含全部内容，再发是重复刷屏；告警不受此开关限制"),
    "prompt_style": ("点评语气", "roast=毒舌锐评 / warm=温和 / neutral=中性 / custom=自定义"),
    "prompt_override": ("自定义提示词", "仅 custom 风格生效；留空则追加在默认提示词后"),
})

_apply_zh_labels(SensitiveSection, {
    "filter": ("敏感词筛查", "生图提示词本地词库二次筛查"),
    "lexicon": ("附加词库路径", "逗号分隔，与插件 data/sensitive_lexicon.txt 合并；词库自备、可选"),
    "rewrites": ("脱敏改写次数", "命中后交文字模型改写的次数；0=命中即降级精简分镜"),
})

_apply_zh_labels(CleanupSection, {
    "enabled": ("启用清理", "定期清理插件数据目录的过期缓存"),
    "cron": ("清理 cron", "建议凌晨低峰期"),
    "retention_days": ("保留天数", "超过则删除；0=不清理"),
})

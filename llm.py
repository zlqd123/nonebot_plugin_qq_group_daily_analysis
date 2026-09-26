"""文字总结模型客户端。

与生图模型完全独立配置（``gdr_text_*``）。同时兼容两种协议：
- base_url 含 ``googleapis.com`` 时走 Gemini ``generateContent``；
- 否则走 OpenAI 兼容的 ``/chat/completions``。

支持多 Key 轮换：遇到 429 自动切到下一个 Key，全部限流才报错。
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from nonebot.log import logger

from .config import Config
from .stats import ChatStats

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(raw: str) -> str:
    """从模型输出里抠出 JSON 文本，容忍 ```json 包裹与前后废话。"""
    match = _JSON_BLOCK.search(raw)
    if match:
        return match.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        return raw[start : end + 1]
    return raw.strip()


def _parse_json(raw: str) -> dict[str, Any]:
    """解析模型输出为字典，失败时抛出 ``ValueError``。"""
    text = _extract_json(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"模型未返回合法 JSON: {e!s}\n原始输出前 500 字: {raw[:500]}") from e
    if not isinstance(data, dict):
        raise ValueError("模型返回的 JSON 顶层不是对象")
    return data


class TextModel:
    """文字总结模型调用封装。"""

    def __init__(self, config: Config) -> None:
        self._config = config

    @property
    def available(self) -> bool:
        """是否配置了可用的 Key。"""
        return bool(self._config.gdr_text_api_keys)

    def _build_request(
        self, key: str, system: str, user: str
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """构造 URL、请求头与请求体，自动适配两种协议。"""
        cfg = self._config
        headers = {"Content-Type": "application/json"}

        if "googleapis.com" in cfg.gdr_text_base_url:
            url = f"{cfg.gdr_text_base_url}/models/{cfg.gdr_text_model}:generateContent?key={key}"
            payload: dict[str, Any] = {
                "contents": [
                    {"parts": [{"text": system}], "role": "user"},
                    {"parts": [{"text": user}], "role": "user"},
                ]
            }
            return url, headers, payload

        headers["Authorization"] = f"Bearer {key}"
        url = f"{cfg.gdr_text_base_url}/chat/completions"
        payload = {
            "model": cfg.gdr_text_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if cfg.gdr_text_thinking is not None:
            payload["reasoning_effort"] = "high" if cfg.gdr_text_thinking else "low"
            payload["extra_body"] = {
                "thinking": {"type": "enabled" if cfg.gdr_text_thinking else "disabled"}
            }
        return url, headers, payload

    async def complete(self, system: str, user: str) -> str:
        """调用文字模型并返回文本。

        Args:
            system: 系统提示词。
            user: 用户内容（通常是压缩后的聊天记录）。

        Returns:
            模型输出文本。

        Raises:
            RuntimeError: 未配置 Key，或全部 Key 调用失败。
        """
        cfg = self._config
        if not cfg.gdr_text_api_keys:
            raise RuntimeError("未配置 gdr_text_api_keys，无法调用文字总结模型")

        last_error: Exception | None = None
        for key in cfg.gdr_text_api_keys:
            url, headers, payload = self._build_request(key, system, user)
            try:
                async with httpx.AsyncClient(
                    proxy=cfg.gdr_text_proxy or None,
                    timeout=cfg.gdr_text_timeout,
                ) as client:
                    resp = await client.post(url, headers=headers, json=payload)
                    resp.raise_for_status()
                    body = resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    logger.warning("[群日报] 文字模型 Key 已限流，切换下一个")
                    last_error = e
                    continue
                raise RuntimeError(f"文字模型调用失败: {e!s}") from e
            except Exception as e:
                raise RuntimeError(f"文字模型调用异常: {e!s}") from e

            if "choices" in body:
                return str(body["choices"][0]["message"].get("content") or "")
            if "candidates" in body:
                parts = body["candidates"][0].get("content", {}).get("parts", [])
                return "".join(str(p.get("text", "")) for p in parts)
            raise RuntimeError(f"文字模型返回了未知结构: {list(body.keys())}")

        raise RuntimeError(f"文字模型所有 Key 均限流或失败: {last_error!s}")

    async def complete_json(
        self, system: str, user: str, *, retries: int = 2
    ) -> dict[str, Any]:
        """调用文字模型并要求返回 JSON，自动做格式纠错重试。

        Args:
            system: 系统提示词。
            user: 用户内容。
            retries: JSON 解析失败后的重试次数。

        Returns:
            解析后的字典。

        Raises:
            RuntimeError: 重试耗尽仍未拿到合法 JSON。
        """
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            raw = await self.complete(system, user)
            try:
                return _parse_json(raw)
            except ValueError as e:
                last_error = e
                logger.warning(f"[群日报] JSON 解析失败（第 {attempt + 1} 次）: {e!s}")
                if attempt < retries:
                    user = f"{user}\n\n【重要】上一次输出不是合法 JSON，请只输出 JSON，不要任何解释文字。"
        raise RuntimeError(f"文字模型多次未返回合法 JSON: {last_error!s}")


# 各风格的点评语气指导。结构沿用 astrbot 的「聊天质量锐评」提示词，
# 但语气分档暴露给用户配置，避免一键毒舌在正经群里不合适。
_STYLE_GUIDES: dict[str, str] = {
    "roast": """【点评风格】毒舌锐评
- 语言接地气，多用互联网黑话，吐槽精准，避重就轻。
- 可以损，但损的是行为和现象，不要针对具体的人贴标签。
- 毒舌程度控制在「群友看了会笑而不是生气」的水平。""",
    "warm": """【点评风格】温和总结
- 语气友好、鼓励为主，多肯定每个人的参与。
- 可以调侃，但保持善意，不做尖锐评价。
- 适合同事群、亲友群等非熟人玩笑的场合。""",
    "neutral": """【点评风格】中性客观
- 只陈述事实与现象，不做价值判断，不使用网络梗。
- 措辞严谨，适合需要存档留痕的群。""",
    "custom": """【点评风格】自定义
- 以上方 JSON 结构与维度划分要求为准，其余按本文档最后追加的自定义要求执行。""",
}


def _style_guide(style: str) -> str:
    """取指定风格的点评语气指导。

    Args:
        style: 风格名，见 :data:`_STYLE_GUIDES`。

    Returns:
        风格指导文本；未知风格回落到毒舌锐评。
    """
    key = (style or "roast").strip().lower()
    if key not in _STYLE_GUIDES:
        logger.warning(f"[群日报] 未知提示词风格 {style!r}，回落到 roast")
        key = "roast"
    return _STYLE_GUIDES[key]


def _custom_prompt(config: Config) -> str:
    """拼装自定义提示词。

    Args:
        config: 插件配置。

    Returns:
        要追加的文本；没配则为空串。
    """
    text = (config.gdr_prompt_override or "").strip()
    return f"\n【额外要求】\n{text}\n" if text else ""


def build_analysis_prompt(
    stats: ChatStats,
    chat_lines: list[str],
    code_to_name: dict[str, str],
    config: Config,
    window_label: str = "今日",
) -> tuple[str, str]:
    """构造结构化分析用的系统提示词与用户内容。

    Args:
        stats: 统计结果。
        chat_lines: 昵称压缩后的聊天记录。
        code_to_name: 代号到昵称的映射。
        config: 插件配置。

    Returns:
        ``(系统提示词, 用户内容)``。
    """
    system = f"""你是群聊分析专家，负责从群聊记录中提取结构化信息。
请严格只输出一个 JSON 对象，不要输出任何解释文字，也不要用 ``` 包裹。

JSON 结构：
{{
  "summary": "3-4 句话的整体总结",
  "topics": [
    {{"title": "话题标题", "detail": "详细描述讨论的起因、经过、不同观点与结论", "users": ["000", "001"]}}
  ],
  "quotes": [
    {{"text": "精彩发言原文", "user": "000", "reason": "为什么这句精彩"}}
  ],
  "keywords": ["热词1", "热词2"],
  "mood": "整体氛围的简短评价",
  "quality": {{
    "title": "今日群聊主题",
    "subtitle": "副标题",
    "dimensions": [
      {{"name": "抽象维度名", "percentage": 35, "comment": "该维度的犀利点评"}}
    ],
    "summary": "一句总结性的金句"
  }},
  "titles": [
    {{"user": "000", "title": "给他起的称号", "reason": "为什么配得上这个称号"}}
  ]
}}

硬性要求：
1. 聊天记录中的用户使用代号（000、001、002 这样的三位十六进制），输出时必须原样保留代号，禁止替换成昵称 或写成「张三（000）」。
2. topics 最多 {config.gdr_max_topics} 个，哪怕某个话题只有两三个人聊了几句也要单独列出。
3. quotes 最多 {config.gdr_max_quotes} 条，text 必须是聊天记录中出现过的原文，不要编造。
4. keywords 给出 6-10 个真正有语义的词或短语（专有名词、产品、技术栈、梗），
   必须是完整可读的词，不要出现「务器」「器部」这类被切碎的碎片。
5. 只输出 JSON，不要 markdown 符号。
6. 所有中文内容不要使用 markdown 语法。
7. titles 给 {config.gdr_max_titles} 个发言有特色的用户起称号，风格可以调侃但不要冒犯或带人身攻击。
8. quality 是「聊天质量锐评」，按下面的维度划分要求产出。

【quality 维度划分要求】
- 划分 3-6 个**高层级、抽象、泛化**的维度（如：就业焦虑、生涯规划、技术方案研究、情感树洞、无意义水群）。
- **严禁在维度名称（name）中出现任何具体的人名、群名、项目名、具体报错或细碎事件点。名称必须高度抽象且 2-6 个字。**
- 每个维度给一个百分比，**总和不超过 100%**。
- 具体的事件、梗、吐槽细节全部放进 comment，不要放进 name。
- title/subtitle 是本次报告的主题标题与副标题，summary 是一句全群表现总结。

{_style_guide(config.gdr_prompt_style)}
{_custom_prompt(config)}"""

    facts = [
        f"统计范围：{window_label}，{stats.time_range_text}",
        f"消息总数：{stats.total_messages} 条",
        f"活跃人数：{stats.participant_count} 人（群总人数 {stats.member_count} 人，"
        f"活跃率 {stats.active_ratio * 100:.1f}%）",
        f"总字数：{stats.total_chars}，平均每条 {stats.avg_length:.1f} 字",
        f"最活跃时段：{stats.peak_hour:02d} 时",
        f"表情使用：{stats.emoji.total} 个",
        f"贡献榜前三：{stats.top_user_text}",
        f"跨度：{stats.duration_hours:.1f} 小时，约 {stats.messages_per_hour:.1f} 条/小时",
    ]
    mapping = "\n".join(f"{code} = {name}" for code, name in code_to_name.items())
    user = (
        "【统计事实】\n" + "\n".join(facts) + "\n\n"
        "【代号与昵称对照】（仅供你理解，输出中禁止使用昵称）\n" + mapping + "\n\n"
        "【聊天记录】\n" + "\n".join(chat_lines)
    )
    return system, user


def build_comic_prompt_prompt(
    summary: str, topics: list[dict[str, Any]], config: Config
) -> str:
    """构造让文字模型产出漫画分镜提示词的系统提示词。

    Args:
        summary: 群聊整体总结。
        topics: 结构化话题列表。
        config: 插件配置。

    Returns:
        系统提示词文本。
    """
    style = config.gdr_comic_prompt or (
        "日式漫画风格，干净线稿，柔和配色，横向分镜，"
        "轻松日常氛围，无文字气泡"
    )
    topic_text = "\n".join(
        f"- {t.get('title', '')}: {str(t.get('detail', ''))[:200]}" for t in topics
    )
    return f"""你是漫画分镜师。请根据群聊内容设计一张 4 格连环漫画的绘图提示词。

【群聊总结】
{summary}

【话题】
{topic_text}

【要求】
1. 输出一段英文绘图提示词，直接描述画面，不要写解释。
2. 必须包含 4 个分镜，用 "panel 1:"、"panel 2:"、"panel 3:"、"panel 4:" 分隔。
3. 每个分镜描述：场景、人物外观（用占位符如 a cheerful girl with short hair）、
   表情、动作、以及该格对应的群聊梗。
4. 人物外观在 4 格中保持一致。
5. 风格要求：{style}
6. 不要在画面中出现任何文字、水印或对话框。"""

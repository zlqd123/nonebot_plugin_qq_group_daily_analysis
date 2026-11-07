# -*- coding: utf-8 -*-
"""文字总结模型客户端与日报分析提示词。

移植自 bison5 的 ``llm.py``，砍掉了漫画分镜与敏感词改写部分。
双协议兼容（OpenAI / Gemini）与解析容错保持原样。
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
_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """剥离模型/中转内联的 ``<think>`` 推理块。

    兼容三种形态：成对、未闭合、孤立闭合标签。
    """
    text = text or ""
    if "</think>" in text and "<think>" not in text:
        text = text.rsplit("</think>", 1)[1]
    text = _THINK_BLOCK.sub("", text)
    if "<think>" in text:  # 未闭合：截断发生在思考中途，后面不会再有正文
        text = text.split("<think>", 1)[0]
    return text.strip()


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
        raise ValueError(
            f"模型未返回合法 JSON: {e!s}\n原始输出前 500 字: {raw[:500]}"
        ) from e
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
        return bool(self._config.gdrq_text_api_keys)

    @property
    def _is_gemini(self) -> bool:
        return "googleapis.com" in self._config.gdrq_text_base_url

    def _build_request(
        self, key: str, system: str, user: str
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """构造 URL、请求头与请求体，自动适配两种协议。"""
        cfg = self._config
        headers = {"Content-Type": "application/json"}

        if self._is_gemini:
            url = f"{cfg.gdrq_text_base_url}/models/{cfg.gdrq_text_model}:generateContent?key={key}"
            payload: dict[str, Any] = {
                # system 必须走 systemInstruction：contents 中连续两个 user
                # 轮次会被 Gemini 拒绝（400 multiturn requests alternate）
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"parts": [{"text": user}], "role": "user"}],
            }
            if cfg.gdrq_max_tokens > 0:
                payload["generationConfig"] = {
                    "maxOutputTokens": cfg.gdrq_max_tokens
                }
            return url, headers, payload

        headers["Authorization"] = f"Bearer {key}"
        url = f"{cfg.gdrq_text_base_url}/chat/completions"
        payload = {
            "model": cfg.gdrq_text_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if cfg.gdrq_max_tokens > 0:
            payload["max_tokens"] = cfg.gdrq_max_tokens
        return url, headers, payload

    @staticmethod
    def _pick_text(body: dict[str, Any]) -> str:
        """从两种协议的响应里取出模型文本，过滤思考内容。"""
        if "choices" in body:
            try:
                message = body["choices"][0].get("message") or {}
            except (IndexError, TypeError, AttributeError):
                message = {}
            text = str(message.get("content") or "")
            if not text.strip():
                # 部分中转把正文放 reasoning_content，content 留空
                text = str(message.get("reasoning_content") or "")
            return strip_reasoning(text)

        if "candidates" in body:
            try:
                parts = body["candidates"][0].get("content", {}).get("parts", [])
            except (IndexError, TypeError, AttributeError):
                parts = []
            kept = [
                str(p.get("text") or "")
                for p in parts
                if isinstance(p, dict) and p.get("thought") is not True
            ]
            return strip_reasoning("".join(kept))

        return ""

    async def complete(self, system: str, user: str) -> str:
        """调用文字模型并返回文本；仅 429 时轮换下一个 Key。"""
        cfg = self._config
        if not cfg.gdrq_text_api_keys:
            raise RuntimeError("未配置 GDRQ_TEXT_API_KEYS，无法调用文字总结模型")

        last_error: Exception | None = None
        for key in cfg.gdrq_text_api_keys:
            url, headers, payload = self._build_request(key, system, user)
            try:
                async with httpx.AsyncClient(
                    proxy=cfg.gdrq_text_proxy or None,
                    timeout=cfg.gdrq_text_timeout,
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

            text = self._pick_text(body)
            if text.strip():
                return text
            raise RuntimeError(f"文字模型返回了空内容或未知结构: {list(body.keys())}")

        raise RuntimeError(f"文字模型所有 Key 均限流或失败: {last_error!s}")

    async def complete_json(
        self, system: str, user: str, *, retries: int = 2
    ) -> dict[str, Any]:
        """调用文字模型并要求返回 JSON，自动做格式纠错重试。"""
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            raw = await self.complete(system, user)
            try:
                return _parse_json(raw)
            except ValueError as e:
                last_error = e
                logger.warning(f"[群日报] JSON 解析失败（第 {attempt + 1} 次）: {e!s}")
                if attempt < retries:
                    user = (
                        f"{user}\n\n【重要】上一次输出不是合法 JSON，"
                        "请只输出 JSON，不要任何解释文字。"
                    )
        raise RuntimeError(f"文字模型多次未返回合法 JSON: {last_error!s}")


# ── 点评语气指导与日报分析提示词 ────────────────────────────────

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
    """取指定风格的点评语气指导；未知风格回落到毒舌锐评。"""
    key = (style or "roast").strip().lower()
    if key not in _STYLE_GUIDES:
        logger.warning(f"[群日报] 未知提示词风格 {style!r}，回落到 roast")
        key = "roast"
    return _STYLE_GUIDES[key]


def _custom_prompt(config: Config) -> str:
    """拼装自定义提示词；没配则为空串。"""
    text = (config.gdrq_prompt_override or "").strip()
    return f"\n【额外要求】\n{text}\n" if text else ""


def build_analysis_prompt(
    stats: ChatStats,
    chat_lines: list[str],
    code_to_name: dict[str, str],
    config: Config,
    window_label: str = "今日",
) -> tuple[str, str]:
    """构造结构化分析用的系统提示词与用户内容。

    ``gdrq_enable_quotes`` / ``gdrq_enable_quality`` 关闭时，对应字段直接
    从 JSON 结构与硬性要求里移除。
    """
    system = f"""你是群聊分析专家，负责从群聊记录中提取结构化信息。
请严格只输出一个 JSON 对象，不要输出任何解释文字，也不要用 ``` 包裹。

JSON 结构：
{{
  "summary": "3-4 句话的整体总结",
  "topics": [
    {{"title": "话题标题", "detail": "详细描述讨论的起因、经过、不同观点与结论", "users": ["000", "001"]}}
  ],"""

    if config.gdrq_enable_quotes:
        system += """
  "quotes": [
    {"text": "精彩发言原文", "user": "000", "reason": "为什么这句精彩"}
  ],"""

    system += """
  "keywords": ["热词1", "热词2"],
  "mood": "整体氛围的简短评价\""""

    if config.gdrq_enable_quality:
        system += """,
  "quality": {
    "title": "今日群聊主题",
    "subtitle": "副标题",
    "dimensions": [
      {"name": "抽象维度名", "percentage": 35, "comment": "该维度的犀利点评"}
    ],
    "summary": "一句总结性的金句"
  },"""

    system += """
  "titles": [
    {"user": "000", "title": "给他起的称号", "reason": "为什么配得上这个称号"}
  ]
}

硬性要求：
1. 聊天记录中的用户使用代号（000、001、002 这样的三位十六进制），输出时必须原样保留代号，禁止替换成昵称 或写成「张三（000）」。
2. topics 最多 """ + str(config.gdrq_max_topics) + """ 个，哪怕某个话题只有两三个人聊了几句也要单独列出。"""

    if config.gdrq_enable_quotes:
        system += (
            "\n3. quotes 最多 "
            + str(config.gdrq_max_quotes)
            + " 条，text 必须是聊天记录中出现过的原文，不要编造。"
        )

    system += """
4. keywords 给出 6-10 个真正有语义的词或短语（专有名词、产品、技术栈、梗），
   必须是完整可读的词，不要出现「务器」「器部」这类被切碎的碎片。
5. 只输出 JSON，不要 markdown 符号。
6. 所有中文内容不要使用 markdown 语法。
7. titles 给 """ + str(config.gdrq_max_titles) + """ 个发言有特色的用户起称号，风格可以调侃但不要冒犯或带人身攻击。"""

    if config.gdrq_enable_quality:
        system += """
8. quality 是「聊天质量锐评」，按下面的维度划分要求产出。

【quality 维度划分要求】
- 划分 3-6 个**高层级、抽象、泛化**的维度（如：就业焦虑、生涯规划、技术方案研究、情感树洞、无意义水群）。
- **严禁在维度名称（name）中出现任何具体的人名、群名、项目名、具体报错或细碎事件点。名称必须高度抽象且 2-6 个字。**
- 每个维度给一个百分比，**总和不超过 100%**。
- 具体的事件、梗、吐槽细节全部放进 comment，不要放进 name。
- title/subtitle 是本次报告的主题标题与副标题，summary 是一句全群表现总结。"""

    system += f"\n\n{_style_guide(config.gdrq_prompt_style)}\n{_custom_prompt(config)}"

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

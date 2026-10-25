"""文字总结模型客户端与漫画分镜提示词。

文字模型部分（``gdr_text_*``）兼容两种协议：

- base_url 含 ``googleapis.com`` 时走 Gemini ``generateContent``，
  系统提示词放 ``systemInstruction``——contents 里连续两个 user 轮次会被
  Gemini 以 400 拒绝（"multiturn requests alternate between user and model"）；
- 否则走 OpenAI 兼容的 ``/chat/completions``。

响应解析按上游 v5.8.4 的经验加固：

- 剥离 ``<think>…</think>`` 推理块——不少中转会把推理模型的思考原文拼进
  ``content``，直接透传会污染下游（漫画提示词曾因此完全跑偏）；
- Gemini 响应跳过 ``thought: true`` 的思考 parts（上游 #265 的同款处理）；
- ``content`` 为空时回落 ``reasoning_content``。

漫画分镜提示词与 ``scene`` 解析按上游 ``ComicStoryboardAnalyzer`` 移植：
每个话题强制一格、气泡中文台词 + 底部字幕条 + Background Context，输出
结构化 JSON（``scene`` 字段），解析有校验、失败有重试。
"""

from __future__ import annotations

import json
import re
from string import Template
from typing import Any

import httpx
from nonebot.log import logger

from .config import Config
from .stats import ChatStats

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """剥离模型/中转内联的 ``<think>`` 推理块。

    兼容三种形态：

    - 成对的 ``<think>…</think>``：整段删除；
    - 未闭合的 ``<think>``（输出被截断在思考中途）：从 ``<think>`` 起全部丢弃；
    - 孤立的 ``</think>``（部分中转只发闭合标签）：丢弃标签及其之前的全部内容。

    Args:
        text: 模型原始输出。

    Returns:
        去掉推理块后的文本；无推理块时原样返回。
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

    @property
    def _is_gemini(self) -> bool:
        return "googleapis.com" in self._config.gdr_text_base_url

    def _build_request(
        self, key: str, system: str, user: str
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """构造 URL、请求头与请求体，自动适配两种协议。"""
        cfg = self._config
        headers = {"Content-Type": "application/json"}

        if self._is_gemini:
            url = f"{cfg.gdr_text_base_url}/models/{cfg.gdr_text_model}:generateContent?key={key}"
            payload: dict[str, Any] = {
                # system 必须走 systemInstruction：contents 中连续两个 user
                # 轮次会被 Gemini 拒绝（400 multiturn requests alternate）
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"parts": [{"text": user}], "role": "user"}],
            }
            # 仅在显式配置时限制输出：推理类中转把思考 token 也计入该额度，
            # 默认发 5000 会把长 JSON（12 话题的日报分析约 8k+ 字）硬截断
            if cfg.gdr_max_tokens > 0:
                payload["generationConfig"] = {
                    "maxOutputTokens": cfg.gdr_max_tokens
                }
            return url, headers, payload

        headers["Authorization"] = f"Bearer {key}"
        url = f"{cfg.gdr_text_base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": cfg.gdr_text_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if cfg.gdr_max_tokens > 0:
            payload["max_tokens"] = cfg.gdr_max_tokens
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
            # 跳过 thought: true 的思考 parts（上游 #265 同款处理）
            kept = [
                str(p.get("text") or "")
                for p in parts
                if isinstance(p, dict) and p.get("thought") is not True
            ]
            return strip_reasoning("".join(kept))

        return ""

    async def complete(self, system: str, user: str) -> str:
        """调用文字模型并返回文本。

        Args:
            system: 系统提示词。
            user: 用户内容（通常是压缩后的聊天记录）。

        Returns:
            模型输出文本（已剥离思考块）。

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

            text = self._pick_text(body)
            if text.strip():
                return text
            raise RuntimeError(f"文字模型返回了空内容或未知结构: {list(body.keys())}")

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


# ── 漫画分镜提示词（按上游 ComicStoryboardAnalyzer 移植）──────────
#
# 与旧版「一段英文四格提示词」的关键差异：
# 1. 每个话题强制一格（Panel 1..N 不许裁减遗漏），画面与群聊强绑定；
# 2. 气泡里显式渲染中文台词（≤15 字），底部字幕条渲染话题短标题（≤30 字），
#    即使画风通用，读者也能一眼对上群聊内容；
# 3. 话题详情作为 Background Context 附在每格，帮助生图模型理解剧情，
#    同时严禁渲染成长文；
# 4. 输出结构化 JSON 的 scene 字段，解析有校验、失败有重试，
#    不再把模型的自由文本原样丢给生图 API。

DEFAULT_STORYBOARD_PROMPT = (
    "你是一个资深的漫画分镜师与 AI 绘画提示词专家。\n"
    "请根据以下给出的【群聊每日核心话题列表】，将其改编并设计为一个精彩的多格连环漫画（Comic Strip）全景视觉画图提示词 (Prompt)。\n\n"
    "【核心视觉、台词与双层排版规则】：\n"
    "1. 【全话题必须覆盖（共 ${topic_count} 个分格）】：给出的待创作核心话题列表中共有 ${topic_count} 个话题，你必须为每一个话题分别设计一个分格 Panel（即 Panel 1 到 Panel ${topic_count}），绝对不许随意裁减、挑选或遗漏任何一个话题！\n"
    "2. 【口语化台词改编】：绝对严禁将报告分析总结原文直接放进对话框！请将每个话题的事件提炼改编为一句角色在漫画中的生动台词或吐槽，【每条台词控制在 15 个汉字以内】（例如：\u201c呜呜！家里云又断网了啦！\u201d、\u201c萝卜子才没有降智！那都是Gemini的错！\u201d）。\n"
    "3. 【精美双层文字排版（气泡 + 可爱旁白字幕条）】：\n"
    "   - 【角色的气泡】：在描述英文 Prompt 时，指定样式为“adorable kawaii anime speech bubble, soft rounded cloud-like shape, cute pointer tail pointing to the speaker”。\n"
    "   - 【分格底部的事件旁白条】：将每个话题概括为生动精炼的短标题（控制在 30 字以内，严禁带有“【事件】”字样），在 Prompt 中指定样式为“cute pastel-colored kawaii caption strip at the bottom of the panel with soft rounded corners”，严禁死板白框！\n"
    "4. 【中文文本显式渲染】：画面整体构图与场景描述使用英文，但气泡与底部旁白条内渲染的中文必须显式指定（指令格式：containing a speech bubble with exact Chinese text \u201c人设吐槽台词\u201d 以及 and a cute caption strip at bottom with exact Chinese text \u201c精炼话题短标题\u201d），绝对禁止将中文翻译成英文！\n"
    "5. 【话题内容直传与文字渲染约束】：在生成传给生图 LLM 的英文提示词 (scene) 时，对于每一个分格，除了描述具体的视觉画面外，你必须将该话题的【完整详情（翻译为英文）】作为 Background Context 附加在该分格的提示词中，帮助生图模型理解剧情。但同时，必须极其强烈地警告生图模型：“绝对禁止将长篇上下文写在画面上，仅允许渲染短标题字幕条和气泡台词！”（示例：Background Context: [Details]. STRICT RULE: DO NOT render the background context text! ONLY render the exact Chinese text in the bubble and caption strip!）。\n"
    "6. 【核心角色强制全覆盖】：在提示词中必须明确要求并描述，每一个分格 (Panel) 都必须无一例外地出现同一位固定主角（自创一名统一的主角并给出具体外貌特征，例如 1girl, short silver hair, blue eyes, white dress），保持整篇连环画的主角绝对连贯！\n\n"
    "【待创作的群聊核心话题列表】：\n${chat_content}\n\n"
    "【输出格式与正反例规范 (GOOD & BAD EXAMPLES)】：\n"
    "⚠️ 核心要求：AI 绘图引擎会直接读取 \u201cscene\u201d 字段作为唯一生图指令。你必须将全局角色总设以及 Panel 1 到 Panel ${topic_count} 的每一格完整描述全部写入 \u201cscene\u201d 字符串中！\n\n"
    "✅ 正确示范 (GOOD EXAMPLE - 必须严格遵循此格式输出纯 JSON)：\n"
    "{\n"
    "  \u201cscene\u201d: \u201cA ${topic_count}-panel comic strip featuring [protagonist description] observing a chaotic QQ group chat.\\n\\nPanel 1: [Visual action] containing a speech bubble with exact Chinese text \u201c[吐槽台词 15字内]\u201d and a cute pastel caption strip at bottom with exact Chinese text \u201c[精炼短标题]\u201d. Background Context: [English Details]. STRICT RULE: DO NOT render the background context text!\\n\\nPanel 2: [Visual action] containing a speech bubble with exact Chinese text \u201c[吐槽台词 15字内]\u201d and a cute pastel caption strip at bottom with exact Chinese text \u201c[精炼短标题]\u201d. Background Context: [English Details]. STRICT RULE: DO NOT render the background context text!\\n\\n... (必须依次写完 Panel 1 到 Panel ${topic_count})\u201d\n"
    "}\n\n"
    "❌ 错误示范 (BAD EXAMPLE - 严禁以下行为)：\n"
    "1. 严禁把 scene 写成一句简短概括，却把具体话题藏在其他字段中（生图模型直接读取 scene，必须全内联写入 scene）！\n"
    "2. 严禁丢失气泡或字幕条里的 exact Chinese text 显式中文台词与短标题！\n"
    "3. 严禁在 JSON 之外输出任何多余的 Markdown 或闲聊文字！\n"
)

#: ``gdr_comic_prompt`` 保留为画面风格后缀，追加在分镜规范之后。
_STYLE_SUFFIX = "\n【画面风格要求】\n{style}\n"


def build_storyboard_prompt(
    topics: list[dict[str, Any]], config: Config
) -> tuple[str, str]:
    """构造漫画分镜提示词。

    Args:
        topics: 结构化话题列表（``title`` / ``detail``）。
        config: 插件配置。

    Returns:
        ``(系统提示词, 用户内容)``。
    """
    valid = [
        (str(t.get("title") or "").strip(), str(t.get("detail") or "").strip())
        for t in topics
        if isinstance(t, dict) and str(t.get("title") or "").strip()
    ]
    topic_count = len(valid)
    chat_content = "\n".join(
        f"{i + 1}. 话题: {t}\n   详情: {d}" for i, (t, d) in enumerate(valid)
    )

    system = Template(DEFAULT_STORYBOARD_PROMPT).safe_substitute(
        chat_content=chat_content, topic_count=topic_count
    )
    style = (config.gdr_comic_prompt or "").strip()
    if style:
        system += _STYLE_SUFFIX.format(style=style)

    user = (
        f"请根据以上 {topic_count} 个群聊话题输出漫画分镜 JSON，"
        "只输出一个 JSON 对象（仅含 scene 字段），不要任何解释文字。"
    )
    return system, user


def _extract_text_chunks(obj: object, depth: int = 0) -> list[str]:
    """递归提取任意多层级 JSON 结构中的所有文本叶子节点（上游同款）。"""
    if depth > 8 or obj is None:
        return []
    if isinstance(obj, str):
        text = obj.strip()
        return [text] if text else []
    chunks: list[str] = []
    if isinstance(obj, list):
        for item in obj:
            chunks.extend(_extract_text_chunks(item, depth + 1))
    elif isinstance(obj, dict):
        for val in obj.values():
            chunks.extend(_extract_text_chunks(val, depth + 1))
    return chunks


def _consolidate_scene_text(data: object) -> str | None:
    """将任意多层级 JSON 结构模糊整合为统一的单段 scene 提示词（上游同款）。"""
    if not data:
        return None
    raw_chunks = _extract_text_chunks(data)
    if not raw_chunks:
        return None

    unique_chunks: list[str] = []
    for chunk in raw_chunks:
        if len(chunk) < 3:
            continue
        if any(
            chunk == existing or (len(chunk) < len(existing) and chunk in existing)
            for existing in unique_chunks
        ):
            continue
        superseded = [
            existing
            for existing in unique_chunks
            if existing != chunk and existing in chunk
        ]
        for s in superseded:
            unique_chunks.remove(s)
        unique_chunks.append(chunk)
    return "\n\n".join(unique_chunks) if unique_chunks else None


def extract_scene(result_text: str) -> str | None:
    """从文字模型输出中提取最终生图提示词 ``scene``。

    解析顺序（上游 ``parse_structured_response`` 主线）：

    1. JSON 对象 → 优先 ``scene`` 字段，其余字段内容做包含去重后合并；
    2. 顶层 JSON 数组 → 递归整合全部文本；
    3. 正则直接抠 ``"scene": "..."``；
    4. 都失败时返回 ``None``（调用方应重试而不是硬塞给生图模型）。

    Args:
        result_text: 模型原始输出（已经过思考块剥离）。

    Returns:
        scene 提示词；提取失败时返回 ``None``。
    """
    text = (result_text or "").strip()
    if not text:
        return None

    # 1. JSON 对象主线
    try:
        data = _parse_json(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        scene_val = data.get("scene")
        scene_str = scene_val.strip() if isinstance(scene_val, str) else ""
        other_data = {k: v for k, v in data.items() if k != "scene"}
        other_text = _consolidate_scene_text(other_data) if other_data else None

        if scene_str and not other_text:
            return scene_str
        if scene_str and other_text:
            if other_text in scene_str or (
                "panel 1" in scene_str.lower() and "panel 2" in scene_str.lower()
            ):
                return scene_str
            return f"{scene_str}\n\n{other_text}"
        if other_text:
            return other_text
        return None

    # 2. 顶层 JSON 数组
    try:
        list_data = json.loads(_extract_json(text))
        if isinstance(list_data, list) and list_data:
            return _consolidate_scene_text(list_data)
    except (json.JSONDecodeError, ValueError):
        pass

    # 3. 正则兜底
    match = re.search(r'"scene"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.DOTALL)
    if match:
        return match.group(1).replace('\\"', '"').replace("\\n", "\n")

    return None


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

    ``gdr_enable_quotes`` / ``gdr_enable_quality`` 关闭时，对应字段直接从
    JSON 结构与硬性要求里移除，不再只是「配置了不生效」的死开关。

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
  ],"""

    if config.gdr_enable_quotes:
        system += """
  "quotes": [
    {"text": "精彩发言原文", "user": "000", "reason": "为什么这句精彩"}
  ],"""

    system += """
  "keywords": ["热词1", "热词2"],
  "mood": "整体氛围的简短评价\""""

    if config.gdr_enable_quality:
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
2. topics 最多 """ + str(config.gdr_max_topics) + """ 个，哪怕某个话题只有两三个人聊了几句也要单独列出。"""

    if config.gdr_enable_quotes:
        system += (
            "\n3. quotes 最多 "
            + str(config.gdr_max_quotes)
            + " 条，text 必须是聊天记录中出现过的原文，不要编造。"
        )

    system += """
4. keywords 给出 6-10 个真正有语义的词或短语（专有名词、产品、技术栈、梗），
   必须是完整可读的词，不要出现「务器」「器部」这类被切碎的碎片。
5. 只输出 JSON，不要 markdown 符号。
6. 所有中文内容不要使用 markdown 语法。
7. titles 给 """ + str(config.gdr_max_titles) + """ 个发言有特色的用户起称号，风格可以调侃但不要冒犯或带人身攻击。"""

    if config.gdr_enable_quality:
        system += """
8. quality 是「聊天质量锐评」，按下面的维度划分要求产出。

【quality 维度划分要求】
- 划分 3-6 个**高层级、抽象、泛化**的维度（如：就业焦虑、生涯规划、技术方案研究、情感树洞、无意义水群）。
- **严禁在维度名称（name）中出现任何具体的人名、群名、项目名、具体报错或细碎事件点。名称必须高度抽象且 2-6 个字。**
- 每个维度给一个百分比，**总和不超过 100%**。
- 具体的事件、梗、吐槽细节全部放进 comment，不要放进 name。
- title/subtitle 是本次报告的主题标题与副标题，summary 是一句全群表现总结。"""

    system += f"\n\n{_style_guide(config.gdr_prompt_style)}\n{_custom_prompt(config)}"

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

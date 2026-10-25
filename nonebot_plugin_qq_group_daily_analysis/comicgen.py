"""漫画绘图客户端（按上游 v5.8.4 绘图栈重新适配）。

旧版 ``imagegen.py`` 只会裸调 OpenAI 兼容的 ``/images/generations``，
提示词与响应都无校验，且 url 形式的响应在已关闭的 httpx client 上下载
必然失败。本模块按上游 ``DrawingClient`` 的经验重写，要点：

- **多协议适配**（``gdr_image_provider``）：

  - ``doubao``  火山方舟 Seedream（默认，含 optimize_prompt 控制）；
  - ``images``  OpenAI 兼容 ``/images/generations``；
  - ``google``  Gemini ``generateContent``（``responseModalities: IMAGE``，
    上游 #265：只取最终图片，跳过 ``thought: true`` 的思考图片）；
  - ``chat``    OpenAI 兼容 ``/chat/completions`` 多模态生图；
  - ``grok``    x.ai ``/images/generations``。

- **图片提取**：递归收集 b64 / data URI / markdown 图链 / url 候选，
  按可靠性依次尝试，并对字节做图片签名校验（拒绝把 HTML/JSON 错误页
  当图片发群里）；url 下载与请求共用**同一个未关闭的** client。
- **重试**：网络/内容异常按 ``gdr_image_retries`` 重试；429 自动轮换 Key。

与生图文字提示词的配合见 :mod:`llm.build_storyboard_prompt`。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
from math import gcd
from typing import Any

import httpx
from nonebot.log import logger

from .config import Config

# OpenAI 的 gpt-image-1 / dall-e-3 原生规格上限（1792），都不含 2048x2048，
# 所以 ups 模式要靠本地放大补齐。其他服务商（如火山方舟 Seedream）本身支持
# 大尺寸，别用这张表去降级，走 native 模式原样发即可。
DEFAULT_NATIVE_SIZES: tuple[tuple[int, int], ...] = (
    (1024, 1024),
    (1024, 1536),
    (1536, 1024),
    (1792, 1024),
    (1024, 1792),
)

MAX_IMAGE_BYTES = 100 * 1024 * 1024


def parse_size(value: str) -> tuple[int, int] | None:
    """解析 ``WxH`` 尺寸字符串。

    Args:
        value: 形如 ``2048x2048`` 的字符串，大小写不限。

    Returns:
        ``(宽, 高)``；格式非法时返回 ``None``。
    """
    try:
        w, h = str(value).lower().split("x")
        width, height = int(w), int(h)
    except (ValueError, AttributeError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def parse_native_sizes(value: str) -> list[tuple[int, int]]:
    """解析逗号分隔的原生规格列表。

    Args:
        value: 形如 ``1024x1024,1024x1536`` 的字符串。

    Returns:
        解析成功的规格列表；全不合法时回落到 :data:`DEFAULT_NATIVE_SIZES`。
    """
    sizes = [s for s in (parse_size(v) for v in str(value or "").split(",")) if s]
    if not sizes:
        logger.warning(
            f"[群日报] gdr_image_native_sizes {value!r} 解析不出任何合法尺寸，"
            f"回落到默认值"
        )
        return list(DEFAULT_NATIVE_SIZES)
    return sizes


def snap_to_native(
    target: tuple[int, int], native_sizes: list[tuple[int, int]]
) -> tuple[int, int]:
    """挑一个宽高比最接近目标的原生规格。

    **必须先按宽高比筛，只看面积会翻车**：目标 2048x2048 若按面积匹配会落到
    1792x1024（横版 1.75:1），再拉成正方形就是严重变形。同理竖版漫画若匹配到
    横版规格，出来的图会被拉得又宽又扁。

    先取宽高比与目标最接近的一组（同组内可能有多个），再在这组里挑面积够用的
    最小规格；都不够就取这组里最大的，放大倍率最小。

    Args:
        target: 期望尺寸。
        native_sizes: 可用的原生规格。

    Returns:
        原生规格。
    """
    want_ratio = target[0] / target[1]
    need = target[0] * target[1]

    best = min(abs(s[0] / s[1] - want_ratio) for s in native_sizes)
    same_ratio = [s for s in native_sizes if abs(s[0] / s[1] - want_ratio) - best < 1e-9]

    big_enough = [s for s in same_ratio if s[0] * s[1] >= need]
    if big_enough:
        return min(big_enough, key=lambda s: s[0] * s[1])
    return max(same_ratio, key=lambda s: s[0] * s[1])


def _resize(data: bytes, size: tuple[int, int]) -> bytes:
    """把图片缩放到指定尺寸。

    Args:
        data: 原图字节。
        size: 目标 ``(宽, 高)``。

    Returns:
        缩放后的 PNG 字节；Pillow 不可用时原样返回。

    Raises:
        RuntimeError: 图片无法解码。
    """
    try:
        import io

        from PIL import Image
    except ImportError:
        logger.warning("[群日报] 未安装 Pillow，跳过放大（需 pip install pillow）")
        return data

    try:
        with Image.open(io.BytesIO(data)) as img:
            out = img.resize(size, Image.LANCZOS)
            buf = io.BytesIO()
            # 原图是 JPEG 就保持 JPEG，否则存 PNG；带透明通道的强制 PNG
            fmt = (
                "PNG"
                if (out.mode in ("RGBA", "LA", "P") or img.format != "JPEG")
                else "JPEG"
            )
            out.save(buf, format=fmt)
            return buf.getvalue()
    except Exception as e:
        raise RuntimeError(f"放大失败: {e!s}") from e


def build_target_url(raw_url: str, provider: str) -> str:
    """按协议智能补全用户配置的 API URL（上游 ``_build_target_url`` 移植）。

    Args:
        raw_url: 配置里的 base_url，可为空。
        provider: 协议名（doubao/images/google/chat/grok）。

    Returns:
        完整接口地址。
    """
    url = (raw_url or "").strip().rstrip("/")

    if provider == "doubao":
        if not url or "volces.com" not in url:
            url = url or "https://ark.cn-beijing.volces.com"
        if url.endswith("/images/generations"):
            return url
        if url.endswith(("/v1", "/v3")):
            return f"{url}/images/generations"
        # 裸域名（如 https://ark.cn-beijing.volces.com）补官方路径
        if "volces.com" in url and url.count("/") == 2:
            return f"{url}/api/v3/images/generations"
        if "volces.com" in url and "/api/v3" in url:
            return f"{url}/images/generations"
        return f"{url}/v1/images/generations"

    if provider == "images":
        if not url:
            url = "https://api.openai.com/v1"
        if url.endswith("/images/generations"):
            return url
        if url.endswith("/v1"):
            return f"{url}/images/generations"
        if "/v1/" in url:
            return url if "images" in url else f"{url}/images/generations"
        return f"{url}/v1/images/generations"

    if provider == "chat":
        if not url:
            url = "https://api.openai.com/v1"
        if url.endswith("/chat/completions"):
            return url
        if url.endswith("/v1"):
            return f"{url}/chat/completions"
        if "/v1/" in url:
            return url if "chat" in url else f"{url}/chat/completions"
        return f"{url}/v1/chat/completions"

    if provider == "google":
        if not url:
            url = "https://generativelanguage.googleapis.com/v1beta"
        return url

    if provider == "grok":
        if not url:
            url = "https://api.x.ai"
        if url.endswith(("/images/generations", "/images/edits")):
            return url
        if url.endswith("/v1"):
            return f"{url}/images/generations"
        if "/v1/" in url:
            return url if "/images/" in url else f"{url}/images/generations"
        return f"{url}/v1/images/generations"

    raise ValueError(f"不支持的生图协议: {provider}")


def validate_image_bytes(data: bytes) -> None:
    """拒绝 HTML、JSON 等非图片响应（上游 ``validate_image_bytes`` 移植）。

    Args:
        data: 待校验字节。

    Raises:
        ValueError: 内容不是受支持的图片格式。
    """
    if not data:
        raise ValueError("响应内容为空")
    probe = data[:32]
    is_webp = len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    is_avif = len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {b"avif", b"avis"}
    is_jp2 = len(data) >= 12 and data[4:8] == b"ftyp" and b"jp2" in data[8:12]
    signatures = (
        b"\x89PNG\r\n\x1a\n",
        b"\xff\xd8\xff",
        b"GIF87a",
        b"GIF89a",
        b"BM",
        b"II*\x00",
        b"MM\x00*",
        b"\x00\x00\x00\x0cjP  ",
    )
    starts_with_sig = any(0 <= probe.find(sig) < 4 for sig in signatures)
    if starts_with_sig or is_webp or is_avif or is_jp2:
        return
    head = data[:64].decode("ascii", errors="ignore").lower()
    if head.startswith(("<!doctype", "<html", "{", "[")):
        raise ValueError("响应内容不是图片（检测到 HTML/JSON）")
    raise ValueError("响应内容不是支持的图片格式")


def decode_base64_image(encoded: str) -> bytes:
    """解码标准或 URL-safe Base64，并确认结果是图片（上游同款）。

    Args:
        encoded: Base64 文本。

    Returns:
        图片字节。

    Raises:
        ValueError: 非法 Base64 或不是图片。
    """
    normalized = re.sub(r"\s+", "", encoded).replace("-", "+").replace("_", "/")
    try:
        normalized.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("Base64 候选内容含非 ASCII 字符，跳过") from exc
    if len(normalized) > MAX_IMAGE_BYTES * 4 // 3 + 4:
        raise ValueError("Base64 图片负载超过 100MB")
    normalized += "=" * (-len(normalized) % 4)
    decoded = base64.b64decode(normalized, validate=True)
    validate_image_bytes(decoded)
    return decoded


def decode_data_uri(data_uri: str) -> bytes:
    """解码 ``data:image/*;base64,...`` 形式的 Data URI。

    Args:
        data_uri: Data URI 字符串。

    Returns:
        图片字节。

    Raises:
        ValueError: 不是 Base64 图片 Data URI。
    """
    header, encoded = data_uri.split(",", 1)
    if ";base64" not in header.lower():
        raise ValueError("Data URI 不是 Base64 图片")
    return decode_base64_image(encoded)


_DATA_URI_RE = re.compile(
    r"data:image/[^\s,;]+(?:;[^\s,;]+)*;base64,[A-Za-z0-9+/=_-]+", re.IGNORECASE
)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^\s<>\"')\]]+)\)")
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


async def extract_image(client: httpx.AsyncClient, data: object) -> bytes | None:
    """从任意绘图响应中递归提取图片（上游 ``extract_image_from_response`` 移植）。

    Google ``candidates`` 响应只接受**最终图片**：跳过 ``thought: true`` 的
    思考图片与中间图片（上游 #265）。其余响应按可靠性依次尝试
    b64 → image 字段 → data URI / markdown 图链 → 正文 URL → 兜底 URL；
    url 候选用**当前仍处于打开状态的** ``client`` 下载。

    Args:
        client: 发起请求时使用的 httpx 客户端（仍打开）。
        data: 接口响应体。

    Returns:
        图片字节；无可用图片时返回 ``None``。
    """
    if isinstance(data, dict) and "candidates" in data:
        candidates = data["candidates"]
        for candidate in candidates if isinstance(candidates, list) else []:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            parts = content.get("parts") if isinstance(content, dict) else None
            for part in parts if isinstance(parts, list) else []:
                if not isinstance(part, dict) or part.get("thought") is True:
                    continue
                inline_data = part.get("inlineData", part.get("inline_data"))
                if not isinstance(inline_data, dict):
                    continue
                mime = inline_data.get("mimeType", inline_data.get("mime_type"))
                encoded_image = inline_data.get("data")
                if not isinstance(mime, str) or not mime.lower().startswith("image/"):
                    continue
                if not isinstance(encoded_image, str) or not encoded_image.strip():
                    continue
                try:
                    return decode_base64_image(encoded_image)
                except (ValueError, binascii.Error) as exc:
                    logger.debug(f"[群日报] 跳过无效 Gemini 最终图片: {exc}")
        return None

    encoded: list[tuple[str, str]] = []
    image_fields: list[tuple[str, str]] = []
    content_images: list[tuple[str, str]] = []
    content_urls: list[tuple[str, str]] = []
    fallback_urls: list[tuple[str, str]] = []

    def collect(value: object, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            for name, item in value.items():
                collect(item, (*path, name.lower()))
        elif isinstance(value, list):
            for item in value:
                collect(item, path)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return
            key = path[-1] if path else ""
            if key in {"b64_json", "base64"}:
                encoded.append(("base64", text))
                return
            if key in {"image_url", "image"} or (
                key == "url"
                and any(
                    name in {"data", "image", "images", "image_url"}
                    for name in path[:-1]
                )
            ):
                image_fields.append(("value", text))
                return

            for item in _DATA_URI_RE.findall(text):
                content_images.append(("value", item))
            markdown_urls = _MD_IMAGE_RE.findall(text)
            for item in markdown_urls:
                content_images.append(("url", item.rstrip(".,;`")))

            urls = _URL_RE.findall(text)
            md_set = set(markdown_urls)
            target = content_urls if key in {"content", "text"} else fallback_urls
            target.extend(
                ("url", item.rstrip(".,;`")) for item in urls if item not in md_set
            )

            # 长且无 URL 的纯文本大概率是被塞进字符串字段的裸 Base64
            if not _DATA_URI_RE.search(text) and not urls and len(text) >= 100:
                encoded.append(("base64", text))

    collect(data)
    for candidate_type, candidate in (
        encoded + image_fields + content_images + content_urls + fallback_urls
    ):
        try:
            if candidate_type == "url" or candidate.startswith(("http://", "https://")):
                resp = await client.get(candidate)
                resp.raise_for_status()
                image = resp.content
                validate_image_bytes(image)
            elif candidate.startswith("data:image/"):
                image = decode_data_uri(candidate)
            elif candidate.startswith("base64://"):
                image = decode_base64_image(candidate[len("base64://") :])
            else:
                image = decode_base64_image(candidate)
            if image:
                return image
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            logger.warning(f"[群日报] 漫画图片下载失败: {type(exc).__name__}: {exc}")
        except (ValueError, TypeError, binascii.Error) as exc:
            logger.warning(f"[群日报] 跳过无效图片候选: {exc}")
    return None


def _google_image_size(raw: str) -> str:
    """把 WxH 或档位映射到 Gemini imageConfig 认识的 1K/2K/4K。"""
    raw = (raw or "").strip()
    if raw.upper() in {"1K", "2K", "4K"}:
        return raw.upper()
    size = parse_size(raw)
    if size:
        longest = max(size)
        if longest <= 1024:
            return "1K"
        if longest <= 2048:
            return "2K"
        return "4K"
    return "2K"


class ComicImageModel:
    """漫画生图客户端：多协议适配 + Key 轮换 + 重试。"""

    def __init__(self, config: Config) -> None:
        self._config = config

    @property
    def available(self) -> bool:
        """是否配置了可用的 Key。"""
        return bool(self._config.gdr_image_api_keys)

    @property
    def provider(self) -> str:
        """当前协议名。"""
        return self._config.gdr_image_provider

    async def generate(self, prompt: str) -> bytes | None:
        """调用生图接口并返回图片字节。

        Args:
            prompt: 生图提示词（来自 :func:`llm.extract_scene` 的 scene）。

        Returns:
            图片字节；未配置、重试耗尽或全部失败时返回 ``None``。
        """
        cfg = self._config
        if not cfg.gdr_image_api_keys:
            logger.warning("[群日报] 未配置 gdr_image_api_keys，跳过漫画生成")
            return None

        retries = max(0, int(cfg.gdr_image_retries))
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            for key in cfg.gdr_image_api_keys:
                try:
                    # 请求、提取、下载共用同一个 client，且都在 with 块内
                    # 完成——旧版把提取挪到块外，url 形式的响应必挂。
                    async with httpx.AsyncClient(
                        proxy=cfg.gdr_image_proxy or None,
                        timeout=httpx.Timeout(
                            connect=20.0,
                            read=float(cfg.gdr_image_timeout),
                            write=20.0,
                            pool=20.0,
                        ),
                    ) as client:
                        data = await self._request(client, key, prompt)
                        if data is None:
                            raise RuntimeError("生图接口未返回图片")
                        image = await extract_image(client, data)
                        if image is None:
                            raise RuntimeError("生图响应中提取不到图片")
                    return self._maybe_upscale(image)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429:
                        logger.warning("[群日报] 生图 Key 已限流，切换下一个")
                        last_error = e
                        continue
                    last_error = e
                    logger.error(f"[群日报] 生图调用失败: {e!s}")
                except Exception as e:
                    last_error = e
                    logger.error(f"[群日报] 生图调用异常: {e!s}")
            if attempt < retries:
                delay = float(cfg.gdr_image_retry_delay)
                logger.info(
                    f"[群日报] 生图第 {attempt + 1} 次尝试失败，"
                    f"{delay:.0f}s 后重试（共 {retries + 1} 次）"
                )
                await asyncio.sleep(delay)
        logger.error(f"[群日报] 生图所有尝试均失败: {last_error!s}")
        return None

    # ── 尺寸处理 ──────────────────────────────────────────

    def _want_and_native(self) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
        """解析期望尺寸与实际请求的原生尺寸（upscale 模式才做吸附）。"""
        cfg = self._config
        want = parse_size(str(cfg.gdr_image_size or "").strip())
        native = want
        if cfg.gdr_image_size_mode == "upscale" and want is not None:
            native = snap_to_native(want, parse_native_sizes(cfg.gdr_image_native_sizes))
            if native != want:
                logger.info(
                    f"[群日报] 原生规格不支持 {want[0]}x{want[1]}，"
                    f"改用 {native[0]}x{native[1]} 请求后放大"
                )
        return want, native

    def _maybe_upscale(self, image: bytes) -> bytes:
        """按需把原生尺寸的图放大到期望尺寸。"""
        want, native = self._want_and_native()
        if want is None or native is None or native == want:
            return image
        try:
            out = _resize(image, want)
        except RuntimeError as e:
            logger.error(f"[群日报] {e!s}")
            return image  # 放大失败总比整张图丢掉好
        logger.info(
            f"[群日报] 已放大到 {want[0]}x{want[1]}"
            f"（{len(image) // 1024}KB → {len(out) // 1024}KB）"
        )
        return out

    def _size_payload(self) -> str:
        """images/doubao 协议的 size 字段值。"""
        cfg = self._config
        raw = str(cfg.gdr_image_size or "").strip()
        _, native = self._want_and_native()
        if cfg.gdr_image_size_mode == "upscale" and native is not None:
            return f"{native[0]}x{native[1]}"
        return raw

    # ── 各协议请求构造 ────────────────────────────────────

    async def _request(
        self, client: httpx.AsyncClient, key: str, prompt: str
    ) -> object:
        """按配置的协议发起请求，返回响应体。"""
        provider = self.provider
        if provider == "doubao":
            return await self._request_doubao(client, key, prompt)
        if provider == "images":
            return await self._request_images(client, key, prompt)
        if provider == "google":
            return await self._request_google(client, key, prompt)
        if provider == "chat":
            return await self._request_chat(client, key, prompt)
        if provider == "grok":
            return await self._request_grok(client, key, prompt)
        raise ValueError(f"不支持的生图协议: {provider}")

    async def _post_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
    ) -> dict[str, Any]:
        """发 JSON 请求并返回解析后的响应体。"""
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code != 200:
            snippet = resp.text[:500] if resp.text else "(空响应)"
            raise RuntimeError(f"生图 API 请求失败 [HTTP {resp.status_code}]: {snippet}")
        try:
            return resp.json()
        except Exception as e:
            snippet = resp.text[:500] if resp.text else "(空正文)"
            raise RuntimeError(f"生图 API 未返回合法 JSON: {snippet}") from e

    async def _request_doubao(
        self, client: httpx.AsyncClient, key: str, prompt: str
    ) -> dict[str, Any]:
        """火山方舟 Seedream（上游豆包预设移植）。

        与 OpenAI images 协议的差异：支持 ``2K`` 档位尺寸、``watermark``、
        ``optimize_prompt_options``（提示词改写开关——旧版从不发送，服务端
        默认改写是漫画跑偏的嫌疑之一）。
        """
        cfg = self._config
        url = build_target_url(cfg.gdr_image_base_url, "doubao")
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, object] = {
            "model": cfg.gdr_image_model,
            "prompt": prompt,
        }
        size = self._size_payload()
        if size:
            payload["size"] = size
        if cfg.gdr_image_response_format:
            payload["response_format"] = cfg.gdr_image_response_format
        if cfg.gdr_image_output_format:
            payload["output_format"] = cfg.gdr_image_output_format
        if cfg.gdr_image_watermark is not None:
            payload["watermark"] = cfg.gdr_image_watermark
        # 提示词优化：off 显式关闭（改写是漫画内容跑偏的常见来源），
        # fast/standard 显式选择档位；留空交由服务端默认。
        mode = str(cfg.gdr_image_optimize_prompt or "").strip().lower()
        if mode == "off":
            payload["optimize_prompt"] = False
        elif mode in {"fast", "standard"}:
            payload["optimize_prompt_options"] = {"mode": mode}
        payload.update(cfg.gdr_image_extra or {})
        logger.info(
            f"[群日报] 生图请求 [doubao] -> {url} "
            f"(model={cfg.gdr_image_model}, size={size or '默认'}, "
            f"optimize_prompt={mode or '服务端默认'})"
        )
        return await self._post_json(client, url, headers, payload)

    async def _request_images(
        self, client: httpx.AsyncClient, key: str, prompt: str
    ) -> dict[str, Any]:
        """OpenAI 兼容 /images/generations（上游 images 预设精简版）。"""
        cfg = self._config
        url = build_target_url(cfg.gdr_image_base_url, "images")
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, object] = {
            "model": cfg.gdr_image_model,
            "prompt": prompt,
            "n": 1,
        }
        size = self._size_payload()
        if size:
            payload["size"] = size
        quality = str(cfg.gdr_image_quality or "").strip().lower()
        if quality:
            payload["quality"] = quality
        if cfg.gdr_image_response_format:
            payload["response_format"] = cfg.gdr_image_response_format
        if cfg.gdr_image_output_format:
            payload["output_format"] = cfg.gdr_image_output_format
        payload.update(cfg.gdr_image_extra or {})
        logger.info(
            f"[群日报] 生图请求 [images] -> {url} "
            f"(model={cfg.gdr_image_model}, size={size or '默认'})"
        )
        return await self._post_json(client, url, headers, payload)

    async def _request_google(
        self, client: httpx.AsyncClient, key: str, prompt: str
    ) -> dict[str, Any]:
        """Google Gemini generateContent 原生接口（上游 google 预设移植）。"""
        cfg = self._config
        base = build_target_url(cfg.gdr_image_base_url, "google")
        url = (
            base
            if ":generateContent" in base
            else f"{base.rstrip('/')}/models/{cfg.gdr_image_model}:generateContent"
        )
        headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
        payload: dict[str, object] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {
                    "image_size": _google_image_size(cfg.gdr_image_size),
                    "aspect_ratio": str(cfg.gdr_image_aspect_ratio or "16:9"),
                },
            },
        }
        payload.update(cfg.gdr_image_extra or {})
        logger.info(
            f"[群日报] 生图请求 [google] -> {url} "
            f"(model={cfg.gdr_image_model}, "
            f"size={_google_image_size(cfg.gdr_image_size)}, "
            f"aspect_ratio={cfg.gdr_image_aspect_ratio})"
        )
        return await self._post_json(client, url, headers, payload)

    async def _request_chat(
        self, client: httpx.AsyncClient, key: str, prompt: str
    ) -> dict[str, Any]:
        """OpenAI 兼容 /chat/completions 多模态生图（上游 chat 预设移植）。

        尺寸/横竖要求显式追加在提示词末尾，防止 chat 协议的模型忽略。
        """
        cfg = self._config
        url = build_target_url(cfg.gdr_image_base_url, "chat")
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        size = self._size_payload() or "1024x1024"
        parsed = parse_size(size)
        if parsed:
            width, height = parsed
            divisor = gcd(width, height)
            ratio = f"{width // divisor}:{height // divisor}"
            orientation = (
                "Horizontal Landscape Orientation"
                if width > height
                else "Vertical Portrait Orientation"
                if width < height
                else "Square Orientation"
            )
            full_prompt = (
                f"{prompt}\n\n[Image Layout & Spec Requirements: Aspect Ratio "
                f"{ratio}, Resolution {size}, {orientation}]"
            )
        else:
            full_prompt = prompt
        payload: dict[str, object] = {
            "model": cfg.gdr_image_model,
            "messages": [{"role": "user", "content": full_prompt}],
        }
        payload.update(cfg.gdr_image_extra or {})
        logger.info(f"[群日报] 生图请求 [chat] -> {url} (model={cfg.gdr_image_model})")
        return await self._post_json(client, url, headers, payload)

    async def _request_grok(
        self, client: httpx.AsyncClient, key: str, prompt: str
    ) -> dict[str, Any]:
        """xAI Grok /images/generations（上游 grok/xai 预设精简版）。"""
        cfg = self._config
        url = build_target_url(cfg.gdr_image_base_url, "grok")
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        raw = str(cfg.gdr_image_size or "").strip()
        resolution = raw.lower() if raw.upper() in {"1K", "2K"} else "2k"
        payload: dict[str, object] = {
            "model": cfg.gdr_image_model,
            "prompt": prompt,
            "n": 1,
            "response_format": cfg.gdr_image_response_format or "b64_json",
            "resolution": resolution,
        }
        payload.update(cfg.gdr_image_extra or {})
        logger.info(
            f"[群日报] 生图请求 [grok] -> {url} "
            f"(model={cfg.gdr_image_model}, resolution={resolution})"
        )
        return await self._post_json(client, url, headers, payload)

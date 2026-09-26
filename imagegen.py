"""生图模型客户端（漫画用）。

与文字总结模型完全独立配置（``gdr_image_*``）。
默认走 OpenAI 兼容的 ``/images/generations``，返回 b64_json 或 url 两种形式都支持。
"""

from __future__ import annotations

import base64

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


def snap_to_native(target: tuple[int, int], native_sizes: list[tuple[int, int]]) -> tuple[int, int]:
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

    # 宽高比差异作为主排序键
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
            fmt = "PNG" if (out.mode in ("RGBA", "LA", "P") or img.format != "JPEG") else "JPEG"
            out.save(buf, format=fmt)
            return buf.getvalue()
    except Exception as e:
        raise RuntimeError(f"放大失败: {e!s}") from e


def _endpoint(base_url: str) -> str:
    """拼出 ``/images/generations`` 接口地址。

    base_url 既可以只写到 ``/v1``，也可以直接写完整的
    ``.../images/generations``（火山方舟的文档就是给完整地址）。两种都得认，
    否则后者会被拼成 ``.../images/generations/images/generations`` 直接 404。

    Args:
        base_url: 配置里的 base_url。

    Returns:
        完整接口地址。
    """
    base = str(base_url or "").rstrip("/")
    if not base:
        return ""
    return base if base.endswith("/images/generations") else f"{base}/images/generations"


class ImageModel:
    """生图模型调用封装。"""

    def __init__(self, config: Config) -> None:
        self._config = config

    @property
    def available(self) -> bool:
        """是否配置了可用的 Key。"""
        return bool(self._config.gdr_image_api_keys)

    async def generate(self, prompt: str) -> bytes | None:
        """调用生图模型并返回图片字节。

        Args:
            prompt: 绘图提示词。

        Returns:
            图片字节；调用失败或未配置时返回 ``None``。
        """
        cfg = self._config
        if not cfg.gdr_image_api_keys:
            logger.warning("[群日报] 未配置 gdr_image_api_keys，跳过漫画生成")
            return None

        url = _endpoint(cfg.gdr_image_base_url)
        if not url:
            logger.error("[群日报] gdr_image_base_url 为空，无法调用生图接口")
            return None

        # size 透传优先。不同服务商的语义完全不同：
        #   OpenAI  : "1024x1536"（固定白名单）
        #   火山方舟 : "2K" / "4K"（分辨率档位，不是 WxH）
        # native 模式下原样发过去，解析不了就绝不瞎猜，免得悄悄改掉用户的意图。
        raw_size = str(cfg.gdr_image_size or "").strip()
        want = parse_size(raw_size)
        native = want

        if cfg.gdr_image_size_mode == "upscale" and want is not None:
            native = snap_to_native(want, parse_native_sizes(cfg.gdr_image_native_sizes))
            request_size = f"{native[0]}x{native[1]}"
            if native != want:
                logger.info(
                    f"[群日报] 原生规格不支持 {want[0]}x{want[1]}，"
                    f"改用 {request_size} 请求后放大"
                )
        else:
            request_size = raw_size

        payload: dict[str, object] = {
            "model": cfg.gdr_image_model,
            "prompt": prompt,
            "n": 1,
        }
        if request_size:
            payload["size"] = request_size
        # quality 是 OpenAI 独有档位，Seedream 等不认，留空就不发，
        # 免得被当成非法参数拒掉
        if cfg.gdr_image_quality:
            payload["quality"] = cfg.gdr_image_quality
        if cfg.gdr_image_response_format:
            payload["response_format"] = cfg.gdr_image_response_format
        if cfg.gdr_image_output_format:
            payload["output_format"] = cfg.gdr_image_output_format
        if cfg.gdr_image_watermark is not None:
            payload["watermark"] = cfg.gdr_image_watermark
        payload.update(cfg.gdr_image_extra or {})

        last_error: Exception | None = None
        for key in cfg.gdr_image_api_keys:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            }
            try:
                async with httpx.AsyncClient(
                    proxy=cfg.gdr_image_proxy or None,
                    timeout=cfg.gdr_image_timeout,
                ) as client:
                    resp = await client.post(url, headers=headers, json=payload)
                    resp.raise_for_status()
                    body = resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    logger.warning("[群日报] 生图 Key 已限流，切换下一个")
                    last_error = e
                    continue
                logger.error(f"[群日报] 生图调用失败: {e!s}")
                return None
            except Exception as e:
                logger.error(f"[群日报] 生图调用异常: {e!s}")
                return None

            return await self._finish(client, body, want, native)

        logger.error(f"[群日报] 生图所有 Key 均失败: {last_error!s}")
        return None

    async def _finish(
        self,
        client: httpx.AsyncClient,
        body: dict,
        want: tuple[int, int] | None,
        native: tuple[int, int] | None,
    ) -> bytes | None:
        """取出图片字节并按需放大。

        Args:
            client: HTTP 客户端，用于 url 形式的响应下载。
            body: 接口响应体。
            want: 期望的最终尺寸，``None`` 表示不放大。
            native: 接口实际使用的尺寸。

        Returns:
            图片字节；失败时返回 ``None``。
        """
        data = await self._extract_image(client, body)
        if data is None or want is None or native is None or native == want:
            return data
        try:
            out = _resize(data, want)
        except RuntimeError as e:
            logger.error(f"[群日报] {e!s}")
            return data  # 放大失败总比整张图丢掉好
        logger.info(
            f"[群日报] 已放大到 {want[0]}x{want[1]}"
            f"（{len(data) // 1024}KB → {len(out) // 1024}KB）"
        )
        return out

    async def _extract_image(self, client: httpx.AsyncClient, body: dict) -> bytes | None:
        """从响应中取出图片字节，兼容 b64_json 与 url 两种返回形式。"""
        try:
            data = body["data"][0]
        except (KeyError, IndexError, TypeError):
            logger.error(f"[群日报] 生图响应缺少 data 字段: {list(body.keys())}")
            return None

        if b64 := data.get("b64_json"):
            try:
                return base64.b64decode(b64)
            except Exception as e:
                logger.error(f"[群日报] 生图 base64 解码失败: {e!s}")
                return None

        if image_url := data.get("url"):
            try:
                resp = await client.get(image_url, timeout=cfg_timeout(self._config))
                resp.raise_for_status()
                return resp.content
            except Exception as e:
                logger.error(f"[群日报] 生图图片下载失败: {e!s}")
                return None

        logger.error("[群日报] 生图响应中既没有 b64_json 也没有 url")
        return None


def cfg_timeout(config: Config) -> int:
    """下载生成图时的超时（秒）。"""
    return config.gdr_image_timeout

"""配置的 ``gdr_*`` 扁平视图。

移植模块（collect/llm/comicgen/pipeline 等）都按 nonebot 版的
``config.gdr_xxx`` 属性名访问配置；MaiBot 的配置则是按分区组织的 pydantic
模型。本模块做一层只读视图：WebUI 配置页看到的是清晰分组，移植代码看到的
仍是原来的扁平字段名，两边互不妥协。

视图接受两种输入：强类型的 :class:`core.config_model.GroupDailyAnalysisConfig`
实例，或（配置热更新回调里拿到的）原始 dict，按点路径取值。
"""

from __future__ import annotations

from typing import Any


def _get(obj: Any, dotted: str, default: Any = None) -> Any:
    """按点路径从对象或 dict 里取值。"""
    cur = obj
    for part in dotted.split("."):
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
    return default if cur is None else cur


def _as_int_list(value: Any) -> list[int]:
    """群号列表统一成 int（配置里是纯数字字符串）。"""
    out: list[int] = []
    for item in value or []:
        try:
            out.append(int(str(item).strip()))
        except (TypeError, ValueError):
            continue
    return out


class GdrConfig:
    """移植模块使用的 ``gdr_*`` 扁平配置视图（只读）。"""

    def __init__(self, config: Any) -> None:
        self._c = config

    # ── basic / 定时 ──────────────────────────────────────
    @property
    def gdr_timezone(self) -> str:
        return str(_get(self._c, "basic.timezone", "Asia/Shanghai"))

    @property
    def gdr_group_schedules(self) -> list[dict[str, Any]]:
        """每群独立时刻，规整成 ``[{"group_id": int, "time": "HH:MM", "enable": bool}]``。"""
        items = _get(self._c, "basic.group_schedules") or []
        out: list[dict[str, Any]] = []
        for item in items:
            gid = _get(item, "group_id", "")
            try:
                gid_int = int(str(gid).strip())
            except (TypeError, ValueError):
                continue
            out.append(
                {
                    "group_id": gid_int,
                    "time": str(_get(item, "time", "")).strip(),
                    "enable": bool(_get(item, "enable", True)),
                }
            )
        return out

    @property
    def gdr_group_list(self) -> list[int]:
        return _as_int_list(_get(self._c, "basic.group_list"))

    @property
    def gdr_push_cron(self) -> str:
        return str(_get(self._c, "basic.push_cron", "")).strip()

    @property
    def gdr_enable_report(self) -> bool:
        return bool(_get(self._c, "basic.enable_report", True))

    @property
    def gdr_enable_comic(self) -> bool:
        return bool(_get(self._c, "basic.enable_comic", True))

    # ── fetch / 采集 ──────────────────────────────────────
    @property
    def gdr_source(self) -> str:
        return str(_get(self._c, "fetch.source", "auto"))

    @property
    def gdr_window(self) -> str:
        return str(_get(self._c, "fetch.window", "today"))

    @property
    def gdr_window_min_messages(self) -> int:
        return int(_get(self._c, "fetch.window_min_messages", 1) or 1)

    @property
    def gdr_history_lens(self) -> int:
        return int(_get(self._c, "fetch.history_lens", 20000) or 20000)

    @property
    def gdr_max_messages(self) -> int:
        return int(_get(self._c, "fetch.max_messages", 4000) or 4000)

    @property
    def gdr_push_prefetch_minutes(self) -> int:
        return int(_get(self._c, "fetch.prefetch_minutes", 8) or 0)

    @property
    def gdr_push_fetch_interval(self) -> float:
        return float(_get(self._c, "fetch.push_fetch_interval", 15.0) or 15.0)

    @property
    def gdr_fetch_page_size(self) -> int:
        return int(_get(self._c, "fetch.fetch_page_size", 5000) or 5000)

    @property
    def gdr_fetch_max_pages(self) -> int:
        return int(_get(self._c, "fetch.fetch_max_pages", 1) or 1)

    @property
    def gdr_fetch_interval(self) -> float:
        return float(_get(self._c, "fetch.fetch_interval", 5.0) or 5.0)

    @property
    def gdr_member_cache_ttl(self) -> int:
        return int(_get(self._c, "fetch.member_cache_ttl", 21600) or 21600)

    @property
    def gdr_member_counts(self) -> dict[int, int]:
        raw = _get(self._c, "fetch.member_counts") or {}
        out: dict[int, int] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    out[int(str(k).strip())] = int(v)
                except (TypeError, ValueError):
                    continue
        return out

    @property
    def gdr_bot_qq(self) -> str:
        return str(_get(self._c, "fetch.bot_qq", "") or "").strip()

    # ── llm / 文字总结模型 ────────────────────────────────
    @property
    def gdr_text_base_url(self) -> str:
        return str(_get(self._c, "llm.base_url", "") or "").strip()

    @property
    def gdr_text_api_keys(self) -> list[str]:
        return [str(k) for k in (_get(self._c, "llm.api_keys") or []) if str(k).strip()]

    @property
    def gdr_text_model(self) -> str:
        return str(_get(self._c, "llm.model", "") or "").strip()



    @property
    def gdr_text_proxy(self) -> str:
        return str(_get(self._c, "llm.proxy", "") or "").strip()

    @property
    def gdr_text_timeout(self) -> int:
        return int(_get(self._c, "llm.timeout", 300) or 300)

    @property
    def gdr_max_tokens(self) -> int:
        return int(_get(self._c, "llm.max_tokens", 0) or 0)

    @property
    def gdr_max_topics(self) -> int:
        return int(_get(self._c, "llm.max_topics", 12) or 12)

    @property
    def gdr_max_quotes(self) -> int:
        return int(_get(self._c, "llm.max_quotes", 8) or 8)

    @property
    def gdr_max_titles(self) -> int:
        return int(_get(self._c, "llm.max_titles", 5) or 5)

    @property
    def gdr_enable_quotes(self) -> bool:
        return bool(_get(self._c, "llm.enable_quotes", True))

    @property
    def gdr_enable_quality(self) -> bool:
        return bool(_get(self._c, "llm.enable_quality", True))

    # ── image / 生图模型 ──────────────────────────────────
    @property
    def gdr_image_provider(self) -> str:
        return str(_get(self._c, "image.provider", "doubao"))

    @property
    def gdr_image_base_url(self) -> str:
        return str(_get(self._c, "image.base_url", "") or "").strip()

    @property
    def gdr_image_api_keys(self) -> list[str]:
        return [str(k) for k in (_get(self._c, "image.api_keys") or []) if str(k).strip()]

    @property
    def gdr_image_model(self) -> str:
        return str(_get(self._c, "image.model", "") or "").strip()

    @property
    def gdr_image_proxy(self) -> str:
        return str(_get(self._c, "image.proxy", "") or "").strip()

    @property
    def gdr_image_timeout(self) -> int:
        return int(_get(self._c, "image.timeout", 300) or 300)

    @property
    def gdr_image_size(self) -> str:
        return str(_get(self._c, "image.size", "") or "").strip()

    @property
    def gdr_image_size_mode(self) -> str:
        return str(_get(self._c, "image.size_mode", "native"))

    @property
    def gdr_image_native_sizes(self) -> str:
        return str(_get(self._c, "image.native_sizes", "") or "")

    @property
    def gdr_image_quality(self) -> str:
        return str(_get(self._c, "image.quality", "") or "").strip()

    @property
    def gdr_image_response_format(self) -> str:
        # WebUI 枚举不能用空串选项，"default" 在这里还原成移植代码认识的 ""
        raw = str(_get(self._c, "image.response_format", "default") or "default")
        return "" if raw == "default" else raw

    @property
    def gdr_image_output_format(self) -> str:
        raw = str(_get(self._c, "image.output_format", "default") or "default")
        return "" if raw == "default" else raw

    @property
    def gdr_image_watermark(self):
        raw = _get(self._c, "image.watermark", "default")
        if raw == "default":
            return None
        if isinstance(raw, bool):
            return raw
        return None if raw is None else str(raw).lower() == "true"

    @property
    def gdr_image_aspect_ratio(self) -> str:
        return str(_get(self._c, "image.aspect_ratio", "16:9") or "16:9")

    @property
    def gdr_image_optimize_prompt(self) -> str:
        raw = str(_get(self._c, "image.optimize_prompt", "default") or "default")
        return "" if raw == "default" else raw

    @property
    def gdr_image_extra(self) -> dict:
        raw = _get(self._c, "image.extra")
        return raw if isinstance(raw, dict) else {}

    @property
    def gdr_comic_prompt(self) -> str:
        return str(_get(self._c, "image.comic_prompt", "") or "")

    @property
    def gdr_image_retries(self) -> int:
        return int(_get(self._c, "image.retries", 1) or 0)

    @property
    def gdr_image_retry_delay(self) -> float:
        return float(_get(self._c, "image.retry_delay", 2.0) or 2.0)

    # ── report / 输出 ─────────────────────────────────────
    @property
    def gdr_report_mode(self) -> str:
        return str(_get(self._c, "report.mode", "both"))

    @property
    def gdr_report_style(self) -> str:
        return str(_get(self._c, "report.style", "scrapbook"))

    @property
    def gdr_report_width(self) -> int:
        return int(_get(self._c, "report.width", 900) or 900)

    @property
    def gdr_split_report(self) -> bool:
        return bool(_get(self._c, "report.split_report", False))

    @property
    def gdr_enable_text_summary(self) -> bool:
        return bool(_get(self._c, "report.enable_text_summary", True))

    @property
    def gdr_send_text(self) -> bool:
        return bool(_get(self._c, "report.send_text", False))

    @property
    def gdr_prompt_style(self) -> str:
        return str(_get(self._c, "report.prompt_style", "roast"))

    @property
    def gdr_prompt_override(self) -> str:
        return str(_get(self._c, "report.prompt_override", "") or "")

    # ── sensitive / 敏感防护 ──────────────────────────────
    @property
    def gdr_sensitive_filter(self) -> bool:
        return bool(_get(self._c, "sensitive.filter", True))

    @property
    def gdr_sensitive_lexicon(self) -> str:
        return str(_get(self._c, "sensitive.lexicon", "") or "")

    @property
    def gdr_sensitive_rewrites(self) -> int:
        return int(_get(self._c, "sensitive.rewrites", 1) or 0)

    # ── cleanup / 清理 ────────────────────────────────────
    @property
    def gdr_cleanup_enabled(self) -> bool:
        return bool(_get(self._c, "cleanup.enabled", False))

    @property
    def gdr_cleanup_cron(self) -> str:
        return str(_get(self._c, "cleanup.cron", "30 4 * * *")).strip()

    @property
    def gdr_retention_days(self) -> int:
        return int(_get(self._c, "cleanup.retention_days", 7) or 0)

"""群日报插件（MaiBot 版）。

移植自 astrbot_plugin_qq_group_daily_analysis（经 nonebot 版迭代）：

- 指令：``/群日报`` 出海报、``/群日报漫画`` 出漫画、``/群日报整体`` 两样都出
- 定时：每群独立时刻（group_schedules）优先，全局 cron（push_cron + group_list）
  兜底，支持提前采集（prefetch_minutes）
- 海报复用 astrbot 原插件的 6 套主题模板与素材（templates/themes/）
- 漫画由独立的生图模型生成（image 分组配置），与文字总结模型（llm 分组配置）
  分开配置；文字模型未填 base_url/api_keys 时回落 MaiBot 宿主模型
- 全部配置在 MaiBot WebUI 的插件配置页修改（config.toml 自动生成）
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from maibot_sdk import Command, MaiBotPlugin

try:  # Runner 以包形式加载插件入口时走相对导入
    from .core import asset_server, cleanup as cleanup_mod
    from .core import collect, scheduling, themes
    from .core.comicgen import ComicImageModel
    from .core.config_model import GroupDailyAnalysisConfig
    from .core.llm import TextModel
    from .core.log import logger
    from .core.pipeline import generate_daily_report
    from .core.settings import GdrConfig
except ImportError:  # 以平铺模块方式加载时兜底
    from core import asset_server, cleanup as cleanup_mod  # type: ignore
    from core import collect, scheduling, themes  # type: ignore
    from core.comicgen import ComicImageModel  # type: ignore
    from core.config_model import GroupDailyAnalysisConfig  # type: ignore
    from core.llm import TextModel  # type: ignore
    from core.log import logger  # type: ignore
    from core.pipeline import generate_daily_report  # type: ignore
    from core.settings import GdrConfig  # type: ignore

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
except ImportError:  # pragma: no cover - requirements.txt 已声明 apscheduler
    AsyncIOScheduler = None


class MaiBotHost:
    """宿主适配层：把移植模块需要的消息/群信息读取封装成统一接口。

    读取全部走 MaiBot SDK 的能力代理（``ctx.message`` / ``ctx.chat`` /
    ``ctx.db``），并做多形状兜底——不同版本宿主返回的字段结构不完全一致，
    细粒度清洗由 :mod:`core.collect` 的 ``_normalize_message`` 负责。
    """

    def __init__(self, plugin: "GroupDailyAnalysisPlugin") -> None:
        self._plugin = plugin
        self._stream_cache: dict[int, str] = {}
        self._stream_info: dict[int, dict] = {}

    @staticmethod
    def _unwrap(result: Any, key: str) -> Any:
        """解包宿主能力信封：``{"success": bool, key: 数据}``。

        Args:
            result: 能力代理返回值。
            key: 数据所在的键名。

        Returns:
            解包后的数据；信封缺失（旧版直接返回数据）时原样返回。
        """
        if isinstance(result, dict) and key in result:
            if result.get("success") is False:
                raise RuntimeError(str(result.get("error") or "宿主能力调用失败"))
            return result.get(key)
        return result

    @property
    def self_id(self) -> int | None:
        """机器人自身 QQ 号；配置未填且探测不到时返回 ``None``（不过滤）。"""
        raw = self._plugin.settings.gdr_bot_qq
        if raw:
            try:
                return int(raw)
            except ValueError:
                return None
        detected = getattr(self._plugin.ctx, "self_id", None) or getattr(
            self._plugin.ctx, "bot_account_id", None
        )
        try:
            return int(detected) if detected else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_stream_id(stream: Any) -> str:
        """从 stream 对象/dict 里取 stream_id。"""
        if isinstance(stream, str):
            return stream.strip()
        if isinstance(stream, dict):
            for key in ("stream_id", "session_id", "chat_id", "id"):
                value = stream.get(key)
                if value:
                    return str(value).strip()
            return ""
        value = getattr(stream, "stream_id", None) or getattr(stream, "session_id", None)
        return str(value).strip() if value else ""

    def _remember_stream(self, group_id: int, stream: Any) -> None:
        """缓存 stream_id 并留存群名等信息。"""
        sid = self._extract_stream_id(stream)
        if not sid:
            return
        self._stream_cache[group_id] = sid
        if isinstance(stream, dict) and stream.get("group_name"):
            self._stream_info[group_id] = stream

    async def _resolve_stream(self, group_id: int) -> str | None:
        """群号 → stream_id（chat_id），带缓存与多级兜底。

        1. ``get_stream_by_group_id`` 精确查询；
        2. ``get_group_streams`` 全量枚举后本地匹配（platform 命名不一致也能兜住）；
        3. ``open_session`` 打开会话（群从未与机器人交互时的最后手段）。
        """
        cached = self._stream_cache.get(group_id)
        if cached:
            return cached
        chat = getattr(self._plugin.ctx, "chat", None)
        if chat is None:
            logger.warning("[群日报] 宿主没有 ctx.chat 能力，无法解析群会话")
            return None

        # 1. 精确查询
        for kwargs in ({"platform": "qq"}, {}):
            try:
                stream = self._unwrap(
                    await chat.get_stream_by_group_id(str(group_id), **kwargs), "stream"
                )
            except Exception as e:
                logger.debug(f"[群日报] get_stream_by_group_id({kwargs}) 失败: {e!s}")
                continue
            sid = self._extract_stream_id(stream)
            if sid:
                self._remember_stream(group_id, stream)
                logger.info(
                    f"[群日报] stream 解析成功（精确查询, kwargs={kwargs}）: "
                    f"{sid} → {stream if isinstance(stream, dict) else type(stream).__name__}"
                )
                return sid

        # 2. 枚举群会话本地匹配
        streams = None
        for kwargs in ({"platform": "qq"}, {}):
            try:
                streams = self._unwrap(await chat.get_group_streams(**kwargs), "streams")
                if streams:
                    break
            except Exception as e:
                logger.debug(f"[群日报] get_group_streams({kwargs}) 失败: {e!s}")
        for item in streams or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("group_id") or "").strip() == str(group_id):
                sid = self._extract_stream_id(item)
                if sid:
                    self._remember_stream(group_id, item)
                    logger.info(f"[群日报] 群 {group_id} 经 get_group_streams 匹配到会话")
                    return sid

        # 3. 打开会话（最后手段；群必须至少和机器人有过交互才会真正存在）
        try:
            stream = self._unwrap(
                await chat.open_session(platform="qq", chat_type="group", group_id=str(group_id)),
                "stream",
            )
            sid = self._extract_stream_id(stream)
            if sid:
                self._remember_stream(group_id, stream)
                logger.info(f"[群日报] 群 {group_id} 经 open_session 打开会话")
                return sid
        except Exception as e:
            logger.debug(f"[群日报] open_session 失败: {e!s}")

        logger.warning(
            f"[群日报] 群 {group_id} 未能解析到 stream_id"
            f"（机器人是否已在群里收到过消息？）"
        )
        return None

    async def get_group_messages(
        self,
        group_id: int,
        *,
        start_ts: int,
        end_ts: int,
        limit: int,
        source: str,
    ) -> list[dict]:
        """按时间窗口取群消息（collect.py 的数据源）。

        Args:
            group_id: 群号。
            start_ts: 窗口起点。
            end_ts: 窗口终点。
            limit: 条数上限。
            source: ``message_api``=宿主消息接口；``db``=直查 ChatHistory。

        Returns:
            消息 dict 列表（结构由 collect 归一化）。
        """
        chat_id = await self._resolve_stream(group_id)
        if not chat_id:
            return []

        messages: list[dict] = []
        if source == "message_api":
            message = getattr(self._plugin.ctx, "message", None)
            if message is None:
                return []
        messages: list[dict] = []
        if source == "message_api":
            message = getattr(self._plugin.ctx, "message", None)
            if message is None:
                logger.warning("[群日报] 宿主没有 ctx.message 能力")
                return []
            getter = getattr(message, "get_by_time_in_chat", None)
            if callable(getter):
                try:
                    # 宿主实现按 float Unix 时间戳取数（data.py:
                    # _cap_message_get_by_time_in_chat）；filter_command
                    # 顺带剔除 /指令 消息，不污染日报
                    raw = await getter(
                        chat_id,
                        float(start_ts),
                        float(end_ts),
                        limit=limit,
                        limit_mode="latest",
                        filter_command=True,
                    )
                    messages = list(self._unwrap(raw, "messages") or [])
                    logger.info(
                        f"[群日报] 群 {group_id} 按时间窗口取到 {len(messages)} 条"
                        f"（chat_id={chat_id}, {start_ts}~{end_ts}）"
                    )
                except Exception as e:
                    logger.warning(f"[群日报] 群 {group_id} 按时间取消息失败: {e!s}")
            if not messages:
                recent = getattr(message, "get_recent", None)
                if callable(recent):
                    try:
                        # 宿主 get_recent 按 hours 回看（默认 24h）
                        raw = await recent(chat_id, limit=limit)
                        messages = list(self._unwrap(raw, "messages") or [])
                        logger.info(
                            f"[群日报] 群 {group_id} get_recent 兜底取到 {len(messages)} 条"
                        )
                    except Exception as e:
                        logger.warning(f"[群日报] 群 {group_id} 取最近消息失败: {e!s}")
        else:  # db
            db = getattr(self._plugin.ctx, "db", None)
            query = getattr(db, "query", None) if db else None
            if not callable(query):
                return []
            try:
                raw = await query(
                    model_name="ChatHistory",
                    query_type="get",
                    filters={"chat_id": chat_id},
                    limit=limit,
                )
                rows = self._unwrap(raw, "data")
                messages = list(rows or []) if isinstance(rows, list) else []
            except Exception as e:
                logger.warning(f"[群日报] 群 {group_id} 查询 ChatHistory 失败: {e!s}")

        # 顺带从消息里扒群名（get_group_meta 接口不可用时的兜底）
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            info = msg.get("message_info") if isinstance(msg.get("message_info"), dict) else msg
            ginfo = info.get("group_info") if isinstance(info.get("group_info"), dict) else {}
            name = str(ginfo.get("group_name") or "").strip()
            if name:
                self._stream_info.setdefault(
                    group_id, {"group_name": name, "group_id": str(group_id)}
                )
                break
        return messages

    async def resolve_stream(self, group_id: int) -> str | None:
        """群号 → stream_id（带缓存，供定时推送与发送层使用）。"""
        return await self._resolve_stream(group_id)

    async def get_group_meta(self, group_id: int) -> tuple[int, str]:
        """取 ``(成员数, 群名)``；宿主没有成员数接口时由配置覆盖表兜底。"""
        info = self._stream_info.get(group_id) or {}
        if not info:
            await self._resolve_stream(group_id)
            info = self._stream_info.get(group_id) or {}
        # 宿主 1.3.x 没有暴露群成员数能力，成员数靠 fetch.member_counts 配置兜底
        return 0, str(info.get("group_name") or "").strip()


class GroupDailyAnalysisPlugin(MaiBotPlugin):
    """插件入口：指令、定时任务与发送编排。"""

    config_model = GroupDailyAnalysisConfig

    def __init__(self) -> None:
        super().__init__()
        self.settings: GdrConfig | None = None
        self.text_model: TextModel | None = None
        self.image_model: ComicImageModel | None = None
        self.host: MaiBotHost | None = None
        self._scheduler: Any = None
        self._group_locks: dict[int, asyncio.Lock] = {}
        self._bg_tasks: set[asyncio.Task] = set()

    # ── 生命周期 ──────────────────────────────────────────

    async def on_load(self) -> None:
        """装配依赖并启动定时任务。"""
        self._setup()
        # 主题大素材（ATRI 24MB 字体）经本地静态服务器供给，
        # 内联会超宿主 16MB RPC 帧上限
        asset_server.start_asset_server(themes.THEME_DIR)
        self._start_scheduler()
        if self.text_model is not None and not self.text_model.available:
            logger.warning("[群日报] llm.api_keys 未配置且宿主模型不可用，插件将无法生成日报")
        logger.info(
            "[群日报] 插件已加载：指令 /群日报、/群日报漫画、/群日报整体；"
            "配置在 WebUI 插件配置页修改"
        )

    async def on_unload(self) -> None:
        """停掉定时任务并释放渲染钩子。"""
        self._stop_scheduler()
        themes.set_html_renderer(None)
        asset_server.stop_asset_server()
        logger.info("[群日报] 插件已卸载")

    async def on_config_update(self, scope: str = "", config_data: Any = None, version: Any = None) -> None:
        """配置热更新：重建模型并重启定时任务。"""
        logger.info("[群日报] 检测到配置更新，重载定时任务与模型客户端")
        self._stop_scheduler()
        self._setup()
        self._start_scheduler()

    # ── 装配 ──────────────────────────────────────────────

    def _setup(self) -> None:
        """按当前配置重建各依赖。"""
        self.settings = GdrConfig(self.config)
        self.host = MaiBotHost(self)
        self.text_model = TextModel(self.settings, host_generate=self._host_generate)
        self.image_model = ComicImageModel(self.settings)
        themes.set_html_renderer(self._render_html_to_png)

    def _start_scheduler(self) -> None:
        """启动 APScheduler 并按配置注册任务。"""
        if AsyncIOScheduler is None:
            logger.error("[群日报] 未安装 apscheduler，定时推送不可用（requirements.txt 应含 apscheduler）")
            return
        assert self.settings is not None
        self._scheduler = AsyncIOScheduler(timezone=scheduling.tzinfo(self.settings.gdr_timezone))
        scheduling.register_schedules(
            self._scheduler,
            self.settings,
            self._make_pusher,
            cleanup_coro=self._cleanup_job,
        )
        self._scheduler.start()

    def _stop_scheduler(self) -> None:
        """关闭 APScheduler（幂等）。"""
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._scheduler = None

    # ── 宿主回调 ──────────────────────────────────────────

    async def _host_generate(self, system: str, user: str) -> str:
        """宿主模型回落：经 ctx.llm.generate 调 MaiBot 配置的模型。"""
        model_name = self.settings.gdr_text_model if self.settings else ""
        try:
            result = await self.ctx.llm.generate(
                prompt=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                model=model_name or "",
            )
        except Exception as e:
            # 失败时打印宿主可用模型列表，方便对照模型名
            available = None
            getter = getattr(self.ctx.llm, "get_available_models", None)
            if callable(getter):
                try:
                    available = await getter()
                except Exception:
                    pass
            logger.error(
                f"[群日报] 宿主模型调用失败（model={model_name or '默认'}）: {e!s}；"
                f"宿主可用模型: {available}"
            )
            raise
        if isinstance(result, dict):
            if not result.get("success", True):
                raise RuntimeError(str(result.get("error") or "宿主模型调用失败"))
            return str(result.get("response") or "")
        return str(result or "")

    async def _render_html_to_png(self, html: str, width: int) -> bytes | None:
        """渲染钩子：包一层宿主 ``ctx.render.html2png``，返回 PNG 字节。"""
        render = getattr(self.ctx, "render", None)
        html2png = getattr(render, "html2png", None) if render else None
        if not callable(html2png):
            logger.error("[群日报] 宿主没有 ctx.render.html2png 能力，无法渲染海报")
            return None
        try:
            result = await html2png(
                html,
                viewport={"width": width, "height": 10},
                # 宿主有 16MB 帧大小上限：默认 2 倍像素密度会把长图顶爆
                # （900px 宽 ×2 × 6000+px 高 ≈ 45MB），降到 1 倍
                device_scale_factor=1.0,
                # 海报是整页长图，默认 False 只截视口高度
                full_page=True,
                # 主题模板的 Google 字体走公网，必须放行网络
                allow_network=True,
                # 等字体/远程资源加载完再截
                wait_until="networkidle",
                wait_for_timeout_ms=1500,
                render_timeout_ms=120_000,
            )
        except Exception as e:
            logger.error(f"[群日报] html2png 渲染失败: {e!s}")
            return None
        if isinstance(result, (bytes, bytearray)):
            return bytes(result)
        if isinstance(result, dict):
            b64 = result.get("image_base64") or result.get("image_b64")
            if b64:
                return base64.b64decode(str(b64))
        logger.error(f"[群日报] html2png 返回了无法识别的结构: {type(result).__name__}")
        return None

    # ── 指令 ──────────────────────────────────────────────

    def _spawn_generation(self, kwargs: dict, *, with_comic: bool, comic_only: bool, empty: str) -> None:
        """把生成任务丢到后台执行。

        宿主对命令调用有 60 秒 RPC 硬超时，而采集+LLM+渲染经常超过它——
        命令处理器必须立即返回，生成结果由后台任务自行发送。
        """
        task = asyncio.create_task(
            self._handle(kwargs, with_comic=with_comic, comic_only=comic_only, empty=empty)
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    @Command(
        "group_daily_report",
        description="生成群聊日报海报",
        pattern=r"^[/／!！]?\s*(?:群聊日报|群日报)\s*$",
    )
    async def handle_report(self, **kwargs):
        """处理 ``/群日报``：只出海报。"""
        self._spawn_generation(
            kwargs,
            with_comic=False,
            comic_only=False,
            empty="未能生成日报，可能是群消息过少或模型调用失败。",
        )
        # 消息全部由后台任务自己发（含失败提示与收尾），这里不返回文案，
        # 免得宿主把返回值再播报一遍
        return True, "", 5

    @Command(
        "group_daily_comic",
        description="只生成群聊日报漫画",
        pattern=r"^[/／!！]?\s*(?:群聊日报漫画|群日报漫画)\s*$",
    )
    async def handle_comic(self, **kwargs):
        """处理 ``/群日报漫画``：只出漫画，不渲染海报。"""
        self._spawn_generation(
            kwargs,
            with_comic=True,
            comic_only=True,
            empty="未能生成漫画，可能是群消息过少或模型调用失败。",
        )
        return True, "", 5

    @Command(
        "group_daily_full",
        description="生成群聊日报海报和漫画",
        pattern=r"^[/／!！]?\s*(?:群聊日报整体|群日报整体)\s*$",
    )
    async def handle_full(self, **kwargs):
        """处理 ``/群日报整体``：海报 + 漫画，两样都发。"""
        self._spawn_generation(
            kwargs,
            with_comic=True,
            comic_only=False,
            empty="未能生成日报，可能是群消息过少或模型调用失败。",
        )
        return True, "", 5

    # ── 指令公共流程 ──────────────────────────────────────

    def _group_lock(self, group_id: int) -> asyncio.Lock:
        """取某个群的日报锁，首次见到时创建。"""
        lock = self._group_locks.get(group_id)
        if lock is None:
            lock = self._group_locks[group_id] = asyncio.Lock()
        return lock

    async def _handle(
        self,
        kwargs: dict,
        *,
        with_comic: bool,
        comic_only: bool,
        empty: str,
    ) -> None:
        """三条指令的公共流程：加锁生成 → 发送。

        ``with_comic`` 和 ``comic_only`` 决定了产出什么，但**三者共用同一次文字
        模型分析**——漫画提示词是从这份 analysis 的 summary/topics 派生的，所以
        「群日报整体」不会为了多出这张图而再调一次文字模型。

        **开始时不发「请稍候」**：生图 + 渲染要几十秒，中途插话会盖在群消息
        流里，反而像刷屏。生成完毕由 Command 返回值收尾。
        """
        group_id = self._extract_group_id(kwargs)
        stream_id = str(kwargs.get("stream_id") or "")
        if not group_id:
            await self._send_text(stream_id, "未能识别群号，请在群内使用本指令。")
            return
        assert self.settings is not None and self.image_model is not None

        if comic_only and not self.image_model.available:
            await self._send_text(stream_id, "未配置 image.api_keys，无法生成漫画。")
            return

        lock = self._group_lock(group_id)
        if lock.locked():
            await self._send_text(stream_id, "本群已有日报任务在进行中，请稍候…")
            return

        async with lock:
            segments = await self._generate(
                group_id, with_comic=with_comic, comic_only=comic_only
            )
            if not segments:
                await self._send_text(stream_id, empty)
                return
            await self._send_segments(stream_id, segments)
            # 收尾与 nonebot 版一致：海报渲染慢、也可能被 QQ 吞掉，
            # 没有这句用户会以为没收到而重复敲指令
            await self._send_text(stream_id, "群日报已发送，请查收")

    def _extract_group_id(self, kwargs: dict) -> int | None:
        """从指令 kwargs 里解析群号。"""
        for key in ("group_id", "chat_id"):
            raw = kwargs.get(key)
            if raw:
                try:
                    return int(str(raw).strip())
                except (TypeError, ValueError):
                    continue
        message = kwargs.get("message") or {}
        if isinstance(message, dict):
            info = message.get("message_info") if isinstance(message.get("message_info"), dict) else message
            ginfo = info.get("group_info") if isinstance(info.get("group_info"), dict) else {}
            raw = ginfo.get("group_id")
            try:
                return int(str(raw)) if raw else None
            except (TypeError, ValueError):
                return None
        return None

    async def _generate(
        self, group_id: int, *, with_comic: bool, comic_only: bool
    ) -> list[dict] | None:
        """跑一遍采集与分析，返回待发送的消息段。"""
        assert self.settings is not None
        try:
            segments, _, _ = await generate_daily_report(
                self.host,
                group_id,
                self.settings,
                text_model=self.text_model,
                image_model=self.image_model,
                with_comic=with_comic,
                comic_only=comic_only,
                # 用户主动敲的指令才值得告诉他「为什么没漫画」；
                # 缺 key 这类问题记日志即可，别拿告警刷群
                notify_missing_comic=comic_only or with_comic,
            )
            return segments
        except Exception as e:
            logger.error(f"[群日报] 指令执行失败 (comic_only={comic_only}): {e!s}")
            return None

    # ── 发送层 ────────────────────────────────────────────

    async def _send_text(self, stream_id: str, text: str) -> None:
        """向群发一条文本。"""
        if not stream_id or not text:
            return
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as e:
            logger.error(f"[群日报] 文本发送失败: {e!s}")

    async def _send_image(self, stream_id: str, b64: str) -> None:
        """向群发一张 base64 图片。"""
        try:
            await self.ctx.send.image(b64, stream_id)
        except Exception as e:
            logger.error(f"[群日报] 图片发送失败: {e!s}")

    async def _send_segments(self, stream_id: str, segments: list[Any]) -> None:
        """逐条发送日报内容。

        段结构：图片段来自 :func:`core.report.to_image_segment`，文字/告警段
        是 ``{"type": "text", ...}``。

        默认只发海报、不发正文（``report.send_text=false``），因为海报里已经
        有全部内容，再发一遍文字是重复且刷屏。但两种例外必须保留：

        * 告警段（``notice=True``）始终发送——否则海报渲染失败时群里一条
          都收不到，会变成「插件没反应」；
        * 一张图都没渲染出来时，退回发文字版，保证日报不为空。

        Args:
            stream_id: 目标群会话。
            segments: 要发送的消息段。
        """
        assert self.settings is not None
        images = [s for s in segments if s.get("type") == "image"]
        notices = "\n".join(
            str((s.get("data") or {}).get("text") or "")
            for s in segments
            if s.get("type") != "image" and s.get("notice")
        ).strip()
        body = "\n".join(
            str((s.get("data") or {}).get("text") or "")
            for s in segments
            if s.get("type") != "image" and not s.get("notice")
        ).strip()

        texts = body if (self.settings.gdr_send_text or not images) else ""
        parts = [t for t in (notices, texts) if t]

        if parts:
            await self._send_text(stream_id, "\n".join(parts))
        elif not images:
            await self._send_text(stream_id, "（日报内容为空）")

        for i, img in enumerate(images):
            b64 = str((img.get("data") or {}).get("b64") or "")
            if b64:
                await self._send_image(stream_id, b64)
            # 多张图之间留一点间隔，避免触发发送频率限制
            if i < len(images) - 1:
                await asyncio.sleep(1)

    # ── 定时推送 ──────────────────────────────────────────

    def _make_pusher(self, group_ids: list[int], lead: int, send_hours: list[tuple[int, int]] | None):
        """构造一个推送指定若干群的协程（scheduling.register_schedules 用）。

        Args:
            group_ids: 目标群号列表。
            lead: 提前采集的分钟数，0 表示到点才采集。
            send_hours: 与 ``group_ids`` 一一对应的 ``(时, 分)``；
                给了才会等待到点再发。

        Returns:
            异步协程。
        """

        async def _run() -> None:
            assert self.settings is not None
            if not (self.text_model and self.text_model.available):
                logger.warning("[群日报] 未配置文字模型（llm.api_keys / 宿主模型），跳过定时推送")
                return
            for i, gid in enumerate(group_ids):
                hh = mm = None
                if send_hours is not None and i < len(send_hours):
                    hh, mm = send_hours[i]
                await self._push_one(gid, send_hour=hh, send_minute=mm, lead=lead)
                # 群之间留间隔，连续调接口容易触发协议端限流
                await asyncio.sleep(3)

        return _run

    async def _prefetch_archive(self, group_id: int):
        """定时推送的提前采集：只拉消息，不做任何分析。"""
        assert self.settings is not None
        start_ts, end_ts, _ = collect.resolve_window(self.settings.gdr_window)
        try:
            archive = await collect.fetch_group_archive(
                self.host,
                group_id,
                history_lens=self.settings.gdr_history_lens,
                source=self.settings.gdr_source,
                member_cache_ttl=self.settings.gdr_member_cache_ttl,
                self_id=self.host.self_id if self.host else None,
                since_ts=start_ts,
                end_ts=end_ts,
                member_counts=self.settings.gdr_member_counts,
            )
        except Exception as e:
            logger.warning(f"[群日报] 群 {group_id} 提前采集失败: {e!s}（到点后仍会再试一次）")
            return None
        logger.info(f"[群日报] 群 {group_id} 提前采集完成：{len(archive.messages)} 条")
        return archive if archive.messages else None

    async def _push_one(
        self,
        group_id: int,
        *,
        send_hour: int | None = None,
        send_minute: int | None = None,
        lead: int = 0,
    ) -> None:
        """向单个群生成并推送日报（海报 + 漫画）。

        ``lead>0`` 时启用提前采集：先把聊天记录拉回来，等到 ``send_hour:send_minute``
        再做分析和发送。采集是耗时/易拥塞的那一步，分析和生图不是，所以只提前采集。

        Args:
            group_id: 群号。
            send_hour: 实际发送时刻的小时；``None`` 表示不等待。
            send_minute: 实际发送时刻的分钟。
            lead: 提前采集的分钟数。
        """
        assert self.settings is not None
        stream_id = await self._resolve_stream_or_none(group_id)
        if not stream_id:
            logger.warning(f"[群日报] 群 {group_id} 无法解析 stream_id，跳过推送")
            return
        archive = None
        if lead > 0:
            archive = await self._prefetch_archive(group_id)
            if send_hour is not None:
                await scheduling.wait_until(
                    send_hour, send_minute or 0, scheduling.tzinfo(self.settings.gdr_timezone)
                )

        try:
            segments, stats, _ = await generate_daily_report(
                self.host,
                group_id,
                self.settings,
                text_model=self.text_model,
                image_model=self.image_model,
                with_comic=self.settings.gdr_enable_comic,
                # enable_report=false：定时推送不出海报（文字/漫画照常），
                # 手动指令不受影响
                poster=self.settings.gdr_enable_report,
                archive=archive,
            )
            if not segments:
                await self._send_text(stream_id, "日报生成失败：无可用消息或模型异常。")
                return
            await self._send_segments(stream_id, segments)
            # 定时推送与指令共用同一个收尾：发完都要报一句「请查收」。
            # 海报渲染慢、也可能被 QQ 吞掉，没有这句用户会以为没收到而重复敲指令。
            await self._send_text(stream_id, "群日报已发送，请查收")
            logger.info(
                f"[群日报] 已推送群 {group_id} 日报"
                f"（{stats.total_messages if stats else 0} 条消息，"
                f"风格={self.settings.gdr_report_style}，漫画={self.settings.gdr_enable_comic}）"
            )
        except Exception as e:
            logger.error(f"[群日报] 定时推送群 {group_id} 失败: {e!s}")

    async def _resolve_stream_or_none(self, group_id: int) -> str | None:
        """群号 → stream_id（host 解析缓存的透传）。"""
        if self.host is None:
            return None
        return await self.host.resolve_stream(group_id)

    # ── 清理 ──────────────────────────────────────────────

    async def _cleanup_job(self) -> None:
        """定期清理插件数据目录里的过期缓存文件。"""
        assert self.settings is not None
        try:
            data_dir = getattr(self.ctx, "paths", None)
            data_dir = getattr(data_dir, "data_dir", None)
            if not data_dir:
                logger.debug("[群日报] 宿主未提供 data_dir，跳过清理")
                return
            await asyncio.to_thread(
                cleanup_mod.cleanup_data_dir,
                data_dir,
                retention_days=self.settings.gdr_retention_days,
            )
        except Exception as e:
            logger.error(f"[群日报] 清理任务异常: {e!s}")


def create_plugin() -> GroupDailyAnalysisPlugin:
    """MaiBot 插件入口工厂（Runner 约定）。"""
    return GroupDailyAnalysisPlugin()

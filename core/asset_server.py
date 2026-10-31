"""主题素材本地静态服务器。

宿主对插件 RPC 帧有 16MB 上限（``transport/base.py``），而 ATRI 等主题把
24MB 字体内联进 HTML 后请求本身就有 45MB+——渲染请求根本到不了浏览器。

所以大素材不再内联：插件在 127.0.0.1 起一个 stdlib 静态服务器，把模板里的
相对资源引用改写为 ``http://127.0.0.1:<port>/<主题>/<相对路径>``，HTML 保持
几十 KB，字体/图片由本机供给（宿主渲染允许网络时可直接访问）。
"""

from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .log import logger

_THEME_ROOT: Path | None = None
_SERVER: ThreadingHTTPServer | None = None
_THREAD: threading.Thread | None = None
_BASE_URL: str = ""


class _ThemeHandler(SimpleHTTPRequestHandler):
    """把 ``/<主题>/<路径>`` 映射到 templates/themes/ 下的静态文件。"""

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        """静默访问日志（每个海报几十个请求，打日志纯噪音）。"""
        return

    def do_GET(self) -> None:  # noqa: N802
        """吞掉浏览器断连（无头浏览器拿完资源就掐 keep-alive，属正常行为）。"""
        try:
            super().do_GET()
        except (ConnectionError, BrokenPipeError, TimeoutError):
            pass

    do_HEAD = do_GET

    def translate_path(self, path: str) -> str:
        """把 URL 路径映射进主题根目录（SimpleHTTPRequestHandler 钩子）。"""
        if _THEME_ROOT is None:
            return str(Path.cwd() / "notfound")
        cleaned = path.split("?", 1)[0].split("#", 1)[0].lstrip("/")
        candidate = (_THEME_ROOT / cleaned).resolve()
        try:
            candidate.relative_to(_THEME_ROOT.resolve())
        except ValueError:
            return str(_THEME_ROOT / "notfound")
        return str(candidate)


class _ThemeServer(ThreadingHTTPServer):
    """静默处理断连，避免 socketserver 往 stderr 刷异常堆栈。"""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        """连接被浏览器掐断属正常，静默；其他错误照常记录。"""
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def start_asset_server(theme_root: Path) -> str:
    """启动（或复用）素材静态服务器，返回 base URL。

    Args:
        theme_root: ``templates/themes`` 目录。

    Returns:
        ``http://127.0.0.1:<port>``；启动失败返回空串（调用方回退内联）。
    """
    global _THEME_ROOT, _SERVER, _THREAD, _BASE_URL

    root = Path(theme_root).resolve()
    if _SERVER is not None and _THEME_ROOT == root:
        return _BASE_URL

    stop_asset_server()
    try:
        # 绑定 0 由系统分配空闲端口，避免与其他服务冲突
        _SERVER = _ThemeServer(("127.0.0.1", 0), partial(_ThemeHandler, directory=str(root)))
        _THEME_ROOT = root
        port = _SERVER.server_address[1]
        _BASE_URL = f"http://127.0.0.1:{port}"
        _THREAD = threading.Thread(target=_SERVER.serve_forever, name="gdr-asset-server", daemon=True)
        _THREAD.start()
        logger.info(f"[群日报] 素材静态服务器已启动: {_BASE_URL}（根目录 {root}）")
        return _BASE_URL
    except OSError as e:
        logger.error(f"[群日报] 素材静态服务器启动失败，海报字体/图片可能加载不出来: {e!s}")
        _SERVER = None
        return ""


def stop_asset_server() -> None:
    """关闭素材服务器（on_unload 调用；幂等）。"""
    global _SERVER, _THREAD, _BASE_URL
    if _SERVER is not None:
        try:
            _SERVER.shutdown()
            _SERVER.server_close()
        except OSError:
            pass
        _SERVER = None
        _THREAD = None
        logger.info("[群日报] 素材静态服务器已关闭")
    _BASE_URL = ""


def get_asset_base_url() -> str:
    """当前素材服务器 base URL；未启动返回空串。"""
    return _BASE_URL

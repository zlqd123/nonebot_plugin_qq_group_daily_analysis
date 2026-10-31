"""把主题引用的 Google 字体与 lucide.js 全量缓存到本地。

渲染页面时 Google Fonts（fonts.googleapis.com / fonts.gstatic.com）和
jsdelivr 在国内经常不可达，导致标题/正文退回系统字体、miku 图标全空。
本工具扫描 6 套主题模板里引用的全部远程资源，一次性下载到
``templates/themes/_fonts/``（CSS）与 ``templates/themes/miku/lucide.js``：

- CSS 按主题的 css2 查询串哈希存为 ``_fonts/css/<hash>.css``，
  内部 url(...) 改写到 ``../files/<hash>.woff2``；
- 字体文件（含 CJK 全部 unicode-range 切片）按 URL 哈希去重下载，
  重复字重自动共享同一份文件；
- 运行时 ``core/themes.py`` 会把模板里的 css2 链接自动改写为本地
  素材服务器地址（缓存存在才改写，否则仍走公网，不影响未缓存用户）。

用法::

    python tools/fetch_webfonts.py            # 全量缓存（约 40~80MB）
    python tools/fetch_webfonts.py --proxy http://127.0.0.1:7897
    python tools/fetch_webfonts.py --dry-run  # 只列出要下载的资源

代理：默认依次尝试环境变量 HTTP(S)_PROXY、本机常见代理端口
（7897/7890/10809）、直连——Google 资源在国内一般需要前两者之一。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import socket
import sys
from pathlib import Path

import httpx

PLUG = Path(__file__).resolve().parent.parent
THEMES = PLUG / "templates" / "themes"
FONTS_DIR = THEMES / "_fonts"
CSS_DIR = FONTS_DIR / "css"
FILES_DIR = FONTS_DIR / "files"

LUCIDE_URL = "https://cdn.jsdelivr.net/npm/lucide@latest/dist/umd/lucide.js"
LUCIDE_DST = THEMES / "miku" / "lucide.js"

_CSS2_RE = re.compile(r"https://fonts\.googleapis\.com/css2\?[^\"'\s]+")
# 模板源码里写的是 Jinja 变量前缀，渲染后才是完整 URL——扫描时两种都要认
_CSS2_TPL_RE = re.compile(r"\{\{\s*t2i_google_fonts_mirror\s*\}\}/css2\?[^\"'\s]+")
_URL_RE = re.compile(r"url\((https://fonts\.gstatic\.com/[^)]+)\)")

# Chrome UA 才能拿到 woff2 + unicode-range 切片版的 css2 响应
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


def _slug(url: str) -> str:
    """css2 URL → 本地缓存文件名（去 scheme/host，只看查询串）。"""
    query = url.split("?", 1)[1] if "?" in url else url
    return hashlib.md5(query.encode("utf-8")).hexdigest()[:16]


def _collect_css2_urls() -> list[str]:
    """扫描全部主题模板，收集去重后的 css2 链接（Jinja 变量前缀还原为域名）。"""
    urls: list[str] = []
    for tpl in sorted(THEMES.glob("*/image_template.html")):
        text = tpl.read_text(encoding="utf-8")
        found = _CSS2_RE.findall(text) + _CSS2_TPL_RE.findall(text)
        urls += [
            u.replace("{{ t2i_google_fonts_mirror }}", "https://fonts.googleapis.com")
            .replace("{{t2i_google_fonts_mirror}}", "https://fonts.googleapis.com")
            for u in found
        ]
    return list(dict.fromkeys(urls))


def _detect_proxies(explicit: str | None) -> list[str | None]:
    """按优先级构造候选代理列表；``None`` 表示直连。"""
    import os

    out: list[str | None] = []
    if explicit:
        out.append(explicit)
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(var)
        if val:
            out.append(val)
            break
    for port in (7897, 7890, 10809, 1080):
        candidate = f"http://127.0.0.1:{port}"
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                pass
            if candidate not in out:
                out.append(candidate)
        except OSError:
            continue
    out.append(None)  # 最后兜底直连
    return out


async def _new_client(proxies: list[str | None]) -> tuple[httpx.AsyncClient, str | None]:
    """依次试代理，返回第一个能通 Google 的 client（含所选代理）。"""
    for proxy in proxies:
        try:
            client = httpx.AsyncClient(
                proxy=proxy, timeout=20, headers=_HEADERS, follow_redirects=True
            )
            # 用一个轻量端点验证连通性
            await client.head("https://fonts.googleapis.com", timeout=8)
            label = proxy or "直连"
            print(f"  使用网络出口: {label}")
            return client, proxy
        except Exception:
            try:
                await client.aclose()
            except Exception:
                pass
    raise SystemExit("✗ 所有网络出口都连不上 fonts.googleapis.com，请检查代理")


async def _download(client: httpx.AsyncClient, url: str, dst: Path) -> int:
    """下载单个文件（存在即跳过），返回写入字节数。"""
    if dst.exists() and dst.stat().st_size > 0:
        return 0
    for attempt in range(3):
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            dst.parent.mkdir(parents=True, exist_ok=True)
            data = resp.content
            tmp = dst.with_suffix(dst.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(dst)
            return len(data)
        except Exception as e:
            if attempt == 2:
                raise RuntimeError(f"下载失败 {url}: {e!s}") from e
            await asyncio.sleep(1.5 * (attempt + 1))
    return 0


async def _cache_css2(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    css2_url: str,
    dry: bool,
) -> tuple[str, int, int]:
    """缓存一条 css2：下载 CSS + 全部字体切片，返回 (主题标签, 文件数, 字节数)。"""
    query = css2_url.split("?", 1)[1]
    css_dst = CSS_DIR / f"{_slug(css2_url)}.css"
    async with sem:
        resp = await client.get(css2_url)
        resp.raise_for_status()
        css = resp.text
    font_urls = list(dict.fromkeys(_URL_RE.findall(css)))

    if dry:
        return query[:60], len(font_urls), 0
    if css_dst.exists():
        return query[:60], 0, 0

    async def _one(u: str) -> int:
        async with sem:
            return await _download(client, u, FILES_DIR / f"{hashlib.md5(u.encode()).hexdigest()[:16]}.woff2")

    results = await asyncio.gather(*[_one(u) for u in font_urls], return_exceptions=True)
    errors = [r for r in results if isinstance(r, Exception)]
    if errors:
        raise errors[0]
    total = sum(r for r in results if isinstance(r, int))

    def _rewrite(m: re.Match[str]) -> str:
        return f"url(../files/{hashlib.md5(m.group(1).encode()).hexdigest()[:16]}.woff2)"

    CSS_DIR.mkdir(parents=True, exist_ok=True)
    css_dst.write_text(_URL_RE.sub(_rewrite, css), encoding="utf-8")
    return query[:60], len(font_urls), total


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", help="显式指定代理，如 http://127.0.0.1:7897")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不下载")
    args = ap.parse_args()

    css2_urls = _collect_css2_urls()
    print(f"扫描到 {len(css2_urls)} 条 css2 引用（按主题合并过）")
    for u in css2_urls:
        print(f"  · {u[:110]}")

    client, _ = await _new_client(_detect_proxies(args.proxy))
    sem = asyncio.Semaphore(8)
    try:
        n_files = n_bytes = 0
        for u in css2_urls:
            label, n, b = await _cache_css2(client, sem, u, args.dry_run)
            n_files += n
            n_bytes += b
            print(f"  ✓ {label}… 字体切片 {n} 个 / {b / 1048576:.1f}MB")
        if args.dry_run:
            print("（dry-run 结束，未写入任何文件）")
            return 0

        # miku 的 lucide 图标库（约 0.3MB），模板改为相对引用后自动内联
        n = await _download(client, LUCIDE_URL, LUCIDE_DST)
        if n:
            print(f"  ✓ lucide.js 缓存 {n / 1024:.0f}KB → {LUCIDE_DST.name}")
        else:
            print("  · lucide.js 已存在，跳过")

        print(f"完成：新下载 {n_files} 个字体文件，共 {n_bytes / 1048576:.1f}MB")
        print(f"缓存目录：{FONTS_DIR}")
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

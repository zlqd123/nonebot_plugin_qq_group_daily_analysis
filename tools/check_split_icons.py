"""分段渲染图标回归检查：验证上/下半图里的 SVG 图标都不丢。

背景：atri 主题的图标全部来自 body 顶部的零尺寸 SVG 精灵表
（<symbol> 集合），分段隐藏脚本若把它 display:none，两半图里的
<use> 引用全部失效。本脚本复用 smoke_test 的假数据渲染 atri 分段
HTML，再用 playwright（msedge）实际截图，肉眼比对图标。

用法（建议用宿主 python-env 跑，里面有 playwright）::

    python tools/check_split_icons.py

产物在 tools/split_check_out/：split_top.html / split_bottom.html
与对应 png。图标正常时两半图都应能看到彩色小图标。
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # import smoke_test

import smoke_test as st  # noqa: E402,F401  导入即完成 sdk 桩

from maibot_plugin_qq_group_daily_analysis.core import collect, themes  # noqa: E402
from maibot_plugin_qq_group_daily_analysis.core.asset_server import (  # noqa: E402
    start_asset_server,
    stop_asset_server,
)
from maibot_plugin_qq_group_daily_analysis.core.stats import analyze  # noqa: E402

OUT = Path(__file__).resolve().parent / "split_check_out"

CAPTURED: dict[str, str] = {}


async def _capture_renderer(html: str, width: int) -> bytes:
    """按注入脚本里的 mode 参数截获上/下半图 HTML。"""
    if "const mode = 'after'" in html:
        CAPTURED["top"] = html
    elif "const mode = 'before'" in html:
        CAPTURED["bottom"] = html
    else:
        CAPTURED["full"] = html
    return b"\x89PNG\r\n\x1a\n" + b"0" * 64


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    start_asset_server(themes.THEME_DIR)
    themes.set_html_renderer(_capture_renderer)
    try:
        archive = await collect.fetch_group_archive(
            st.FakeHost(), 123456789, history_lens=20000, source="message_api"
        )
        stats = analyze(archive)
        analysis = json.loads(st.ANALYSIS_JSON)
        split = await themes.render_theme_image_split(
            archive, stats, analysis, style="atri", width=900
        )
        # 假渲染器对两半返回相同字节，split 可能是 None（上下图一致保护）；
        # 只要 HTML 都截获到就能继续做图标检查
        if "top" not in CAPTURED or "bottom" not in CAPTURED:
            print(f"✗ 只截获到 {list(CAPTURED)}，缺少分段 HTML（split={split is not None}）")
            return 1

        for key in ("top", "bottom"):
            (OUT / f"split_{key}.html").write_text(CAPTURED[key], encoding="utf-8")

        # 无头浏览器截图：图标靠 <use> 引用精灵表，截图见真章
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge", headless=True)
            page = await browser.new_page(viewport={"width": 900, "height": 1200})
            for key in ("top", "bottom"):
                await page.goto((OUT / f"split_{key}.html").as_uri())
                try:
                    await page.wait_for_load_state("networkidle", timeout=30_000)
                except Exception:
                    pass  # Google 字体可能拉不动，不阻塞检查
                await page.wait_for_timeout(800)
                await page.screenshot(path=str(OUT / f"split_{key}.png"), full_page=True)
                n_use = CAPTURED[key].count("<use href=")
                # 精灵表是否保留可见：body 顶部的 <symbol> 集合没被 display:none
                sprite_ok = 'id="gdr-split-marker"' in CAPTURED[key]
                print(f"  [{key}] <use> 引用 {n_use} 个，截图 split_{key}.png，含标记={sprite_ok}")
            await browser.close()
        print("✓ 两半 HTML 已截图，请肉眼确认图标（心形/星形/闪光等彩色小图标）")
        return 0
    finally:
        stop_asset_server()
        themes.set_html_renderer(None)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    sys.exit(asyncio.run(main()))

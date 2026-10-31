"""不生图的静态自检：主题表、素材、入参映射、配置模型是否都自洽。

不依赖 MaiBot 宿主运行时，也不需要配任何 key（``maibot_sdk`` 以最小桩替代）::

    python tools/selfcheck.py
"""

import asyncio
import re
import sys
import types as t
from pathlib import Path

# Windows GBK 控制台也能打出 ✓/✗
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

# 插件根目录 = 本文件所在的 tools/ 的上一级，跟着仓库走而不是写死本机路径
PLUG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUG.parent))

# ── maibot_sdk 最小桩：config_model 只用到 Field / PluginConfigBase ──
try:
    import pydantic
except ImportError as e:  # pragma: no cover
    print(f"selfcheck 需要 pydantic: {e!s}")
    sys.exit(2)

_sdk = t.ModuleType("maibot_sdk")
_sdk.Field = pydantic.Field
_sdk.PluginConfigBase = pydantic.BaseModel


def _sdk_getattr(name: str):
    mapping = {"Field": pydantic.Field, "PluginConfigBase": pydantic.BaseModel}
    try:
        return mapping[name]
    except KeyError as e:
        raise AttributeError(name) from e


_sdk.__getattr__ = _sdk_getattr  # type: ignore[method-assign]
sys.modules["maibot_sdk"] = _sdk

from maibot_plugin_qq_group_daily_analysis.core import report, sensitive, themes  # noqa: E402
from maibot_plugin_qq_group_daily_analysis.core.collect import resolve_window  # noqa: E402
from maibot_plugin_qq_group_daily_analysis.core.comicgen import (  # noqa: E402
    build_target_url,
    classify_api_error,
    parse_size,
    snap_to_native,
    validate_image_bytes,
)
from maibot_plugin_qq_group_daily_analysis.core.config_model import (  # noqa: E402
    GroupDailyAnalysisConfig,
)
from maibot_plugin_qq_group_daily_analysis.core.llm import (  # noqa: E402
    build_scene_rewrite_prompt,
    build_storyboard_prompt,
    extract_scene,
    strip_reasoning,
)
from maibot_plugin_qq_group_daily_analysis.core.scheduling import (  # noqa: E402
    parse_cron,
    shift_cron_back,
)
from maibot_plugin_qq_group_daily_analysis.core.settings import GdrConfig  # noqa: E402

ok = True

SET = re.compile(r"\{%\s*set\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*=")
# {% for x in y %}：x 和 y 都是模板内部的名字，两边都要排除
FOR = re.compile(
    r"\{%\s*for\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+in\s+([a-zA-Z_][a-zA-Z0-9_]*)"
)

# 1. 主题表与目录一一对应
print("=== 主题表 vs 磁盘目录 ===")
dirs = {p.name for p in themes.THEME_DIR.iterdir() if p.is_dir()}
for s in themes.REPORT_STYLES:
    has = s in dirs
    ok = ok and has
    print(f"  {'✓' if has else '✗'} {s:<10} 目录存在={has}")
extra = dirs - set(themes.REPORT_STYLES) - {"_fonts"}  # _fonts 是 fetch_webfonts 的字体缓存，非主题
print(f"  多余目录: {extra or '无'}")
ok = ok and not extra

# 2. 每套主题必需的模板都在
print("\n=== 每套主题的模板完整性 ===")
NEED = ("image_template.html", "activity_chart.html", "chat_quality_item.html",
        "quote_item.html", "topic_item.html", "user_title_item.html")
for s in themes.REPORT_STYLES:
    miss = [n for n in NEED if not (themes.THEME_DIR / s / n).exists()]
    ok = ok and not miss
    print(f"  {'✓' if not miss else '✗'} {s:<10} 缺: {miss or '无'}")

# 3. 入参覆盖：主模板比顶层上下文，子模板比 themes.py 实际传给它的
print("\n=== 模板入参覆盖检查 ===")

# 各子模板的入参由 themes.py 显式传入，不来自顶层上下文
SUB_CTX = {
    "activity_chart.html": {"chart_data"},
    "topic_item.html": {"topics", "t2i_atri_font_mirror"},
    "quote_item.html": {"quotes", "t2i_atri_font_mirror"},
    "user_title_item.html": {"titles", "t2i_atri_font_mirror"},
    "chat_quality_item.html": {"title", "subtitle", "summary", "dimensions"},
}
# jinja 内建 + {% import %} 进来的东西，不是我们该提供的
BUILTIN = {"loop", "inline_assets", "self"}


def incoming(text: str) -> set[str]:
    """取出一个模板里真正需要外部传入的变量名。

    Args:
        text: 模板源码。

    Returns:
        变量名集合。
    """
    found = {m.group(1) for m in re.finditer(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)", text)}
    found |= {m.group(1) for m in re.finditer(r"\{%\s*if\s+([a-zA-Z_][a-zA-Z0-9_]*)", text)}
    found -= set(SET.findall(text))       # {% set x = ... %} 的局部变量
    for tgt, src in FOR.findall(text):    # {% for x in y %}：x、y 都是内部的
        found -= {tgt, src}
    found -= BUILTIN
    return {v for v in found if not v.endswith("_html")}


for s in themes.REPORT_STYLES:
    d = themes.THEME_DIR / s
    provided = (
        {"current_date", "current_datetime", "group_name", "message_count",
         "participant_count", "total_characters", "emoji_count",
         "most_active_period", "prompt_tokens", "completion_tokens",
         "total_tokens", "t2i_font_source"}
        | set(themes._FONT_MIRRORS)
        | set(themes._THEMES[s].get("extra", {}))
    )
    if local := themes._THEMES[s].get("local_mirror"):
        provided.add(local)

    problems = []
    main = d / "image_template.html"
    miss = incoming(main.read_text(encoding="utf-8")) - provided
    if miss:
        problems.append(f"主模板缺 {sorted(miss)}")
    for name, ctx in SUB_CTX.items():
        f = d / name
        if not f.exists():
            continue
        # 子模板内部还会用 mirror 变量拼素材路径
        sub_provided = set(ctx)
        if local := themes._THEMES[s].get("local_mirror"):
            sub_provided.add(local)
        m2 = incoming(f.read_text(encoding="utf-8")) - sub_provided
        if m2:
            problems.append(f"{name} 缺 {sorted(m2)}")
    ok = ok and not problems
    print(f"  {'✓' if not problems else '✗'} {s:<10} {'; '.join(problems) or '全部覆盖'}")

# 4. 入参映射本身
print("\n=== 数据映射 ===")
tp = themes._topics([{"title": "A", "detail": "d", "users": ["u1", "u2"]},
                     {"title": "", "detail": "x", "users": []}])
print(f"  话题  2 条 -> 保留 {len(tp)} 条（空标题应被丢弃）: {tp[0]['contributors']}")
ok = ok and len(tp) == 1

qs = themes._quotes([{"text": "q1", "user": "u", "reason": "r"}, {"text": " "}])
print(f"  金句  2 条 -> 保留 {len(qs)} 条（空正文应被丢弃）: {qs[0]['content']}")
ok = ok and len(qs) == 1

ti = themes._titles([{"user": "u", "title": "T", "reason": "r"}, {"user": "", "title": "T"}])
print(f"  称号  2 条 -> 保留 {len(ti)} 条（缺昵称应被丢弃）: {ti[0]['title']}")
ok = ok and len(ti) == 1

cd = themes._chart_data([0, 5, 0, 10] + [0] * 20)
print(f"  图表  hour 类型={type(cd[0]['hour']).__name__}（必须 int） 峰值占比={cd[3]['percentage']}")
ok = ok and isinstance(cd[0]["hour"], int)

qz = themes._quality({"title": "t", "subtitle": "s", "summary": "m",
                      "dimensions": [{"name": "a", "percentage": 30, "comment": "c"},
                                     {"name": "b", "percentage": 30, "comment": "c"}]})
tot = sum(d["percentage"] for d in qz["dimensions"])
print(f"  锐评  2 维 30%+30% -> 归一化 {tot:.1f}%，颜色={[d['color'] for d in qz['dimensions']]}")
ok = ok and abs(tot - 100.0) < 0.01 and all(d.get("color") for d in qz["dimensions"])


def _check_sensitive(checker, entries):
    """敏感词筛查回归：用例全部从词库文件里取，代码不含任何具体词条。

    Returns:
        全部通过时返回 ``True``。
    """
    good = True

    def hit_case(label, text, expect_hit):
        nonlocal good
        hits = checker.scan(text)
        ok = bool(hits) == expect_hit
        good = good and ok
        print(f"  {'✓' if ok else '✗'} {label} 命中={bool(hits)} 期望={expect_hit}")

    # 正例①原词：取词库中前几个互不相同的词条，原样应命中
    # 正例②拆写：多字词条按字插入空格（归一化后仍应命中）
    # 正例③大小写 + leet：ASCII 词条全大写应命中
    # 负例：与群聊无关的日常文本（含英文）不得误报
    sample_cjk = [e for e in entries if len(e.folded) >= 4 and not e.folded.isascii()][:3]
    sample_ascii = [e for e in entries if len(e.folded) >= 4 and e.folded.isascii()][:3]
    for e in sample_cjk:
        hit_case(f"原词({len(e.word)}字)", e.word, True)
        hit_case(f"拆写({len(e.word)}字)", " ".join(e.word), True)
    for e in sample_ascii:
        hit_case(f"大写词条({len(e.word)}字符)", e.word.upper(), True)
    # 负例需要与词库实际词条避开：随机拼接的日常长句
    hit_case("日常中文负例", "今天一起吃了午饭，游戏五连跪，明天下雨记得带伞", False)
    hit_case("日常英文负例", "project deadline tomorrow, please review the docs", False)
    return good


# 5. 敏感词筛查（可选词库：文件不在/为空时整段静默跳过）
print("\n=== 敏感词库与匹配 ===")
if sensitive.PACKAGED_LEXICON.exists():
    entries = sensitive.load_entries([sensitive.PACKAGED_LEXICON])
    if entries:
        has_lex = len(entries) > 1000
        print(f"  {'✓' if has_lex else '✗'} 词库文件 词条数={len(entries)}（应 > 1000）")
        ok = ok and has_lex
        _checker = sensitive.SensitiveChecker(entries)
        ok = _check_sensitive(_checker, entries) and ok
    else:
        print("  - 词库文件存在但为空，静默跳过筛查检查")
else:
    print("  - 未找到词库文件（可选组件），静默跳过筛查检查")

# 6. 图片段是通用 dict（{"type","data":{"b64"}}），发送层按 type 分流
seg = report.to_image_segment(b"\x89PNG\r\n\x1a\nfake")
seg_ok = seg.get("type") == "image" and str((seg.get("data") or {}).get("b64") or "").strip()
print(f"  {'✓' if seg_ok else '✗'} to_image_segment 消息段 type={seg.get('type')} 带b64={bool(seg_ok)}")
ok = ok and bool(seg_ok)

# 7. 子模板渲染出的素材路径必须是相对路径（内联前）
print("\n=== 子模板素材路径（必须是相对路径）===")


async def check_asset_paths() -> bool:
    """渲染各子模板，检查产出的素材路径可解析。

    Returns:
        全部为可解析路径时返回 ``True``。
    """
    quotes = [{"text": "想去一直没去", "user": "某人", "reason": "精准"}]
    titles = [{"user": "某人", "title": "摸鱼王", "reason": "整天摸鱼"}]
    quality = {"title": "t", "subtitle": "s", "summary": "m",
               "dimensions": [{"name": "水群", "percentage": 100, "comment": "表情包"}]}

    good = True
    for style in themes.REPORT_STYLES:
        env = themes._env(style)
        conf = themes._THEMES[style]
        frag_ctx = {
            **themes._FONT_MIRRORS,
            "t2i_font_source": themes._FONT_SOURCE,
            **conf.get("extra", {}),
        }
        if local := conf.get("local_mirror"):
            frag_ctx[local] = "."
        for tpl, ctx in (
            ("quote_item.html", {"quotes": themes._quotes(quotes)}),
            ("user_title_item.html", {"titles": themes._titles(titles)}),
            ("chat_quality_item.html", themes._quality(quality)),
        ):
            html = await themes._frag(env, tpl, **ctx, **frag_ctx)
            found = re.findall(r'(?:src="([^"]+)"|url\([\'"]?([^\'")]+))', html)
            paths = [a or b for a, b in found]
            paths = [p for p in paths
                     if "/file/" in p or any(e in p for e in (".png", ".gif", ".webp"))]
            if not paths:
                continue
            # 远程 CDN 正常；本地素材必须是 ./ 开头的相对路径
            bad = [p for p in paths if p.startswith("/") or "://" not in p and not p.startswith(".")]
            good = good and not bad
            print(f"  {'✓' if not bad else '✗'} [{style:<9}] {tpl:<22} "
                  f"{len(paths)} 个引用  例: {paths[0]}")
    return good


ok = asyncio.run(check_asset_paths()) and ok

# 8. 本地素材改写：小文件内联 data URI，大文件走本地素材服务器
print("\n=== 本地素材改写 ===")


def check_inline_assets() -> bool:
    """验证小素材内联、大素材走素材服务器、远程引用不动。

    Returns:
        全部通过时返回 ``True``。
    """
    from maibot_plugin_qq_group_daily_analysis.core import asset_server

    good = True
    atri = themes.THEME_DIR / "atri"
    sample = next(
        (f for f in sorted(atri.rglob("*"))
         if f.is_file() and f.suffix.lower() in (".png", ".gif", ".webp")
         and f.stat().st_size < 512 * 1024),
        None,
    )
    if sample is not None:
        rel = sample.relative_to(atri).as_posix()
        html = themes._inline_assets(f'<img src="./{rel}">', "atri")
        ok = "data:image/" in html and f"./{rel}" not in html
        good = good and ok
        print(f"  {'✓' if ok else '✗'} 小素材内联 data URI: {rel}")
    else:
        print("  - 无小于 512KB 的图片素材，跳过内联用例")

    # 大文件（字体）：启动素材服务器后应改写为 127.0.0.1 URL
    big = next(
        (f for f in sorted(atri.rglob("*"))
         if f.is_file() and f.suffix.lower() in (".woff2", ".ttf", ".otf")
         and f.stat().st_size > 512 * 1024),
        None,
    )
    base_url = asset_server.start_asset_server(themes.THEME_DIR)
    if big is not None and base_url:
        rel = big.relative_to(atri).as_posix()  # 引用相对主题目录
        html = themes._inline_assets(
            f'<style>@font-face {{ src: url("./{rel}") }}</style>', "atri"
        )
        ok = f"{base_url}/atri/{rel}" in html and "./" not in html
        good = good and ok
        print(f"  {'✓' if ok else '✗'} 大素材改写到本地服务器: atri/{rel}")
    elif big is None:
        print("  - 无大字体素材，跳过服务器用例")
    else:
        good = False
        print("  ✗ 素材服务器未能启动")

    # 远程与 data URI 不被二次处理
    keep = themes._inline_assets(
        '<img src="https://a.b/c.png"><img src="data:image/png;base64,AAA">', "atri"
    )
    same = keep.count("src=") == 2 and "data:image/png;base64,AAA" in keep
    print(f"  {'✓' if same else '✗'} 远程/已有 data URI 引用保持原样")
    asset_server.stop_asset_server()
    return good and same


ok = check_inline_assets() and ok

# 9. 生图与文字模型工具函数
print("\n=== 模型工具函数 ===")
good = strip_reasoning("<think>x</think>正文") == "正文"
good = good and strip_reasoning("<think>未闭合") == ""
good = good and strip_reasoning("前置</think>正文") == "正文"
scene = extract_scene('{"scene": "A 2-panel comic strip"}')
good = good and scene == "A 2-panel comic strip"
good = good and extract_scene("不是 json") is None
print(f"  {'✓' if good else '✗'} strip_reasoning / extract_scene")

good = classify_api_error("400 InputTextSensitiveContentDetected")[0] is True
good = good and classify_api_error("429 Too Many Requests")[1] is True
good = good and classify_api_error("server on fire") == (False, False)
print(f"  {'✓' if good else '✗'} classify_api_error 敏感/限流分类")

good = build_target_url("", "doubao").endswith("/api/v3/images/generations")
good = good and build_target_url("https://api.openai.com/v1", "images").endswith("/images/generations")
good = good and build_target_url("https://api.openai.com/v1", "chat").endswith("/chat/completions")
print(f"  {'✓' if good else '✗'} build_target_url URL 补全")

good = parse_size("2048x1152") == (2048, 1152) and parse_size("bad") is None
good = good and snap_to_native((2048, 2048), [(1024, 1024), (1792, 1024)]) == (1024, 1024)
try:
    validate_image_bytes(b"<!doctype html><html>")
    good = good and False
except ValueError:
    good = good and True
print(f"  {'✓' if good else '✗'} parse_size / snap_to_native / validate_image_bytes")
ok = ok and good

# 10. 定时工具
print("\n=== 定时工具 ===")
good = parse_cron("0 14,22 * * *")["hour"] == "14,22"
shifted = shift_cron_back(parse_cron("0 14,22 * * *"), 8)
good = good and shifted == {
    "minute": "52", "hour": "13,21", "day": "*", "month": "*", "day_of_week": "*"
}
good = good and shift_cron_back(parse_cron("0 0 * * *"), 8) is None  # 跨天放弃
good = good and shift_cron_back(parse_cron("*/5 * * * *"), 8) is None  # 通配放弃
print(f"  {'✓' if good else '✗'} parse_cron / shift_cron_back（含跨天与通配拒绝）")
w = resolve_window("today")
good = w[0] <= w[1] and w[2] == "今日"
w2 = resolve_window("hours:3")
good = good and w2[1] - w2[0] == 3 * 3600
print(f"  {'✓' if good else '✗'} resolve_window")
ok = ok and good

# 11. 配置模型与 gdr_* 视图
print("\n=== 配置模型与扁平视图 ===")
cfg = GroupDailyAnalysisConfig()
view = GdrConfig(cfg)
good = view.gdr_timezone == "Asia/Shanghai" and view.gdr_report_style == "scrapbook"
good = good and view.gdr_group_schedules == [] and view.gdr_member_counts == {}
good = good and view.gdr_text_api_keys == [] and not view.gdr_text_base_url
cfg2 = GroupDailyAnalysisConfig.model_validate({
    "basic": {"group_schedules": [{"group_id": "123456789", "time": "14:30"}]},
    "fetch": {"member_counts": {"123": 45}, "bot_qq": "10000"},
    "report": {"style": "miku"},
})
view2 = GdrConfig(cfg2)
good = good and view2.gdr_group_schedules == [
    {"group_id": 123456789, "time": "14:30", "enable": True}
]
good = good and view2.gdr_member_counts == {123: 45} and view2.gdr_bot_qq == "10000"
good = good and view2.gdr_report_style == "miku"
print(f"  {'✓' if good else '✗'} 默认值 / 群时刻规整 / 成员数覆盖表")

# 分镜提示词（含降级模式）构造
topics = [{"title": "话题", "detail": "详情"}]
sys_prompt, user_prompt = build_storyboard_prompt(topics, view2)
good = "Panel 1" in sys_prompt and "scene" in user_prompt
sys_deg, _ = build_storyboard_prompt(topics, view2, include_context=False)
good = good and "Background Context" not in sys_deg
rw_sys, rw_user = build_scene_rewrite_prompt("scene text", hits=["词A"])
good = good and "改写" in rw_sys and "词A" in rw_user
print(f"  {'✓' if good else '✗'} 分镜/降级分镜/改写提示词构造")
ok = ok and good

print("\n" + ("=== 全部通过 ===" if ok else "=== 存在失败 ==="))
sys.exit(0 if ok else 1)

"""不生图的静态自检：主题表、素材、入参映射是否都自洽。

不依赖 nonebot 运行时，也不需要配任何 key：

    python tools/selfcheck.py
"""

import asyncio
import importlib.util
import re
import sys
import types as t
from pathlib import Path

# 插件根目录 = 本文件所在的 tools/ 的上一级，跟着仓库走而不是写死本机路径
PLUG = Path(__file__).resolve().parent.parent
PKG = PLUG.name  # 用真实包名注册，模块间相对导入才成立
sys.path.insert(0, str(PLUG.parent))

pkg = t.ModuleType(PKG)
pkg.__path__ = [str(PLUG)]
sys.modules[PKG] = pkg
for name in ("nonebot", "nonebot.log"):
    mod = t.ModuleType(name)
    mod.__getattr__ = lambda n: (lambda *a, **k: None)  # noqa: E731
    sys.modules[name] = mod
sys.modules["nonebot.log"].logger = type("L", (), {
    k: staticmethod(lambda *a: None) for k in ("debug", "info", "warning", "error")})
_hr = t.ModuleType("nonebot_plugin_htmlrender")


async def _stub(**_kw):
    """本脚本不渲染。"""
    raise NotImplementedError


_hr.template_to_pic = _stub
# themes.py 从子模导入 get_new_page（分段渲染用）。selfcheck 不渲染，
# 但导入必须成立，否则 load("themes") 会 ModuleNotFoundError。
_browser_mod = t.ModuleType("nonebot_plugin_htmlrender.browser")
_browser_mod.get_new_page = _stub
_hr.browser = _browser_mod
sys.modules["nonebot_plugin_htmlrender.browser"] = _browser_mod
sys.modules["nonebot_plugin_htmlrender"] = _hr
import nonebot  # noqa: E402

nonebot.require = lambda *a, **k: _hr


def load(name):
    """按路径加载模块。"""
    s = importlib.util.spec_from_file_location(f"{PKG}.{name}", PLUG / f"{name}.py")
    m = importlib.util.module_from_spec(s)
    sys.modules[f"{PKG}.{name}"] = m
    s.loader.exec_module(m)
    return m


load("stats")
load("report")
report = load("report")
load("themes")
themes = load("themes")

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
extra = dirs - set(themes.REPORT_STYLES)
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
#    （早先按目录整体扫，把子模板入参和 jinja 的 loop/import 也算成缺失，全是误报）
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


# 5. 子模板渲染出的素材路径必须是相对路径。
#    这项防的是一个很隐蔽的坑：ATRI 的 quote_item / user_title_item 里写的是
#    `{{ t2i_atri_font_mirror }}/file/x.gif`，而**渲染子模板时若没把这个变量传进去**，
#    Jinja 会把未定义变量渲染成空串，拼出来就是 `/file/x.gif`——
#    文件系统根目录的绝对路径，浏览器必然加载失败，而且不报错、静悄悄地裂图。
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


print("\n=== 子模板素材路径（必须是相对路径）===")
ok = asyncio.run(check_asset_paths()) and ok

print("\n" + ("=== 全部通过 ===" if ok else "=== 存在失败 ==="))
sys.exit(0 if ok else 1)

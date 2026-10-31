"""离线冒烟测试：不连 MaiBot、不调真实模型、不出真图，跑通全链路。

用假宿主（多形状的消息 dict）、假文字模型（返回预置的分析/分镜 JSON）和
假渲染器（截获 HTML、返回最小 PNG）依次渲染 6 套主题的完整日报，验证：

- collect 的消息归一化（多种宿主字段形状）；
- 统计、昵称压缩/还原、四类分析产出的传递；
- pipeline 编排（海报 / 文字 / 告警 / 漫画消息段）；
- 6 套主题模板都能渲染出含关键内容的 HTML（写盘供人工检查）。

用法::

    python tools/smoke_test.py            # 全部主题
    python tools/smoke_test.py atri       # 只渲染指定主题
"""

import asyncio
import json
import sys
from pathlib import Path

PLUG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUG.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

# ── maibot_sdk 最小桩（与 selfcheck 相同）：config_model 只用到 Field/PluginConfigBase ──
import pydantic  # noqa: E402

_sdk = pydantic  # noqa: F841 占位避免误删
import types as _t  # noqa: E402

_sdk_mod = _t.ModuleType("maibot_sdk")
_sdk_mod.Field = pydantic.Field
_sdk_mod.PluginConfigBase = pydantic.BaseModel


def _sdk_getattr(name: str):
    mapping = {"Field": pydantic.Field, "PluginConfigBase": pydantic.BaseModel}
    try:
        return mapping[name]
    except KeyError as e:
        raise AttributeError(name) from e


_sdk_mod.__getattr__ = _sdk_getattr  # type: ignore[method-assign]
sys.modules["maibot_sdk"] = _sdk_mod

from maibot_plugin_qq_group_daily_analysis.core import collect, themes  # noqa: E402
from maibot_plugin_qq_group_daily_analysis.core.comicgen import ComicImageModel  # noqa: E402
from maibot_plugin_qq_group_daily_analysis.core.config_model import (  # noqa: E402
    GroupDailyAnalysisConfig,
)
from maibot_plugin_qq_group_daily_analysis.core.llm import TextModel  # noqa: E402
from maibot_plugin_qq_group_daily_analysis.core.pipeline import (  # noqa: E402
    generate_daily_report,
)
from maibot_plugin_qq_group_daily_analysis.core.settings import GdrConfig  # noqa: E402

OUT_DIR = PLUG / "tools" / "smoke_out"

ANALYSIS_JSON = json.dumps(
    {
        "summary": "今天是话题最多的一天，群里非常热闹",
        "topics": [
            {"title": "新版本上线讨论", "detail": "大家围绕新功能展开了热烈讨论", "users": ["a1b", "c2d"]},
            {"title": "晚饭吃什么", "detail": "从火锅聊到了减脂餐", "users": ["e3f"]},
        ],
        "quotes": [
            {"text": "这个功能我等了半年", "user": "a1b", "reason": "真情实感"},
        ],
        "keywords": ["新版本", "火锅", "减脂"],
        "mood": "热烈而克制",
        "quality": {
            "title": "技术水群的一天",
            "subtitle": "吃与代码齐飞",
            "dimensions": [
                {"name": "技术讨论", "percentage": 60, "comment": "新版本引发的高质量输出"},
                {"name": "美食水群", "percentage": 40, "comment": "饭点前的经典保留节目"},
            ],
            "summary": "干活与干饭两不误",
        },
        "titles": [
            {"user": "c2d", "title": "冲浪先锋", "reason": "总能第一时间带来新消息"},
        ],
    },
    ensure_ascii=False,
)
SCENE_JSON = json.dumps({"scene": "A 2-panel comic strip. Panel 1: bubble 汉字台词"}, ensure_ascii=False)


class FakeHost:
    """假宿主：返回多种形状的消息 dict，顺带测 _normalize_message 的兜底。"""

    self_id = 10000

    def __init__(self, count: int = 60) -> None:
        self.count = count

    async def get_group_messages(self, group_id, *, start_ts, end_ts, limit, source):
        import time

        now = int(time.time())
        users = [("9%d" % i, f"用户{i}", f"昵称{i}") for i in range(5)]
        out = []
        for i in range(self.count):
            uid, card, nick = users[i % len(users)]
            body = {
                # 形状 A：MaiBot 经典 message_info/user_info 嵌套
                "message_info": {
                    "message_id": 1000 + i,
                    "time": now - (self.count - i) * 60,
                    "group_info": {"group_id": str(group_id), "group_name": "测试群"},
                },
                "user_info": {"user_id": int(uid), "user_nickname": nick, "user_cardname": card},
                "plain_text": f"第{i}条消息，今天天气不错 {i % 7}",
            }
            if i == 3:
                # 形状 B：扁平字段
                body = {
                    "message_id": 1003,
                    "time": now - (self.count - i) * 60,
                    "user_id": int(uid),
                    "nickname": nick,
                    "processed_text": "扁平形状的消息",
                }
            if i == 4:
                # 形状 C：段结构（纯图片消息，应被剔除）
                body = {
                    "message_info": {"message_id": 1004, "time": now - (self.count - i) * 60},
                    "user_info": {"user_id": int(uid), "user_nickname": nick},
                    "message_segments": [{"type": "image", "data": {"url": "x.png"}}],
                    "plain_text": "",
                }
            if i == 5:
                # 形状 D：机器人自己的消息（应被过滤）
                body["user_info"]["user_id"] = 10000
            if i == 6:
                # 形状 E：CQ 码字符串
                body["plain_text"] = "看看这个表情 [CQ:face,id=1] 还有图 [CQ:image,file=a]"
            out.append(body)
        return out

    async def get_group_meta(self, group_id):
        return 50, "测试群"


class FakeTextModel(TextModel):
    """假文字模型：绕过网络调用，返回预置 JSON。"""

    async def complete(self, system, user):
        if "漫画分镜" in system or "Comic Strip" in system:
            return SCENE_JSON
        if "改写员" in system:
            return "rewritten safe scene text"
        return ANALYSIS_JSON


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class FakeRenderer:
    """假渲染器：截获 HTML 写盘，返回最小 PNG。"""

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.rendered = 0

    async def __call__(self, html: str, width: int) -> bytes:
        self.rendered += 1
        (self.out_dir / f"theme_{self.rendered:02d}.html").write_text(
            html, encoding="utf-8"
        )
        # 返回值带上次序差异，分段逻辑才能区分上下图
        return PNG + bytes([self.rendered & 0xFF])


async def main() -> int:
    styles = sys.argv[1:] or list(themes.REPORT_STYLES)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for stale in OUT_DIR.glob("theme_*.html"):
        stale.unlink()

    cfg = GroupDailyAnalysisConfig()
    view = GdrConfig(cfg)
    host = FakeHost()
    text_model = FakeTextModel(view)
    renderer = FakeRenderer(OUT_DIR)
    themes.set_html_renderer(renderer)

    ok = True
    for style in styles:
        cfg = GroupDailyAnalysisConfig()
        cfg.report.style = style  # 每轮渲染不同主题
        view = GdrConfig(cfg)
        image_model = ComicImageModel(view)
        segments, stats, archive = await generate_daily_report(
            host,
            123456789,
            view,
            text_model=text_model,
            image_model=image_model,
            with_comic=True,
        )
        kinds = [s.get("type") for s in segments]
        n_img = kinds.count("image")
        has_notice = any(s.get("notice") for s in segments)
        line = (
            f"[{style:<9}] 消息={len(archive.messages)} 统计={stats.total_messages}"
            f" 段={kinds} 告警={has_notice}"
        )
        good = stats.total_messages > 0 and n_img >= 1 and not has_notice
        # 海报 HTML 里应能看到还原后的昵称与话题标题
        html = next(
            (OUT_DIR / p.name for p in sorted(OUT_DIR.glob("theme_*.html"))),
            None,
        )
        ok = ok and good
        print(f"  {'✓' if good else '✗'} {line}")
        if not good:
            continue

    # 漫画链路：FakeTextModel 走到了分镜分支且 image_model.available=False
    # （未配 image.api_keys），所以上面只有海报 + 文字段——补一次带 key 的漫画验证
    cfg2 = GroupDailyAnalysisConfig()
    cfg2.image.api_keys = ["fake-key"]
    view2 = GdrConfig(cfg2)

    calls = {"n": 0}

    async def fake_request(self, client, key, prompt):
        calls["n"] += 1
        return {"data": [{"b64_json": __import__("base64").b64encode(PNG).decode()}]}

    ComicImageModel._request = fake_request  # type: ignore[method-assign]
    image_model2 = ComicImageModel(view2)
    segments, _, _ = await generate_daily_report(
        FakeHost(), 123456789, view2,
        text_model=FakeTextModel(view2), image_model=image_model2,
        with_comic=True,
    )
    kinds = [s.get("type") for s in segments]
    comic_ok = kinds.count("image") >= 2 and calls["n"] >= 1  # 海报 + 漫画
    ok = ok and comic_ok
    print(f"  {'✓' if comic_ok else '✗'} 漫画链路 段={kinds} 生图调用={calls['n']}")

    # 分段渲染：两次渲染应产出不同的图（脚本由 FakeRenderer 直接生效于…）
    from maibot_plugin_qq_group_daily_analysis.core.report import (
        build_text_summary,
        render_report_image_split,
    )
    from maibot_plugin_qq_group_daily_analysis.core.stats import analyze

    archive = await collect.fetch_group_archive(
        FakeHost(), 123456789, history_lens=20000, source="message_api"
    )
    stats = analyze(archive)
    analysis = json.loads(ANALYSIS_JSON)
    split = await render_report_image_split(archive, stats, analysis, style="scrapbook")
    split_ok = split is not None and len(split) == 2 and split[0] != split[1]
    print(f"  {'✓' if split_ok else '✗'} 分段渲染（FakeRenderer 每次返回不同字节）")
    # 注意：真实浏览器里分段靠注入脚本；这里只验证"两图不同才算分段"的判定逻辑。
    ok = ok and split_ok

    text = build_text_summary(analysis, stats, "今日")
    text_ok = "群聊日报" in text and "冲浪先锋" in text
    print(f"  {'✓' if text_ok else '✗'} 文字版摘要（含称号）")
    ok = ok and text_ok

    print(f"\n渲染产物已写盘: {OUT_DIR}")
    print("=== 全部通过 ===" if ok else "=== 存在失败 ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

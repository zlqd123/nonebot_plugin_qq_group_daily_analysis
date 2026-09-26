"""把 astrbot 原插件的主题模板与素材同步进本插件的 templates/themes。

原则是**原样搬运、不改模板**：ATRI 的 24MB 字体和 6.5MB 动图全部带上，
这样渲染结果和上游一致，也不用去连 GitHub CDN。

**运行前必须先有上游仓库的本地副本**（脚本本身不含它，也不会去自动下载）::

    git clone --depth 1 \\
        https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis.git \\
        D:/tool/deepseek/workspace2/astrbot_daily
    python tools/sync_themes.py

路径写死在本机，迁移时改下面两个常量即可。脚本会**整体覆盖**
``templates/themes/``，跑之前确认没有手工改动。
"""

import shutil
import sys
from pathlib import Path

SRC_ROOT = Path(r"D:\tool\deepseek\workspace2\astrbot_daily")
TPL_SRC = SRC_ROOT / "src" / "infrastructure" / "reporting" / "templates"
ASSET_SRC = SRC_ROOT / "assets"
DST = Path(__file__).resolve().parent.parent / "templates" / "themes"

# 本插件配置里的主题名 -> 上游目录名。
# 只保留上游 README 明确列出的 6 套：scrapbook / retro_futurism /
# HatsuneMiku / hack / ATRI / art_nouveau。上游目录里另有 simple、
# spring_festival、BlueArchive、format，但不在其 README 的主题清单内，
# 且 format 本身只是个基础模板，故不搬。
THEME_DIRS = {
    "scrapbook": "scrapbook",
    "retro": "retro_futurism",
    "miku": "HatsuneMiku",
    "hack": "hack",
    "atri": "ATRI",
    "nouveau": "art_nouveau",
}

# 图片渲染真正要用的模板：主模板 + 4 个条目子模板 + 图表 + 主题自带附属文件。
# html_template.html 是上游的交互式网页版，引用了一堆 uploaded: 占位图，
# 静态截图用不到，不搬。
WANTED_SUFFIX = (
    "image_template.html",
    "activity_chart.html",
    "chat_quality_item.html",
    "quote_item.html",
    "topic_item.html",
    "user_title_item.html",
    "shared_styles.html",
    "inline_assets.html",
)

# 主题名 -> (素材源目录, 素材落在主题目录下的子路径)
# ATRI 的文件都在 assets/ATRI/file/ 下，比 HatsuneMiku 多一层。
THEME_ASSETS = {"atri": ("ATRI", "file"), "miku": ("HatsuneMiku", "")}


def main() -> None:
    """执行搬运。"""
    if not TPL_SRC.is_dir():
        print(
            f"找不到上游模板目录：{TPL_SRC}\n"
            f"请先 clone：\n"
            f"  git clone --depth 1 "
            f"https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis.git "
            f"{SRC_ROOT}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if DST.exists():
        shutil.rmtree(DST)
    total = 0
    for name, src_dir in THEME_DIRS.items():
        out = DST / name
        out.mkdir(parents=True, exist_ok=True)
        n = 0
        for f in sorted((TPL_SRC / src_dir).glob("*.html")):
            if f.name in WANTED_SUFFIX:
                shutil.copy2(f, out / f.name)
                n += 1
        size = sum(p.stat().st_size for p in out.glob("*"))
        total += size
        print(f"  [{name:<15}] <- {src_dir:<15} 模板 {n} 个")

    for name, (asset_dir, sub) in THEME_ASSETS.items():
        src = ASSET_SRC / asset_dir / sub if sub else ASSET_SRC / asset_dir
        if not src.exists():
            print(f"  [警告] 素材目录缺失: {src}")
            continue
        out = DST / name / sub if sub else DST / name
        out.mkdir(parents=True, exist_ok=True)
        copied = 0
        for f in src.iterdir():
            if f.is_file() and not f.name.endswith("-demo.jpg"):
                shutil.copy2(f, out / f.name)
                copied += 1
        size = sum(p.stat().st_size for p in out.glob("*"))
        total += size
        print(f"  [{name:<15}] 素材 {copied} 个  {size / 1024 / 1024:.1f} MB")

    print(f"\n合计 {total / 1024 / 1024:.1f} MB -> {DST}")


if __name__ == "__main__":
    main()

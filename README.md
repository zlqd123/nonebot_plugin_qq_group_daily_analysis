<div align="center">

# 群日报 · NoneBot 版

[![NoneBot](https://img.shields.io/badge/NoneBot2-2.x-4e5f95?style=for-the-badge&logo=nonebot&logoColor=white)](https://nonebot.dev)
[![OneBot V11](https://img.shields.io/badge/适配-OneBot%20V11-3b82f6?style=for-the-badge)](https://github.com/botuniverse/onebot-11)
[![Python](https://img.shields.io/badge/Python-%3E%3D3.10-3776ab?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg?style=for-the-badge)](LICENSE)

_✨ 抓群聊历史 → 大模型结构化分析 → 渲染成长图海报推送到群里，可顺带生成漫画。 ✨_

**本插件移植自 [astrbot 版群聊日常分析插件](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis)，
海报主题直接复用其全部 6 套模板与素材。**

</div>

---

- [效果](#效果)
- [功能特色](#功能特色)
- [安装](#安装)
- [配置](#配置)
- [指令](#指令)
- [定时推送](#定时推送)
- [海报主题](#海报主题)
- [与原版的差异](#与原版的差异)
- [常见问题](#常见问题)

## 效果

海报内容包含：统计概览、时段分布、话题讨论、金句摘录、称号评定、聊天质量锐评，
以及可选的日报漫画。六套主题共用同一套数据入参，切换 `gdr_report_style` 即可。

| 配置值 | 主题 | 素材 |
| :--- | :--- | :--- |
| `scrapbook` | 手账 | 仅字体 |
| `retro` | 复古未来 | 仅字体 |
| `miku` | 初音未来 | 本地 6MB 立绘 |
| `hack` | 赛博 | 仅字体 |
| `atri` | 亚托莉 | 本地 34MB（立绘 + 表情 + 3 个 LXGW 字体） |
| `nouveau` | 新艺术运动 | 仅字体 |

> 本仓库不存放日报截图——一张海报动辄几 MB，提交进仓库会迅速膨胀。
> 想加效果图的话，自己生成一次后命名成 `demo.png` 放进对应主题目录即可。

## 功能特色

- **📊 群聊统计** — 消息量、活跃人数、时段分布、贡献榜、表情统计
- **🧠 大模型话题总结** — 话题讨论、金句摘录、称号评定、聊天质量锐评
- **🖼️ 六套海报主题** — 复用 astrbot 原版模板与 40MB 素材，**不连 GitHub CDN**
- **🎨 日报漫画** — 由独立的生图模型生成 4 格分镜漫画
- **⏰ 定时推送** — 支持每群独立时刻，也可退回全局 cron
- **🔒 纯文字分析** — 剔除纯图片与合并转发消息，只分析文字
- **⚙️ 纯 .env 配置** — 全部配置走 `gdr_` 前缀变量，**没有 HTTP 配置页**
- **🔌 模型解耦** — 文字总结模型与生图模型完全独立配置，互不影响

## 安装

### 用 nb-cli 安装

```bash
nb plugin install nonebot_plugin_qq_group_daily_analysis
```

### 手动安装

```bash
git clone -b nonebot https://github.com/zlqd123/nonebot_plugin_qq_group_daily_analysis.git
cp -r nonebot_plugin_qq_group_daily_analysis/nonebot_plugin_qq_group_daily_analysis ./plugins/
```

### 依赖

| 依赖 | 用途 |
| :--- | :--- |
| `nonebot2>=2.3.0` | 框架 |
| `nonebot-adapter-onebot11>=2.3.0` | OneBot V11 适配器 |
| `nonebot-plugin-htmlrender>=0.6.0` | 海报渲染（Playwright/Chromium） |
| `nonebot-plugin-apscheduler>=0.4.0` | 定时推送 |
| `nonebot-plugin-chatrecorder>=0.3.0` | 消息落库（可选用作采集源） |

> ⚠️ 渲染海报需要 Chromium。首次启动 `nonebot-plugin-htmlrender` 会自动下载，
> 约 150MB，请确保容器内有足够空间。

## 配置

在 `.env` 里加以下几行即可跑起来，完整参数见
[包内详细文档](nonebot_plugin_qq_group_daily_analysis/README.md#全部配置项)。

```env
# ── 文字总结模型（必填）────────────────────────────
# 列表类变量要写成 JSON 数组，单个值不要漏引号
gdr_text_api_keys=["sk-xxx"]
gdr_text_model="gemini-2.5-flash"

# ── 海报主题 ─────────────────────────────────────
gdr_report_style="atri"

# ── 生图模型（只有要出漫画时才需要）──────────────
gdr_image_api_keys=["ark-xxx"]
gdr_image_model="doubao-seedream-5-0-flash-260915"
gdr_image_size="2048x2048"

# ── 定时推送 ─────────────────────────────────────
gdr_group_schedules=[{"group_id": 123456, "time": "21:00"}]
```

> 变量名小写 `gdr_` 前缀。nonebot 的配置层不区分大小写，
> 但**列表和字典的值必须是合法 JSON**，写成 `gdr_image_api_keys=xxx` 会在启动时报
> `SettingsError`。

## 指令

| 指令 | 产出 | 说明 |
| :--- | :--- | :--- |
| `群日报` | 海报 | 只渲染海报，不调生图 |
| `群日报漫画` | 漫画 | 只出漫画，省掉一次海报截图 |
| `群日报整体` | 海报 + 漫画 | 两样都发 |

**三条指令共用同一次文字模型分析。** 漫画的绘图提示词是从日报那份 `analysis` 的
`summary` 和 `topics` 派生的，所以「群日报整体」不会为了多出那张图再调一次文字模型——
这与原版 astrbot 插件的做法一致。

指令执行期间**不发「正在采集…请稍候」**，全部发完后补一句「群日报已发送，请查收」。

## 定时推送

```env
# 推荐：每群独立时刻
gdr_group_schedules=[{"group_id": 123456, "time": "21:00"}]

# 或全局 cron（兼容旧配置）
gdr_group_list=[123456]
gdr_push_cron="0 14,22 * * *"
```

定时推送**默认连带生成漫画**（`gdr_enable_comic=true`）。该开关只管定时，
手动指令不受影响。

同一群有任务在跑时会直接拒绝重复触发，生图 API 按次计费，不做无谓重试。

## 海报主题

六套主题**直接复用 astrbot 原插件的模板和素材**，不是照着样子另写的：

| 配置值 | 原版目录 | 素材 |
| :--- | :--- | :--- |
| `scrapbook` | `scrapbook` | 仅字体 |
| `retro` | `retro_futurism` | 仅字体 |
| `miku` | `HatsuneMiku` | 本地 6MB 立绘 |
| `hack` | `hack` | 仅字体 |
| `atri` | `ATRI` | 本地 34MB（立绘 + 表情 + 3 个 LXGW 字体） |
| `nouveau` | `art_nouveau` | 仅字体 |

上游 `templates/` 下还有 `simple`、`spring_festival`、`BlueArchive`，但不在其 README
的主题清单内，故未收录；`format` 本身只是无配色的基础模板。

> 字体仍走公网（Google Fonts / jsDelivr），**渲染环境需能访问外网**，断网会回退到系统字体。

## 与原版的差异

本插件是面向 NoneBot 的移植版，与原版 astrbot 插件并非完全等价：

| 项目 | 说明 |
| :--- | :--- |
| **人格 / Persona** | **未迁移**。原版的人格设定与「人格标签」相关功能不包含在内 |
| **WebUI / HTTP 配置** | **未迁移**。配置全部走 `.env` |
| **增量分析模式** | **未迁移**。本插件每次全量分析时间窗口内的消息 |
| **多平台** | **仅 OneBot V11**。不支持 v12、Satori、QQ 官方、Discord |
| **群相册上传** | **未迁移** |
| **头像** | 不拉取。模板对空值有保护，渲染为空框 |

另有若干针对 nonebot 场景的取舍：同群防重入、生图全局并发上限 2、
缺生图模型时按「定时静默 / 手动告知」区分提示。

## 常见问题

**Q：出图时间是错的，偏了 8 小时？**

如果你在 **Windows 上直接跑**本插件，注意 `.env` 里的 `TZ=Asia/Shanghai` 是 IANA 名，
Windows 的 CPython 解析不了（它只认 `CST-8` 这类 POSIX 写法），解析失败会退回 UTC。
容器（Debian/Linux）有 zoneinfo，不受影响。**仅本机调试时注意。**

**Q：海报图片裂了？**

本仓库 40MB 素材已随包分发，正常不会裂。若仍有问题，先跑一次自检：

```bash
python nonebot_plugin_qq_group_daily_analysis/tools/selfcheck.py
```

它不生图、不需要任何 key，会检查主题表、目录、模板入参、数据映射和素材路径。

**Q：没有中文粗体？**

容器内需装中文字体，否则标题会退化成伪粗体：

```bash
apt-get install -y fonts-noto-cjk
```

> `apt-get` 装的东西不随容器重建保留，请写进镜像或 volume。

**Q：漫画很慢？**

生图是 2048×2048 出图，本来就慢。同一群的重复指令已被锁挡住。

---

## 许可

本插件代码以 MIT 许可发布。海报主题的模板与素材来自
[astrbot_plugin_qq_group_daily_analysis](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis)，
遵循其原始许可；其中 ATRI 主题使用的 LXGW 字体为 SIL OFL 1.1。

详细配置项、参数说明与实现细节见
[nonebot_plugin_qq_group_daily_analysis/README.md](nonebot_plugin_qq_group_daily_analysis/README.md)。

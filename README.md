# maibot_plugin_qq_group_daily_analysis（MaiBot 群日报插件）

从 [astrbot_plugin_qq_group_daily_analysis](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis)
移植的 **MaiBot 1.x** 插件（移植蓝本为 NoneBot 版迭代），功能与素材完整保留：

- **群聊日报**：消息采集 → 统计分析 → 文字模型结构化总结（整体总结 / 话题 / 金句 /
  用户称号 / 聊天质量锐评）→ 渲染海报推送
- **6 套原版主题海报**：`scrapbook` 手账 / `retro` 复古未来 / `miku` 初音未来 /
  `hack` 赛博 / `atri` 亚托莉 / `nouveau` 新艺术运动——astrbot 上游模板与素材
  （含 ATRI 24MB 字体、Miku 素材）原样搬运、渲染前本地内联，不依赖 GitHub CDN
- **群漫画**：每个话题强制一格的分镜提示词（气泡中文台词 + 底部字幕条）→
  结构化解析 → 多协议生图 → 敏感词三层防护 → 服务端拦截后自动改写重试
- **定时推送**：每群独立时刻（推荐）+ 全局 cron 兜底，支持提前采集
- **指令**：`/群日报` 出海报、`/群日报漫画` 只出漫画、`/群日报整体` 两样都发
  （别名 `群聊日报` / `群聊日报漫画` / `群聊日报整体`）

## 部署

1. 把本目录整体复制到 MaiBot 的 `plugins/` 目录：

   ```
   MaiBot/
   └── plugins/
       └── maibot_plugin_qq_group_daily_analysis/
           ├── _manifest.json
           ├── plugin.py
           ├── requirements.txt
           ├── core/…
           ├── templates/themes/…
           └── tools/…
   ```

2. 重启 MaiBot（或重载插件）。Runner 会按 `requirements.txt` 自动安装
   `httpx / jinja2 / pillow / apscheduler`，并按 `core/config_model.py`
   自动生成 `config.toml` 与 **WebUI 插件配置页**。

3. 在 WebUI 配置页（或 `plugins/maibot_plugin_qq_group_daily_analysis/config.toml`）
   填写群号与时刻，重启生效（改动配置会触发热重载）。

## 配置页速查

全部配置在 WebUI 插件配置页分组呈现，常用的几项：

| 分组 | 配置项 | 说明 |
|---|---|---|
| 基础与定时 | `group_schedules` | **每群独立推送时刻**，如 `[{"group_id": "123456789", "time": "14:30"}]`，多条可并存 |
| 基础与定时 | `group_list` + `push_cron` | 全局 cron（默认 `0 14,22 * * *`）推给群列表，与独立时刻可共存 |
| 基础与定时 | `enable_report` / `enable_comic` | 定时是否出海报 / 是否连带漫画（手动指令不受限） |
| 消息采集 | `window` | 日报覆盖范围：`today` / `24h` / `yesterday` / `hours:N` |
| 消息采集 | `prefetch_minutes` | 定时提前 N 分钟采集（默认 8，0 关闭） |
| 消息采集 | `member_counts` | 宿主取不到群人数时的覆盖表 `{"群号": 人数}` |
| 文字总结模型 | `base_url` + `api_keys` + `model` | **单独指定文字模型**。兼容 OpenAI `/chat/completions` 与 Gemini 两种协议，Key 429 自动轮换 |
| 文字总结模型 | `base_url` 留空 | 回落 **MaiBot 宿主模型**（用 `model` 调 `ctx.llm.generate`） |
| 生图模型（漫画） | `provider` | `doubao`（火山方舟 Seedream，默认）/ `images`（OpenAI）/ `google`（Gemini）/ `chat`（多模态）/ `grok`（x.ai） |
| 生图模型（漫画） | `base_url` + `api_keys` + `model` + `size` | **单独指定生图模型**；`size_mode=upscale` 可对原生封顶的服务本地放大 |
| 输出与提示词 | `mode` / `style` / `width` | 输出方式（image/text/both）、海报主题（6 选 1 下拉）、渲染宽度 |
| 输出与提示词 | `prompt_style` | 提示词语气：`roast` 毒舌（默认）/ `warm` / `neutral` / `custom`（配 `prompt_override`） |
| 敏感内容防护 | `filter` / `lexicon` / `rewrites` | 生图提示词本地词库筛查 + 文字模型脱敏改写次数 |
| 缓存清理 | `enabled` + `cron` + `retention_days` | 定期清理插件数据目录的过期缓存文件 |

### 配置示例（config.toml）

```toml
[basic]
timezone = "Asia/Shanghai"
enable_report = true
enable_comic = true

[[basic.group_schedules]]
group_id = "123456789"
time = "22:00"
enable = true

[llm]
# 直连：填 base_url + api_keys；走宿主模型：三项都留空/留列表空
base_url = "https://generativelanguage.googleapis.com/v1beta"
api_keys = ["AIza..."]
model = "gemini-flash-latest"

[image]
provider = "doubao"
base_url = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
api_keys = ["..."]
model = "doubao-seedream-5-0-flash-260915"
size = "2048x1152"

[report]
mode = "both"
style = "scrapbook"
prompt_style = "roast"
```

## 采集说明（与 nonebot 版的差异）

- MaiBot 插件拿不到 OneBot 的 `get_group_msg_history`，消息来源改为**宿主消息库**：
  优先 `ctx.message`（按时间窗口取），一条没拿到才回落 `ctx.db` 直查 ChatHistory
  （`fetch.source` 可强制指定）。
- 因此**只覆盖机器人在线期间的消息**；回溯不到窗口起点时只记日志告警，不会发到群里。
- 昵称、时间戳、段结构做了多形状归一化（`core/collect.py::_normalize_message`），
  兼容不同版本宿主的字段差异；群成员数取不到时用 `fetch.member_counts` 兜底，
  保证海报上的活跃率有分母。

## 渲染说明（与 nonebot 版的差异）

- nonebot 版用 `nonebot-plugin-htmlrender` 手控 Playwright 页面；MaiBot 插件没有
  页面控制权，渲染钩子改为宿主 `ctx.render.html2png`（`plugin.py` 注入）。
- **本地素材内联**：渲染前把模板里指向主题目录的图片/字体引用统一改写为 base64
  data URI（atri 全量内联后 HTML 约 37MB，渲染稍慢属正常），彻底摆脱对文件基准
  URL 的依赖。Google 字体仍走公网。
- **分段海报**（`report.split_report`）依赖宿主渲染器执行页面脚本：切点固定在
  「高亮记忆碎片」之后、「神人名片颁发」之前；若渲染器不执行 JS（两次截图一致），
  自动退回整图。

## 本地验证（无需 MaiBot 宿主）

```bash
python tools/selfcheck.py    # 主题完整性 / 入参覆盖 / 敏感词回归 / 配置模型 / 工具函数
python tools/smoke_test.py   # 假宿主+假模型+假渲染器跑通全链路，6 主题 HTML 写盘到 tools/smoke_out/
python tools/smoke_test.py atri   # 只渲染单个主题
```

## 其他工具

- `tools/sync_themes.py`：从 astrbot 上游仓库 clone 同步主题模板（整体覆盖
  `templates/themes/`，确认无手工改动后再跑）。
- `tools/build_sensitive_lexicon.py`：从 konsheng/Sensitive-lexicon 修剪重建
  `data/sensitive_lexicon.txt`（词库自备、可选，缺省时本地筛查层静默跳过）。

## 目录结构

```
maibot_plugin_qq_group_daily_analysis/
├── _manifest.json        # MaiBot Manifest v2
├── plugin.py             # 入口：指令 / 定时 / 发送编排 / 宿主适配层
├── requirements.txt      # httpx / jinja2 / pillow / apscheduler
├── core/                 # 移植核心（包内相对导入）
│   ├── config_model.py   # pydantic 配置模型（WebUI 配置页 schema）
│   ├── settings.py       # gdr_* 扁平视图（移植模块按旧字段名读配置）
│   ├── collect.py        # 消息采集与清洗（MaiBot 消息接口/DB 适配）
│   ├── stats.py          # 统计 + 昵称压缩/还原（原样移植）
│   ├── llm.py            # 提示词全量保留 + TextModel（直连 + 宿主回落）
│   ├── comicgen.py       # 5 协议生图客户端（原样移植）
│   ├── themes.py         # 6 主题上下文组装 + 素材内联 + html2png 渲染钩子
│   ├── report.py         # 海报/文字版输出封装
│   ├── pipeline.py       # 主编排：分析 → 敏感筛查 → 海报 → 漫画链路
│   ├── scheduling.py     # cron 注册 / 提前采集前移（原逻辑移植）
│   ├── cleanup.py        # 插件数据目录清理
│   └── sensitive.py      # 本地敏感词筛查（原样移植）
├── templates/themes/     # 6 套主题模板与素材（41MB，原样搬运）
├── tools/                # selfcheck / smoke_test / sync_themes / build_sensitive_lexicon
└── data/                 # （可选）sensitive_lexicon.txt
```

## 已知限制

- 不移植 astrbot 版的：多平台适配、增量分析/断点恢复、WebUI 控制台、模板在线
  安装器、trace 系统（与 NoneBot 版范围一致）。
- 不清理 MaiBot 宿主的消息数据库（插件无权也不应该动宿主库）。
- 宿主消息接口/DB 的返回字段若与归一化假设不符，日报昵称会退化为 QQ 号——
  日志会给出 `昵称退化为 QQ 号` 告警，届时按日志调整
  `core/collect.py::_normalize_message` 的字段路径即可。

## 版权与致谢

- 上游插件与主题模板素材：[SXP-Simon/astrbot_plugin_qq_group_daily_analysis](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis)
- ATRI / Miku 等主题素材版权归原作者所有（各主题目录内 `SOURCE-NOTICE.md` 有署名）。
- 敏感词词源：[konsheng/Sensitive-lexicon](https://github.com/konsheng/Sensitive-lexicon)（MIT License）。

# nonebot_plugin_qq_daily_report

QQ 官方机器人（adapter-qq）群聊日报：群内全量消息实时落库 → LLM 结构化分析 →
scrapbook 主题海报 → sd_media 分片上传 → markdown 卡片发送。

移植自 bison5 的 `nonebot_plugin_qq_group_daily_analysis`（OneBot v11 版），
采集层因官方平台限制重写，分析/渲染层沿用其提示词与 astrbot 模板。

## 与 OneBot 版的关键差异

| | OneBot 版（bison5） | 本插件（bison2） |
| --- | --- | --- |
| 消息来源 | `get_group_msg_history` 历史回溯 | 实时 WS 推送落库（官方无历史接口） |
| 离线时段 | 上线后可回补 | **消息丢失，无法回补**，日报会标注覆盖范围 |
| 昵称 | `sender.card` 原始字段 | openid；发消息时存 `author.username`，出报告时调成员接口补全并缓存 |
| 发图 | OneBot base64 段 | 分片上传（`sd_media`）拿 raw_url → markdown 卡片 |
| 漫画 | 支持 | 未移植 |

## 依赖的前置条件

- **QQ 侧**：群主/管理员给机器人开启「接收所有消息」（全量群消息），
  `.env` 的 `QQ_BOTS` intent 需含 `"group_messages": true`
- **图片**：复用 `extra_plugins/sd_media.py` 的分片上传通道（`SD_UPLOAD_GROUP`
  锚点群），raw_url 有效期 1 天，过期后历史海报会裂
- **渲染**：直连 chromium 容器 CDP（`SD_CDP_HOST`/`SD_CDP_PORT` 环境变量，
  与 `sd_render.py` 同款方式），**不走 htmlrender**（其 0.8.x rasterize 在
  本环境实测 `Page.screenshot` 挂起）；chromium 的 `/start.sh` 已去掉
  `--single-process --no-zygote`（该模式 CDP 建页即崩）；scrapbook 字体走
  Google Fonts 公网，断网会回退系统字体
- **markdown**：自定义 markdown 全量开放（permission.type=2），无需模板资质

## 指令

群里发 **「群日报」**（带不带 @ 都行）即可，海报渲染 + 上传约需 30~60 秒。
上一份还没跑完时再发会提示「任务进行中」。窗口内消息少于
`GDRQ_WINDOW_MIN_MESSAGES` 时提示数据不足。

## 配置（.env，GDRQ_ 前缀）

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `GDRQ_TEXT_BASE_URL` / `GDRQ_TEXT_API_KEYS` / `GDRQ_TEXT_MODEL` | mimo | 文字模型，OpenAI 兼容 / Gemini 双协议自动适配 |
| `GDRQ_WINDOW` | `today` | `today` / `24h` / `yesterday` / `hours:N` |
| `GDRQ_WINDOW_MIN_MESSAGES` | `20` | 低于该数跳过日报 |
| `GDRQ_MAX_MESSAGES` | `4000` | 送模型条数上限（取最近 N 条） |
| `GDRQ_RETENTION_HOURS` | `72` | 落库保留时长，每日 04:30 清理 |
| `GDRQ_CACHE_DIR` | `/app/cache/qq_daily_report` | SQLite 目录 |
| `GDRQ_GROUP_SCHEDULES` | `[]` | 每群独立定时，如 `[{"group_openid":"xxx","time":"21:00"}]`；可选 `"start_date":"YYYY-MM-DD"` 指定生效起始日（之前的触发自动跳过，适合"数据从那天才开始攒"的场景） |
| `GDRQ_SPLIT_REPORT` | `false` | 海报过长时分上下两段发送 |
| `GDRQ_SEND_TEXT` | `false` | 出海报时是否额外发文字版 |
| `GDRQ_PROMPT_STYLE` | `roast` | `roast` / `warm` / `neutral` / `custom` |
| `GDRQ_PROMPT_OVERRIDE` | 空 | 自定义提示词（任何风格下追加） |
| `GDRQ_IMG_SIZE` | `#600px #600px` | markdown 图片尺寸参数（客户端会压缩略图，点击看原图） |

改完 `.env` 需重启容器生效。

## 结构

```
__init__.py   指令 + 定时注册 + 发送（裸 HTTP 绕 adapter timestamp 坑）
config.py     GDRQ_ 配置
archive.py    ChatMessage / ChatArchive / resolve_window
store.py      SQLite 落库/查询/成员昵称缓存/群信息缓存/72h 清理
ingest.py     GROUP_MESSAGE_CREATE 实时监听入库
stats.py      纯统计 + 昵称代号压缩（移植）
llm.py        TextModel 双协议 + 日报分析提示词（移植，砍漫画）
render.py     scrapbook 渲染（整图/分段）+ 纯文字降级（移植）
templates/    astrbot scrapbook 主题模板与素材
```

## 已知限制

- 机器人离线期间的消息丢失；日报开头会标注实际覆盖起点
- 仅分析文字：图片/文件/无正文消息不入库
- 群成员数接口（群信息）与成员昵称接口有频控，报告补查昵称上限 30 人/次
- 定时推送是**主动消息**（无 msg_id），消耗机器人主动消息配额
- raw_url 1 天有效，历史海报不可回看

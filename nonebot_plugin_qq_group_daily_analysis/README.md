# 群日报 · nonebot-plugin-group-daily-report

抓群聊历史 → 大模型结构化分析 → 渲染成长图海报推送到群里。仅适配 **OneBot v11**。

配置全部走 bison 的 `.env`（`gdr_` 前缀），**没有 HTTP 配置页**。
文字总结模型与生图模型**完全独立**，各自配 base_url / key / model。

> **本次重新适配（对齐上游 astrbot 版 v5.8.4）**
>
> 保留：采集（NapCat 风控规避、时间驱动翻页）、昵称代号压缩、海报一图分二。
> 其余按上游最新版重新适配，重点变化：
>
> - **漫画链路整体重写**：分镜提示词改为「每话题一格 + 气泡中文台词 +
>   底部字幕条 + Background Context」的结构化 `scene` 输出，多层解析 +
>   失败重试；文字模型输出先剥离 `<think>` 思考块（详见「生图模型」章节）。
> - **生图客户端多协议化**（`gdr_image_provider`）：doubao / images /
>   google / chat / grok；修复旧版 url 形式响应在已关闭的 httpx client
>   上下载必失败的 bug；图片字节做签名校验，拒绝把错误页当图片发群里。
> - **Gemini 文字模型修复**：system 走 `systemInstruction`（旧版连续两个
>   user 轮次会被 400 拒绝，默认配置完全不可用）。
> - **死配置激活/移除**：`gdr_max_tokens`、`gdr_enable_quotes`、
>   `gdr_enable_quality`、`gdr_enable_report` 现在真实生效；
>   `gdr_text_thinking` 移除（其 `extra_body` 实现是无效的 SDK 约定字段）。
> - **落库清理修复**：VACUUM 改走 AUTOCOMMIT 连接（旧版在事务内必报错）；
>   不再删除共享 uninfo 表的 Session/User 孤儿行。
> - **敏感内容防护（生图链路）**：分镜要求脱敏 + 输出自查，可选外置词库
>   （`sensitive.py`，词库文件自备、缺失即静默跳过）二次筛查 + 文字模型
>   事后改写兜底——豆包 `400 InputTextSensitiveContentDetected` 的
>   词源就是旧版要求「完整详情翻译为英文」进了 Background Context，
>   现已改为中性化改写，详见「敏感内容防护（生图链路）」章节。

---

## 指令

| 指令 | 别名 | 产出 | 说明 |
| :--- | :--- | :--- | :--- |
| `群日报` | `群聊日报` | 海报 | 只渲染海报，不调生图 |
| `群日报漫画` | `群聊日报漫画` | 漫画 | 只出漫画，省掉一次海报截图 |
| `群日报整体` | `群聊日报整体` | 海报 + 漫画 | 两样都发 |

> bison 的 `COMMAND_START=["/", ""]` 含空前缀，**直接发「群日报」即可触发，不必带斜杠**。
> 没有注册「日报」别名——指令按「句首最长前缀」匹配，会被日常聊天误触发。
> 三条指令必须各自 `on_command`，**不能互相写成别名**：别名在同一条匹配里竞争，
> 「群日报」比「群日报整体」短，会先把整个前缀吃掉。

### 三条指令共用同一次文字模型分析

漫画的绘图提示词是从日报那份 `analysis` 的 `summary` 和 `topics` 派生的，所以：

- **「群日报整体」不会为了多出那张图再调一次文字模型**——采集、分析、统计各只跑一遍，
  海报和漫画吃的是同一个 `analysis` 对象。
- 这和 astrbot 原插件的做法一致：日报流程触发的漫画直接复用日报分析结果里的
  `topics`，只有独立漫画指令才会单独跑一次轻量的 `analyze_topics` 话题提取。
  本插件为了少一条代码路径没有拆出轻量路径，独立漫画指令走的也是完整分析。

**省掉的只有海报渲染**：`comic_only=True` 会跳过 HTML 截图，但采集和文字模型
分析一步都省不掉。

### 同群防重入

三条指令都受**每群一把锁**保护。上一条还没跑完时再发，会回
「本群已有日报任务在进行中，请稍候…」而不是再跑一遍——生图 API 是按次计费的，
连刷指令会重复扣钱。

漫画生成另有全局并发上限（`_COMIC_SEM = 2`），避免多个群同时打爆生图服务。
## 快速开始

最少只要这四行，其余全走默认值：

```env
gdr_text_api_keys=["sk-xxx"]
gdr_text_base_url="https://xxx/v1"
gdr_text_model="your-text-model"
gdr_image_api_keys=["ark-xxx"]
```

配好定时自动推送：

```env
gdr_group_schedules=[{"group_id": 965148132, "time": "21:00"}]
```

> 改完 `.env` 需**重启 nonebot** 才会重新注册定时任务。

---

## 定时推送

### 每群独立时刻（推荐）

```env
gdr_group_schedules=[
  {"group_id": 965148132, "time": "14:30"},
  {"group_id": 123456789, "time": "22:00", "enable": false}
]
```

每个群注册**独立的 cron 任务**，互不影响。`enable: false` 可临时停用而不删配置。
时刻是 **24 小时制**，按 `gdr_timezone` 解释：`9:05` 自动补成 `09:05`；
`25:00` / `14:99` / `1430` / `14:30:00` 会在启动时报错而不是静默跑偏。

### 全局定时（兼容旧配置）

```env
gdr_group_list=[965148132]
gdr_push_cron="0 14,22 * * *"
```

`gdr_group_schedules` 非空时**优先**用它，这两个不再生效。两者也能共存。

### 时区

```env
gdr_timezone="Asia/Shanghai"
```

服务器在 UTC 时**必须**设，否则定时偏 8 小时。填 `Local` 用服务器本地时区。
每个 job 带 `misfire_grace_time=1800`——机器人 14:30:20 才起来仍会补发。

---

## 海报主题

```env
gdr_report_style="atri"
```

**这 6 套主题是直接复用 astrbot 原插件的模板和素材，不是照着样子另写的。**
`templates/themes/` 下就是上游 `templates/` 的原文件，`themes.py` 只负责把本
插件的数据翻译成上游模板认识的入参。

| 值 | 上游目录 | 素材 |
| :--- | :--- | :--- |
| `scrapbook` | `scrapbook` | 仅字体（走 Google Fonts CDN） |
| `retro` | `retro_futurism` | 仅字体 |
| `miku` | `HatsuneMiku` | 本地 6 个立绘 PNG + 字体 + lucide CDN |
| `hack` | `hack` | 仅字体 |
| `atri` | `ATRI` | 本地 34MB：立绘、表情、背景 + 3 个 LXGW 字体 |
| `nouveau` | `art_nouveau` | 仅字体 |

上游 `templates/` 里还有 `simple`、`spring_festival`、`BlueArchive` 和 `format`
四个目录，但**不在其 README 的主题清单里**（`format` 本身只是个无配色的基础
模板），所以没有搬。

### 素材为什么能本地读

上游是把素材挂在 GitHub CDN 上，渲染时去网络拉。本插件把 40MB 素材全部落到
`templates/themes/*/`，靠的是 `template_to_pic` 的执行顺序：

```python
await page.goto("file://<模板目录>")   # 先建立基准 URL
await page.set_content(html, ...)     # 再塞 HTML
```

页面 URL 停在模板目录上，所以模板里的 `./file/xxx.webp` 就能解析到磁盘。
（顺序反过来、或改用 data: 页面，图片会全部裂开。）

字体仍走公网（Google Fonts / jsDelivr），本地没有副本——**渲染环境需要能
访问外网**，否则会回退到系统字体。中文回退顺序见「中文字体」一节。

### 素材更新

模板和素材是从上游仓库同步的，`tools/sync_themes.py` 负责搬运，
`tools/selfcheck.py` 负责静态自检（主题表 ↔ 磁盘目录 ↔ 模板入参 ↔ 数据映射）。
脚本本身不含上游仓库，跑之前先 clone 一份：

```bash
git clone --depth 1 https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis.git
python tools/sync_themes.py    # 会整体覆盖 templates/themes/
python tools/selfcheck.py      # 不生图，只做静态检查
```

路径写死在 `sync_themes.py` 顶部的常量里，迁移时改一下即可。

## 提示词语气

```env
gdr_prompt_style="roast"
```

`roast` 毒舌锐评（默认）/ `warm` 温和 / `neutral` 中性 / `custom` 完全自定义。

```env
gdr_prompt_override="用粤语写，多用网络梗"
```

`custom` 时作为主要求；其余模式下追加在默认要求之后。

### 锐评的结构

不是打一个 0-100 总分，而是划 3-6 个**抽象维度**（对齐 astrbot 的 `QualityReview`）：

- 维度名 2-6 个字且**高度抽象**（如「技术方案研究」「情感树洞」）
- **禁止**在维度名里出现具体人名、项目名、事件名
- 具体事件、梗、吐槽全部塞进该维度的 `comment`
- 各维度百分比归一化到 100%，模型没给就平均分配

---

## 生图模型（漫画）

漫画由独立的生图模型产出，和文字总结模型**完全分开配置**，互不影响——
不想出漫画就把 `gdr_image_api_keys` 清空，插件会提示「未配置 gdr_image_api_keys，
漫画已跳过」而不会报错。

生图**按次计费且偏慢**，所以有两道保护：同群防重入（上一条没跑完时直接拒绝）
和全局并发上限 2。标题为空的话题会在调生图之前被滤掉，不白花钱。

### 漫画链路（按上游 astrbot 版 v5.8.4 重新适配）

旧版的漫画提示词是「让文字模型自由发挥一段英文四格描述」，拿到什么发什么，
既不校验也不过滤思考内容，是漫画内容完全跑偏的根源。现在按上游移植：

1. **每个话题强制一格**——分析出的 N 个话题对应 Panel 1..N，不许模型自行裁减；
2. **气泡中文台词 + 底部字幕条**——台词 ≤15 字、字幕条 ≤30 字，显式指定
   渲染中文原文，画面与群聊内容一眼可对上；
3. **话题详情脱敏改写后作为 Background Context** 附在每格提示词里（帮生图
   模型理解剧情，同时严禁渲染成正文长文）——改写规则要求把现实人名/组织/
   事件中性化、虚构化，是敏感内容防护的第一道（详见下文）；
4. **结构化输出**——文字模型必须返回 `{"scene": "..."}`，插件做多层解析
   （JSON 对象 → 正则 → 数组整合），失败自动重试一次，不会把模型闲聊
   原样丢给生图 API；
5. 文字模型输出先**剥离 `<think>` 思考块**（推理模型经中转时常见污染），
   Gemini 响应跳过 `thought: true` 的思考 parts。

诊断口令：日志里搜 `漫画分镜 scene`，能看到最终发给生图 API 的提示词前 300 字。

### 敏感内容防护（生图链路）

豆包 Seedream 对提示词里的敏感内容会直接 `400 InputTextSensitiveContentDetected`：
图没出、配额照样扣。做 3 组对照实验可以确认：触发点不是提示词结构或长度，
而是提示词里的**具体内容词**（群聊里的涉政表述被原样搬进生图提示词）。
因此防护是三层，全部只作用于生图链路，日报正文/海报不受影响：

1. **提示词规则层**：分镜规范要求文字模型把话题详情**脱敏改写**后再作为
   Background Context（中性指代、虚构化），并在输出 `scene` 前自查
   敏感词/谐音/拼音/拆字变体；
2. **本地词库层**（`sensitive.py`，默认开）：对最终 `scene` 做二次筛查——
   NFKC 归一化 + 去分隔符（字符间插标点/空格的**拆写**照常命中）+ 谐音/
   拆字**变体** + ASCII leet 折叠（大小写/数字代写）。
   本层**不随插件分发词库**：启用时自行准备词库文件（UTF-8 文本，行格式
   `词条<TAB>类别`，`#` 为注释行），放到 `data/sensitive_lexicon.txt`，
   或用 `gdr_sensitive_lexicon` 配置附加词库路径（逗号分隔多个）。
   **词库是可选项**：文件不存在或为空时本层静默跳过，不影响其余防护与
   出图；具体词条只存在于词库文件里，代码与日志均不收录；
3. **事后层**（上游 astrbot 同款兜底）：即使生图服务仍以内容审查原因拒绝，
   也会把**被拒绝的原提示词 + 拒绝原因原文**打包给文字模型重新生成提示词，
   再试最后一次。

筛查命中的处置顺序：文字模型脱敏改写（`gdr_sensitive_rewrites` 次，默认 1，
改完本地复查）→ 仍命中则降级为**剥离 Background Context 的精简分镜**重出一轮
→ 仍不行则跳过漫画，群里明示「本次漫画内容涉敏，已被生图服务拦截，已跳过」，
而不是笼统的「漫画生成失败」。

```env
gdr_sensitive_filter=true                  # 本地筛查总开关（默认开）
gdr_sensitive_lexicon=""                   # 附加词库路径，逗号分隔
gdr_sensitive_rewrites=1                   # 命中后交给文字模型改写的次数
```

自查命令：`python tools/selfcheck.py`（配置了词库文件时）会附带跑词库
完整性与匹配回归（去分隔符 / 谐音变体 / 大小写 leet / 日常文本不误伤）。
想临时关掉只看改写效果，把 `gdr_sensitive_filter` 设为 `false` 即可。

### 生图协议（gdr_image_provider）

| 值 | 说明 |
| :--- | :--- |
| `doubao` | 火山方舟 Seedream（默认） |
| `images` | OpenAI 兼容 `/images/generations`（gpt-image-1 等） |
| `google` | Gemini `generateContent` 原生接口（nano-banana 等，跳过思考图片） |
| `chat` | OpenAI 兼容 `/chat/completions` 多模态生图 |
| `grok` | x.ai `/images/generations` |

### 火山方舟 Seedream（默认）

```env
gdr_image_api_keys=["ark-xxx"]
# base_url / model / size 都有默认值，可省略
```

默认值：`https://ark.cn-beijing.volces.com/api/v3/images/generations`、
`doubao-seedream-5-0-flash-260915`、`2048x1152`（16:9 横版，适合漫画条）。

`base_url` 写裸域名、写到 `/v1` 或写完整的 `.../images/generations` **都认**，
不会被重复拼接。

`gdr_image_optimize_prompt` 控制 Seedream 的服务端提示词改写：留空走服务端
默认；`off` 显式关闭——**漫画内容跑偏时优先试这个**，让分镜提示词原样生效；
`fast` / `standard` 显式选择优化档位。

### size 的两种写法（互斥，别混用）

**① 档位**——推荐，默认 `2K`，可选 `1K` / `1.5K` / `2K`。
`1.5K` 与 `1K` 同价但效果更好。

| 档位 | 1:1 | 16:9 | 9:16 | 21:9 |
| :--- | :--- | :--- | :--- | :--- |
| 1K | 1024x1024 | 1424x800 | 800x1424 | 1568x672 |
| 1.5K | 1536x1536 | 2048x1152 | 1152x2048 | 2352x1008 |
| 2K | 2048x2048 | 2816x1584 | 1584x2816 | 3136x1344 |

**② 宽x高**——须**同时**满足两个约束：

- 总像素 ∈ `[921600, 4624220]`（即 `1280x720` ~ `2048x2048x1.1025`）
- 宽高比 ∈ `[1/16, 16]`

```env
gdr_image_size=2048x1152   # 有效：16:9 横版，2359296 像素
gdr_image_size=512x512     # 无效：262144 像素，低于下限
```

### 换 OpenAI

OpenAI 原生封顶 1536/1792，**没有 2048x2048**，必须开 upscale：

```env
gdr_image_provider="images"
gdr_image_base_url="https://api.openai.com/v1"
gdr_image_model="gpt-image-1"
gdr_image_size=2048x2048
gdr_image_size_mode=upscale
gdr_image_quality=high
```

`upscale` 先按 `gdr_image_native_sizes` 里**宽高比最接近**的规格请求，再用 Pillow
放大到目标尺寸。按宽高比匹配是关键——只按面积匹配的话 `2048x2048` 会落到
`1792x1024`（横版 1.75:1），再拉成正方形就是严重变形。

### 换 Gemini（google 协议）

```env
gdr_image_provider="google"
gdr_image_base_url="https://generativelanguage.googleapis.com/v1beta"
gdr_image_model="gemini-2.5-flash-image"
gdr_image_aspect_ratio="16:9"
gdr_image_size="2K"        # 自动映射到 imageConfig 的 1K/2K/4K
```

### quality 与分辨率无关

`gdr_image_quality` 是 **OpenAI 独有**的质量档位（`low`/`medium`/`high`/`auto`），
只影响细节、耗时和价格，**跟出图尺寸没关系**。Seedream 等不认这个字段，
留空即不发送（默认值就是空）。

### 其他出图参数

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `gdr_image_response_format` | `b64_json` | `b64_json` 直接给字节；`url` 给链接（插件会额外下载一次，多一个失败点） |
| `gdr_image_output_format` | 空 | `png` / `jpeg`，空则由服务商决定 |
| `gdr_image_watermark` | 未设置 | 是否加水印；未设置则不发送该字段 |
| `gdr_image_extra` | `{}` | 追加原始请求字段，如 `{"stream": false}`，会覆盖同名字段 |
| `gdr_image_retries` | `1` | 生图失败后的重试次数（网络/内容异常均计） |
| `gdr_image_retry_delay` | `2.0` | 生图重试间隔秒数 |
| `gdr_image_aspect_ratio` | `16:9` | 宽高比（google / chat 协议使用） |
| `gdr_image_timeout` | `300` | 超时（秒） |
| `gdr_image_proxy` | 空 | 形如 `http://ip:port` |

---

## 消息采集

```env
gdr_source="auto"
gdr_window="24h"
```

- `gdr_source`：`chatrecorder` 读落库（零 API 调用）/ `api` 调历史接口 /
  `auto` 优先读库、库空时回落 API
- `gdr_window`：`today` 今天 0 点至今 / `24h` 最近 24 小时 / `yesterday` 昨日全天 /
  `hours:N` 最近 N 小时

**按时间回溯，不是按条数。** 实测 NapCat 基本不截断 `count`：单次 5000 条可回溯
**72 小时**，一次拉满就够覆盖 24 小时日报。

```env
gdr_fetch_page_size=5000
gdr_fetch_max_pages=1
gdr_fetch_interval=5.0
```

`gdr_fetch_max_pages` 保持 1：NapCat 的 `message_seq` 游标返回的是**该点之后**的
消息而非之前，往回翻页拿不到更早的数据；而且连续快速调用会触发限流，
**反而拿到更少**。`gdr_fetch_interval` 是翻页间隔，5 秒是安全值。

> 走 `chatrecorder` 可彻底绕开上述风控问题，代价是全群消息落库、且只能记录
> 机器人在线期间的消息。bison5 已装该插件但未启用。

---

## 只分析文字

**图片和合并转发不参与分析**，这是硬规则，不是「恰好没处理到」。

| 消息类型 | 处理方式 |
| :--- | :--- |
| 纯文字 | ✅ 正常分析 |
| 文字 + 图片 | ✅ 只分析文字，图片不识别、不描述 |
| 纯图片 | ❌ 整条剔除，不计入消息数 |
| 合并转发（`forward`） | ❌ 整段丢弃，内容不展开 |
| `json` / structmsg | ❌ 同上（内部同样承载聊天记录） |
| 纯表情 / 纯文件 / 纯 at | ❌ 无正文，不计入统计 |

合并转发的内容有三种投递方式，全部拦得住：

1. 独立的 `forward` 段（NapCat 的实际形态，`data.content` 是**字符串化的节点列表**）
2. `json` 段 / `[CQ:json,...]`（QQ 的 `com.tencent.structmsg`）
3. **被客户端塞进 `text` 段**——这种最阴险，不拦的话节点列表会直接变成日报正文里的一坨 JSON

被剔除的消息数会打在日志里，也会写进海报底部：

```
[群日报] 群 965148132 按「只分析文字」剔除：纯图片 736 条、纯合并转发 40 条
```

> 该群实测 2994 条原始消息里，约 30% 是纯图片或纯转发。
> 日报显示的消息数是**剔除后**的条数，与原始条数对不上是正常的。

---

## 输出

```env
gdr_send_text=false
gdr_report_width=900
```

`gdr_send_text` 默认关闭——海报里已包含全部内容，再发一条文字版是重复刷屏。
**告警信息不受此开关影响，始终会发。**

海报用 `send_group_msg` 直接发图，**不走合并转发**——NapCat 无法在
`send_group_forward_msg` 的节点里发送 base64 图片。

### 指令的群内消息序列

指令执行期间**不发「正在采集…请稍候」**。生图加渲染要几十秒，中途插话只会盖在
群消息流里像刷屏。实际序列是：

```
用户发「群日报」  →（静默 30~60s）→  [海报图片]
                                  →  群日报已发送，请查收
```

「群日报已发送，请查收」在**手动指令**后发，**定时推送不发**——21:00 定时群发
一条确认语纯属噪音。

只有两种情况会提前回话：上一条任务没跑完（「本群已有日报任务在进行中，请稍候…」）
和纯漫画指令缺生图 key（「未配置 gdr_image_api_keys，无法生成漫画。」）。

### 缺生图模型时的提示分寸

漫画出不来有两条路径，**故意做得不一样**：

| 场景 | 群里提示 |
| :--- | :--- |
| 定时推送 | **不提示**，只写日志 |
| `群日报`（只出海报） | **不提示**（本来就没要漫画） |
| `群日报整体` | ⚠️ 漫画已跳过 |
| `群日报漫画` | 直接回「无法生成漫画」并中止 |

定时那个点没人在看，「你少配了一个 key」对群友毫无意义；而用户主动敲了漫画指令
却什么都没收到，就必须告诉他原因。实现上是 `generate_daily_report` 的
`notify_missing_comic` 开关，只有手动漫画指令才置位。

---

## 全部配置项

### 采集

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `gdr_source` | `auto` | 消息来源 |
| `gdr_window` | `today` | 覆盖范围 |
| `gdr_history_lens` | `20000` | 条数安全上限，真正的终止条件是回溯够时间窗口 |
| `gdr_fetch_page_size` | `5000` | 单次请求条数 |
| `gdr_fetch_max_pages` | `1` | 最大翻页数 |
| `gdr_fetch_interval` | `5.0` | 翻页间隔（秒） |
| `gdr_member_cache_ttl` | `21600` | 成员数缓存（秒） |
| `gdr_max_messages` | `4000` | 送模型条数上限 |
| `gdr_window_min_messages` | `20` | 少于该数视为数据不足（如凌晨运行）直接提示 |

### 文字总结模型

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `gdr_text_base_url` | Gemini 地址 | 兼容 OpenAI / Gemini 两种协议 |
| `gdr_text_api_keys` | `[]` | 多个 key 遇 429 自动轮换 |
| `gdr_text_model` | `gemini-flash-latest` | 模型名 |
| `gdr_text_proxy` | 空 | 代理 |
| `gdr_text_timeout` | `300` | 超时（秒） |
| `gdr_max_tokens` | `0` | 输出上限，`0`=不发送该字段由服务端决定（推荐；推理类中转把思考 token 计入该额度，设小会把长 JSON 截断） |
| `gdr_max_topics` | `12` | 最多话题数 |
| `gdr_max_quotes` | `8` | 最多金句数 |
| `gdr_enable_quotes` | `true` | 是否提取金句（关闭后提示词里不再要求该字段） |
| `gdr_max_titles` | `5` | 最多起几个称号 |
| `gdr_enable_quality` | `true` | 是否评估群聊锐评（关闭后提示词里不再要求该字段） |

### 生图模型

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `gdr_image_provider` | `doubao` | 协议：`doubao` / `images` / `google` / `chat` / `grok` |
| `gdr_image_base_url` | 火山方舟地址 | 写裸域名、`/v1` 或完整路径都认 |
| `gdr_image_api_keys` | `[]` | 留空则不生成漫画 |
| `gdr_image_model` | `doubao-seedream-5-0-flash-260915` | 模型名 |
| `gdr_image_size` | `2048x1152` | 档位 `1K`/`1.5K`/`2K` 或 `宽x高` |
| `gdr_image_size_mode` | `native` | `native` 原样透传 / `upscale` 降级请求后本地放大 |
| `gdr_image_native_sizes` | OpenAI 那组 | upscale 模式可选的原生规格，逗号分隔 |
| `gdr_image_quality` | 空 | OpenAI 独有档位，与分辨率无关 |
| `gdr_image_response_format` | `b64_json` | `url` / `b64_json` / 空 |
| `gdr_image_output_format` | 空 | `png` / `jpeg` / 空 |
| `gdr_image_watermark` | 未设置 | 是否加水印 |
| `gdr_image_extra` | `{}` | 追加原始请求字段 |
| `gdr_image_proxy` | 空 | 代理 |
| `gdr_image_timeout` | `300` | 超时（秒） |
| `gdr_image_retries` | `1` | 生图失败重试次数（服务端敏感拦截不重试） |
| `gdr_sensitive_filter` | `true` | 生图提示词本地敏感词筛查总开关 |
| `gdr_sensitive_lexicon` | 空 | 附加敏感词库路径，逗号分隔（行格式 `词条<TAB>类别`） |
| `gdr_sensitive_rewrites` | `1` | scene 命中后交给文字模型脱敏改写的次数 |
| `gdr_comic_prompt` | 空 | 漫画风格提示词，空则用内置默认 |

### 输出与渲染

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `gdr_send_text` | `false` | 是否额外发文字版 |
| `gdr_report_width` | `900` | 海报渲染宽度（像素） |
| `gdr_report_style` | `scrapbook` | 6 套主题之一，见「海报主题」 |
| `gdr_report_mode` | `both` | `image` / `text` / `both` |
| `gdr_enable_text_summary` | `true` | 是否生成纯文字总结 |
| `gdr_split_report` | `false` | 海报过长时分两段发送，切点在「高亮记忆碎片」之后、「神人名片颁发」之前；话题为空时自动退回整图 |
| `gdr_prompt_style` | `roast` | 4 种语气之一 |
| `gdr_prompt_override` | 空 | 自定义提示词 |

### 定时

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `gdr_timezone` | `Asia/Shanghai` | 定时时区，`Local` 用服务器本地 |
| `gdr_group_schedules` | `[]` | 每群独立时刻 |
| `gdr_group_list` | `[]` | 全局定时群列表 |
| `gdr_push_cron` | `0 14,22 * * *` | 全局 cron |
| `gdr_enable_report` | `true` | 定时是否生成日报图 |
| `gdr_enable_comic` | `true` | 定时推送是否顺带出漫画（只管定时，手动指令不受影响） |

### chatrecorder 落库清理

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `gdr_cleanup_enabled` | `false` | 默认关：机器人若不在凌晨启动，cron 可能永不触发 |
| `gdr_cleanup_cron` | `30 4 * * *` | 清理 cron |
| `gdr_retention_days` | `7` | 保留天数，`0` 不按时间清理 |
| `gdr_db_max_mb` | `100` | 体积上限（MB），`0` 不按体积清理 |
| `gdr_cleanup_batch` | `5000` | 每批删除条数，避免长事务锁库 |

---

## 依赖

- `nonebot-adapter-onebot`（v11）
- `nonebot-plugin-apscheduler`（bison 已有）
- `nonebot-plugin-htmlrender` + Chromium（bison 已有）
- `httpx`
- `pillow`（可选，`gdr_image_size_mode=upscale` 时需要）

### 中文字体

海报由容器内的 Chromium 渲染，字体必须**在容器里真实存在**。
镜像默认只有 `WenQuanYi Zen Hei`（无 Bold），所有 600/700 都是浏览器合成的伪粗体，
中文会发糊。装上真 Bold 即可：

```bash
docker exec bison5-nonebot-1 apt-get update
docker exec bison5-nonebot-1 apt-get install -y fonts-noto-cjk
```

> ⚠️ `apt-get` 装的东西**不随容器重建保留**。要长期生效请写进
> `docker-compose.yml` 的 volume 或派生镜像，否则 `docker compose up --force-recreate`
> 之后字体会退回伪粗体。

`Noto Sans CJK SC` / `Source Han Sans SC` 均为 SIL OFL 1.1，可商用，有真 Bold。

---

## 结构

```
nonebot_plugin_qq_group_daily_analysis/
├── __init__.py     插件入口：三条指令、定时注册、元数据
├── config.py       配置定义
├── collect.py      消息采集与清洗（时间窗口回溯 + 只分析文字 + 昵称剥离）
├── stats.py        纯统计：活跃度、时段分布、贡献榜、昵称压缩
├── llm.py          文字总结模型客户端（Gemini / OpenAI 双协议）+ 分镜/改写提示词
├── comicgen.py     生图模型客户端（多协议 + Key 轮换 + 敏感拦截分类）
├── sensitive.py    生图链路本地敏感词筛查（归一化 + 去分隔符 + 谐音/leet 变体；
│                   词库文件可选、自备，放 data/sensitive_lexicon.txt 即启用）
├── themes.py       海报渲染：翻译入参 + 复用 astrbot 原主题模板
├── report.py       渲染入口 + 纯文字降级 + 数据清洗
├── pipeline.py     主流程编排
├── templates/
│   └── themes/     astrbot 原主题模板与素材（6 套，约 40MB）
├── tools/
│   ├── sync_themes.py  从上游仓库同步模板与素材
│   └── selfcheck.py    静态自检，不生图（含敏感词筛查回归）
└── README.md
```

## 已知限制

- **只支持 OneBot v11**，不兼容 v12
- 采集依赖 NapCat 的历史消息接口，历史深度受服务端保留策略限制
- 海报高度随内容增长，超长群聊可能触发 QQ 的图片尺寸上限
- 主题模板里的 Google Fonts 走公网，**渲染环境断网会回退到系统字体**（版式会变）
- 海报里的「人格标签」恒为空：上游该字段来自其人格系统，按约定未迁移
- 头像一律不取：上游支持 QQ 头像，本插件不拉取，模板对空值有保护
- **在 Windows 上直接跑会让所有时间偏 8 小时**：`.env` 里的
  `TZ=Asia/Shanghai` 是 IANA 名，Windows 的 CPython 解析不了（它只认
  `CST-8` 这类 POSIX 写法），解析失败就退回 UTC。容器是 Debian、有
  zoneinfo，不受影响。**只在本机调试时注意，别据此判断插件有问题。**
- `gdr_send_text=false` 时告警和错误提示仍会照发，这是有意为之
- 中文热词由文字模型产出；本地词频只作兜底且仅保留 ASCII 词——无词典的中文
  n-gram 切分无法可靠区分「服务器」与「务器部」

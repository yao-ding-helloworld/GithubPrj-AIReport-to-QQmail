# 技术设计：GitHub AI 开源热点雷达 v1

## 环境约束（本机实测）

| 实测项 | 结果 | 设计应对 |
|---|---|---|
| Esafenet 透明加密 | Bash 启动脚本被破坏、跨扩展名 rename 报 EXDEV | **零第三方依赖**（纯标准库）；执行统一走受信进程 |
| Python 内 `unlink()` | 被安全删除钩子拦截并报 EXDEV（同源问题） | 运行时**不删除任何文件**；清理一律走外部命令。日志/临时产物只覆盖不删除 |
| gh CLI | 未安装，无既有认证 | 认证走 `.env` 的 `GITHUB_TOKEN`，可选 |
| 报告形态约束 | 用户硬性要求 | 单文件 HTML、内联、无外部依赖、响应式 |
| 模块命名 | 不能用 `http.py` | 与标准库 `http` 包同名会遮蔽它，导致 `urllib` 内 `import http.client` 失败 → 改名 `net.py` |

## 目标 / 非目标

**目标**
1. 周期采集可回溯：快照留存本地 SQLite，历史越久报告越准
2. 双榜：热度（star 总量）+ 趋势（窗口净增量）
3. 三时间窗：7 / 30 / 91 天
4. 领域分桶 + 页面交互筛选（时间窗 × 榜单类型 × 领域 × 关键字）

**非目标（v1 明确不做）**
- 不做后端服务，不做数据库服务器
- 不做 star 刷量识别（仅展示，人工判断）
- 不引入 LangGraph / LLM 调用（v1 是确定性管道；LLM 点评留 v2 接口）
- 不抓取非官方数据源（trending 页面、第三方统计站）

## 架构与数据流

```
run.py [--period weekly|monthly|quarterly|all] [--top 10] [--out dist/report.html]
       [--no-collect] [--no-render] [--notify] [--reclassify] [--max-queries N]
  │
  ├─ collect.py   领域配置 → search 查询（含新晋）→ 候选池（去重/过滤/截断）→ 当日快照 upsert
  │               + 趋势冷启动回溯（stargazers 尾页，仅 token 模式）
  ├─ classify.py  topics/关键词规则 → 领域标签；先走 purge_blocked 清历史违规项
  ├─ compute.py   快照差值（主）+ backfill（兜底）→ windows 表 + 双榜排序
  ├─ render.py    榜单 + 元数据 → 单文件 HTML（数据 JSON 内联，三维切换零请求）
  └─ notify.py    digest.py 摘要 → EmailProvider(SMTP) / ConsoleProvider(兜底)

persist.py  CSV ↔ SQLite（云端持久化载体，配合 .github/workflows/daily-radar.yml）
```

数据流关键点：**collect 每次运行对候选池全量记快照**；compute 取"窗口内最早快照"与"当前快照"求差（允许间隔不均匀）；render 把三个 period 的榜单全部算好内嵌，页面切换零请求。

## 关键决策（ADR）

### ADR-1 数据源：GitHub Search API，不用 trending 页面
`GET /search/repositories?q=...&sort=stars&per_page=100` 一次返回最多 100 个仓库，**响应自带 stargazers_count**——所以"快照采集"不需要逐仓库再调 API，一个领域 2~3 个查询即可完成。trending 页面无官方 API、结构随时变，放弃。

### ADR-2 趋势计算：快照差值为准，尾页回溯做冷启动
- **快照差值**（ground truth）：窗口末快照 star − 窗口初快照 star。准确、零额外 API，但需要两次运行才能产生。
- **尾页回溯**（冷启动增强）：`GET /repos/{o}/{r}/stargazers?per_page=100` + `Accept: application/vnd.github.star+json`，按 `ceil(stargazers_count/100)` 直接跳到最后一页，从后向前翻，直到某页最早 `starred_at` 早于窗口起点。每仓库 1~3 个请求。**仅 token 模式启用**（匿名 60 次/小时撑不住）。
- 已知限制：GitHub 对超大仓库（>40k star）的 stargazers 分页受限 → 回溯值标记 `estimated=true`，报告页加"估算"角标。
- 优先级：同一仓库同时有 snapshot 与 backfill 值时，**取 snapshot**。

### ADR-3 存储：SQLite 三表
```sql
repos(repo_id INTEGER PK, full_name TEXT UNIQUE, description TEXT,
      language TEXT, topics TEXT,          -- JSON 数组
      domain TEXT, first_seen DATE)

snapshots(repo_id INTEGER, snap_date DATE, stars INTEGER,
          PRIMARY KEY(repo_id, snap_date))

windows(period TEXT, repo_id INTEGER, stars_delta INTEGER,
        source TEXT,           -- 'snapshot' | 'backfill'
        estimated INTEGER,     -- 0/1
        PRIMARY KEY(period, repo_id))
```
单文件、零部署、SQL 聚合天然适配榜单计算；同日快照 upsert 保证采集幂等。

### ADR-4 认证：.env 可选，匿名可降级
- `GITHUB_TOKEN` 存在 → `Authorization: Bearer` 头，5000 req/h，启用尾页回溯
- 缺失 → 匿名 60 req/h，跳过回溯；趋势榜退化为"自下次采集起生效"，页面明示
- 符合既有约定：.env 自动加载，缺失时主动提示补充，不硬编码

### ADR-5 领域分类：规则引擎，配置即扩展
- 每个领域 = `search_queries[]`（发现渠道）+ `topic_rules[]` + `keyword_rules[]`（归类规则）
- 归类优先级：topics 精确命中 > 名称/描述关键词 > other 兜底
- 多领域同时命中：取规则序首个为主领域，其余记 secondary（不参与榜单）
- 新增领域 = 在 config 里加一段 dict，不改代码；LLM 分类留 v2（domain 字段可被后处理覆盖）

### ADR-6 报告：单文件 HTML，零依赖
- 数据以 `<script>const DATA = {...}</script>` 内联（三个 period 全量预算好）
- 交互：原生 JS——时间窗 tab、榜单类型切换、领域多选、关键字过滤
- 图表：手写 SVG（水平条形榜单 + sparkline 折线），不用图表库
- 响应式：CSS grid + 媒体查询，窄屏单列
- sparkline 需 ≥3 个快照，不足则整体隐藏该组件

### ADR-7 形态：确定性管道，不是对话式 Agent
v1 的"Agent" = 命令行管道（collect → classify → compute → render），全链路可预测、可测试、可断点重跑。LLM 点评（render 前插入 summarize 步骤）与 LangGraph 状态图编排留 v2，接口已预留。

### ADR-8 快报推送：SMTP 邮件单通道

- **通道选型：SMTP 邮件（v1 唯一通道）**——只用 `smtplib` + `email.message` 标准库，零第三方依赖
- **为什么不用 IM / 连接器**（三条否决理由，按否决强度排序）：
  1. **连接器够不着**：WorkBuddy 连接器（`wecom` / `qq-mail` / `feishu` / `dingtalk`）只能在 WorkBuddy 运行环境内被调用，**GitHub Actions 无法访问**；且本机实测接入 QQ邮箱连接器直接失败（`replace-target (EXDEV)`——Esafenet 拦截跨扩展名 rename）。连接器路线需放弃云端无人值守，且在本机装不上
  2. **IM 装不下报告**：企业微信群机器人 markdown 正文上限 **4096 字节**，三领域双榜摘要必然溢出，只能截断成"标题 + 链接"，正文全丢
  3. **邮件四项全中**：标准协议 ✅ + 零依赖 ✅ + 能承载完整 `report.html` 附件 ✅ + 云端 Actions 可直接调用 ✅——其余通道最多中三项
- **provider 抽象保留**：`notify.py` 定义统一接口 `send(subject, markdown, html_path=None)`，v1 只落地 `EmailProvider` 与 `ConsoleProvider`（未配置 SMTP 时兜底）。**接口保留但不做多通道广播**——若 v2 需要加 IM 通道，只增实现、不改调用方
- 密钥：`SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `MAIL_TO`，本地读 `.env`、云端读 GitHub Secrets，**变量名保持一致**以复用同一份代码
- 邮件配置要点（以 QQ 邮箱为例）：设置 → 账号 → 开启 IMAP/SMTP 服务 → 生成 **16 位授权码**（不是登录密码，可随时吊销）；服务器 `smtp.qq.com`，SSL 端口 `465`（或 STARTTLS `587`）
- 内容形态：正文 = Markdown 摘要（每领域「趋势 Top 3 + 热度 Top 3」+ 数据覆盖说明）；附件 = `report.html`（完整交互报告）。**不把 HTML 内联进正文**——邮件客户端会剥离内联 JS，三维筛选交互会失效
- 失败策略：推送失败不影响整体运行成功——报告与快照已落盘，重试 2 次后仅记录错误
- 降级路径：SMTP 变量缺失 → 转 `ConsoleProvider` 打印并提示配置方式，流程不中断
- 已知取舍：报告以**附件**形式抵达，手机端需下载后用浏览器打开才能交互；v2 可选「发布公网链接 + 邮件正文带链接」补齐"点开即看"体验

### ADR-9 调度：GitHub Actions cron（云端）

- workflow：`.github/workflows/daily-radar.yml`，`schedule` + `workflow_dispatch`（手动补跑）
- **cron 使用 UTC**：欲北京时间 09:00 触发 ⇒ 表达式 `0 1 * * *`
- **快照持久化采用「SQLite 运行时 + CSV 提交载体」双格式**：每次运行从仓库检出 CSV 重建库，结束后导出 CSV 提交。
  理由：SQLite 是二进制，每日全量提交会使仓库体积线性膨胀至 GB 级；CSV 为文本，git 增量压缩友好（年增量约数 MB），且可读、可 diff、可人工修正
- 权限：需 `permissions: contents: write` 才能提交快照
- 自洽点：GitHub 会在仓库 60 天无活动后自动暂停定时工作流；本流程每日提交快照，天然保持活跃
- 产物：报告 HTML 作为 workflow artifact 保留；摘要经 ADR-8 推送

### ADR-10 内容卫生：描述截断 + 非技术内容黑名单

首轮全量采集暴露了两个真实世界的坑，都必须在**入库前**处理：

- **超长描述**：实测有仓库描述长达 **64773 字符**（关键词堆砌）。后果有三：报告体积从 46KB 膨胀到 447KB、关键词匹配被长文本"轰炸"造成误分类、存储被污染。
  → 对策：`config.clean_text()` 折叠空白并截断到 `DESC_MAX = 300`；分类时只在截断后的文本上匹配关键词。
- **非技术内容**：同一批数据里出现了政治宣传类仓库，与技术榜单主题无关，且不宜出现在产物中。
  → 对策：`config.BLOCKED_TERMS` 黑名单，命中即不进候选池；**并加一道兜底**——`db.purge_blocked()` 在每次分类前清理库内历史命中项（连带删除其 snapshots / windows），保证报告永不含此类内容。
- 黑名单是**可配置的策略**而非硬编码逻辑：需要时在 `config.py` 增删词条即可，不触碰管道代码。

## 领域配置（v1 预置）

| 领域 | 中文 | 发现查询（示意） | 归类规则（示意） |
|---|---|---|---|
| agent-frameworks | 智能体构造 | `topic:ai-agent stars:>800 sort:stars`；`agent framework in:name,description stars:>500`；新晋 `topic:ai-agent created:>{created_after} stars:>300` | topics: langchain, langgraph, crewai, autogen, agno, ai-agent-framework... |
| agent-optimization | 智能体优化 | `topic:prompt-engineering stars:>500`；`dspy OR "prompt optimization" stars:>300`；`topic:agent-memory stars:>300` | topics: dspy, prompt-engineering, llm-evaluation, agent-memory... |
| llm-theory | 大模型理论 | `topic:llm topic:training stars:>500`；`transformer implementation stars:>400`；`topic:quantization stars:>300`；`topic:mixture-of-experts stars:>300` | topics: transformer, attention, llm-training, fine-tuning, quantization, cuda... |
| （可选）rag / tooling-ecosystem / multimodal-eval | 检索增强 / 工具协议生态 / 多模态与评测 | 配置已留位，开关控制 | — |

查询阈值与规则均在 `config.py` 集中维护，调优不动核心代码。
**注意 `created:>{created_after}` 占位符**：GitHub 的 `created:` 只接受绝对日期，写成 `created:>180d` 会直接 422，采集时把占位符替换成 `今天 − NEWCOMER_DAYS`。

## 风险与对策

| 风险 | 对策 |
|---|---|
| search 限速（匿名 10/min、token 30/min） | 查询间强制间隔 + 指数退避，429 按 Retry-After 等待 |
| 新仓库 star 总量低，热度榜看不见 | 每领域附加"新晋查询"（created + stars 阈值），趋势榜天然覆盖新贵 |
| 关键词误分类 | 规则集中在 config；描述截断后再匹配（ADR-10）；报告展示 other 桶，便于人工纠偏后加入规则 |
| 超长描述撑大产物 / 污染匹配 | `DESC_MAX` 截断（ADR-10） |
| 非技术内容混入 | 黑名单过滤 + 分类前 `purge_blocked` 兜底清理（ADR-10） |
| 新晋查询 422（相对日期） | 查询模板占位符在采集时替换为绝对日期 |
| 单个查询失败 | 4xx 跳过该查询并记日志，其余查询照常，流程不中断 |
| 窗口内快照不足两次 | 回溯值兜底（token 模式）；两者皆无则不出现在趋势榜 |
| 一次运行中断 | 采集幂等（同日 upsert）；compute/render 只读不写，可独立重跑 |
| 超大仓库回溯不完整 | estimated 标记 + 页脚口径说明 |

## 决策记录（2026-10-14 已确认）

| 分叉点 | 决策 | 影响 |
|---|---|---|
| API 认证 | **使用 GitHub Token，填入 `.env`** | 启用 ADR-2 的尾页回溯；首次运行即产出完整趋势榜。Token 为只读、零 scope、可设 90 天有效期 |
| 图表实现 | **手写 SVG + 原生 JS**（ADR-6） | 不使用任何图表库，单文件产物控制在 300KB 以内，离线可完整交互 |
| 领域范围 | **三大核心领域**：agent-frameworks / agent-optimization / llm-theory | 每领域约 3 个查询，单次运行 API 消耗约 150~450 次（含回溯），token 额度下约 1~2 分钟跑完 |
| 快报渠道 | **SMTP 邮件单通道**（QQ 邮箱，`smtplib` 标准库；正文 = 摘要，附件 = `report.html`） | 标准协议 + 零依赖 + 能承载完整报告 + 云端可调用，四项全中；IM 有 4096 字节上限、连接器在 Actions 中不可用且本机装不上 |
| 调度方式 | **GitHub Actions cron（云端）** | 新增 workflow；快照改「SQLite 运行时 + CSV 提交载体」双格式；密钥走 GitHub Secrets，变量名与本地 `.env` 一致 |

扩展路径：新增领域只需在 `config.py` 的 `DOMAINS` 中追加一段（查询组 + 归类规则），不触碰采集/指标/渲染代码。RAG、工具与协议生态、多模态与评测已预留在可选列表。

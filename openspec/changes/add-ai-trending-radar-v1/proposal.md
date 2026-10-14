# 变更提案：GitHub AI 开源热点雷达（v1）

## Why（为什么做）

手动刷 GitHub Trending 有三个硬伤：只看得到当天快照、不能按 AI 子领域筛选、更不能回答"过去一周/一个月/一个季度谁涨得最快"。本项目做一个周期运行的管道：按周/月/季三个窗口、按领域分桶，用「热度榜（star 总量）+ 趋势榜（star 增速）」两个视角整理 AI 开源项目，产出一份可交互的单文件 HTML 报告。

## What Changes（变更内容）

| 模块 | 职责 |
|---|---|
| collect（采集） | 按"领域 → search 查询组"配置调用 GitHub Search API 构建候选池；search 结果自带 stargazers_count，直接落当日快照 |
| store（存储） | SQLite 单文件三表：repos / snapshots / windows |
| classify（分类） | 规则引擎：repo topics 精确命中 → 名称/描述关键词 → 兜底 other |
| compute（指标） | 热度 = 当前 star；趋势 = 窗口净增量（快照差值为准，stargazers 尾页回溯做冷启动增强） |
| render（报告） | 单文件 HTML：内联 JSON + 原生 JS + 手写 SVG 图表，无外部依赖，响应式 |
| notify（快报） | 榜单 → Markdown 摘要 → SMTP 邮件推送（正文 = 摘要，附件 = 完整 `report.html`；provider 接口保留，未配置时降级控制台，失败不影响主流程） |
| schedule（调度） | GitHub Actions cron 每日运行；快照以 CSV 提交回仓库实现云端持久化 |

## 核心口径

- **时间窗口**：weekly = 7 天，monthly = 30 天，quarterly = 91 天
- **热度榜**：领域内按当前 star 总量降序 Top N —— 回答"谁是大牌"
- **趋势榜**：领域内按窗口 star 净增量降序 Top N —— 回答"谁在爆发"
- **领域**（v1 预置，配置可扩展）：agent-frameworks（智能体构造）/ agent-optimization（智能体优化）/ llm-theory（大模型理论），可选扩展 rag / tooling-ecosystem / multimodal-eval

## Impact（影响面）

- 全新独立项目 `github-trending-agent/`，不影响既有代码
- **纯 Python 标准库**（urllib / sqlite3 / json），零第三方依赖——规避本机 Esafenet 加密环境对 pip 的干扰
- 密钥走 `.env`（`GITHUB_TOKEN`），自动加载，缺失时降级匿名模式并主动提示
- GitHub API 用量预估：匿名 ≤ 10 请求/次运行；带 token ≤ 300 请求/次运行（含趋势回溯）
- 本机已验证：未安装 gh CLI、无既有 GitHub 认证 → token 由用户后续自行补充

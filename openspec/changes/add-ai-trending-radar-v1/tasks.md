# 任务清单：GitHub AI 开源热点雷达 v1

> 依据 `design.md` 的 ADR 与 `specs/` 下的行为规格。
> 原则：每个任务都可独立验证，避免"全写完才知道对不对"。
> 标记：`[x]` 已完成并实测；`[ ]` 待做；`[~]` 已实现但受外部条件限制尚未实测。

## 1. 骨架

- [x] 1.1 项目结构与 `config.py`：三领域查询组、归类规则、时间窗口（7/30/91 天）、star 阈值、TopN
- [x] 1.2 `env.py`：零依赖 `.env` 解析；缺失 `GITHUB_TOKEN` 时打印提示并降级匿名模式（不中断）
- [x] 1.3 `db.py`：SQLite 建表 `repos` / `snapshots` / `windows`（+ `meta`）；幂等 upsert（`(repo_id, snap_date)` 主键）
- [x] 1.4 `net.py`：urllib 封装——User-Agent、超时、429/403 退避重试、读取 `X-RateLimit-Remaining`
      （**不叫 `http.py`**：与标准库 `http` 包同名会遮蔽它，导致 `urllib` 内部 `import http.client` 失败）

**验收**：`python -c "import db; db.init()"` 成功建库；`net` 能取回一次 search 结果
→ 验收脚本 `tools/smoke_stage1.py`，四项全 PASS（建表幂等 / `/rate_limit` / 真实 search）

## 2. 采集 collect

- [x] 2.1 查询生成器：领域配置 → search 查询（主查询 + 新晋查询），`{created_after}` 占位符替换为绝对日期
- [x] 2.2 搜索执行与候选池合并去重（按 `repo_id`），单查询失败只跳过不中断
- [x] 2.3 快照 upsert（同日覆盖，重跑不产生重复）
- [~] 2.4 stargazers 尾页回溯：页码 = `ceil(stars/100)`，从末页向前翻至越出窗口起点；`estimated` 标记超限仓库
      （代码就绪，仅 token 模式启用；本地为匿名模式，尚未实测）

**验收**：数据库出现 ≥60 个仓库与当日快照 → 实测 **930 个仓库**；回溯待 token 验证

## 3. 分类 classify

- [x] 3.1 三级判定：topics 精确命中 → 名称/描述关键词 → `other` 兜底
- [x] 3.2 多领域同时命中：取规则序首个为主领域，其余记 `secondary`（不参与榜单）
- [x] 3.3 支持 `--reclassify` 对已有数据重算标签；分类前先 `purge_blocked` 清历史违规项

**验收**：抽查分类合理；`other` 占比 < 20% → 实测 **other 11 / 930 ≈ 1.2%**，分布 574 / 218 / 127

## 4. 指标 compute

- [x] 4.1 窗口快照对查找：取窗口内最早可用快照，容错不均匀间隔
- [x] 4.2 趋势值优先取 `snapshot`，缺失时回退 `backfill`（靠主键覆盖天然实现）
- [x] 4.3 双榜计算：热度榜（star 总量）/ 趋势榜（净增量），每领域 Top N
- [x] 4.4 增速百分比（展示用）与"数据覆盖 N 天"标注

**验收**：手工核对增量与 GitHub 页面一致 → 用临时库灌合成历史快照验证：
三个窗口各写入 930 条增量，趋势 Top3 排序与增长曲线一致，sparkline 数据点齐备

## 5. 报告 render

- [x] 5.1 HTML 骨架：内联 `DATA`、响应式 grid、浅色主题
- [x] 5.2 顶部控制条：时间窗 tab × 榜单类型切换 × 领域筛选 × 关键字搜索
- [x] 5.3 榜单渲染：排名、项目名（外链）、描述、star 总量、窗口增量、语言
- [x] 5.4 手写 SVG：榜单条形 meter + sparkline（快照数 ≥3 才渲染）
- [x] 5.5 页脚 + 顶部提示条：采集时间、数据覆盖天数、`estimated` 口径说明、免责声明

**验收**：断网打开报告，三个维度切换交互正常，窄屏自动单列 → 已生成 `dist/report.html`（46.6 KB）

## 6. 串联与验收

- [x] 6.1 `run.py` CLI：`--period` / `--top` / `--out` / `--db` / `--no-collect` / `--no-render` / `--notify` / `--reclassify` / `--max-queries`
- [x] 6.2 端到端冒烟：真实采集（15 查询 / 930 仓库）→ 生成报告 → 浏览器验收
- [x] 6.3 逐条对照 `specs/` 的行为场景验证（含首轮暴露的三个真实问题，见 design.md ADR-10 与风险表）
- [x] 6.4 `.env.example` 与 `.gitignore`（排除 `.env`、`*.db`、报告产物）

## 7. 定时快报 delivery

- [x] 7.1 `notify.py`：provider 抽象（`send(subject, markdown, html_path=None)`）+ `EmailProvider`（SMTP）+ `ConsoleProvider`（未配置时兜底）
- [x] 7.2 `digest.py`：榜单 → Markdown 摘要（每领域趋势 3 + 热度 3 + 数据覆盖说明）+ 极简 Markdown→HTML 转换
- [x] 7.3 `persist.py`：快照导入导出 CSV ↔ SQLite（`import` / `export` / `status` 三个子命令）
- [x] 7.4 `.github/workflows/daily-radar.yml`：cron（UTC 01:00）+ workflow_dispatch + Secrets + 提交快照 + 上传 artifact
- [x] 7.5 `run.py` 增加 `--notify` 开关，支持"只推送不重采"（`--no-collect --notify`）
- [ ] 7.6 端到端验证：把仓库推到 GitHub，手动触发 workflow，确认**邮箱收到摘要与附件**、仓库出现快照提交
      （**需要用户提供**：一个 GitHub 仓库 + Secrets）
- [x] 7.7 `EmailProvider` 细节：`EmailMessage` + `smtplib.SMTP_SSL:465`，纯文本 + HTML 双份正文、`report.html` 作 MIME 附件，`MAIL_TO` 支持逗号分隔多收件人；`SMTPAuthenticationError` 提示"需用 16 位授权码"
      （未配置 SMTP 时已实测降级到 ConsoleProvider 并输出完整摘要）

**验收**：Actions 页面手动触发一次成功；邮箱收到含三领域双榜的摘要 + `report.html` 附件；仓库新增一次快照提交
→ 前两项待 7.6；本地已验证脚本链路 + CSV 往返（repos / snapshots / windows 各 930 / 930 / 0 行）

## 8. 待用户补充的外部条件

- [ ] GitHub PAT（Fine-grained，只勾 Public Repositories read-only），写入 `.env` 的 `GITHUB_TOKEN`
- [ ] QQ 邮箱授权码 + 收件地址，写入 `.env` 的 `SMTP_PASS` / `MAIL_TO`
- [ ] 一个 GitHub 仓库（承载 workflow 与快照 CSV）

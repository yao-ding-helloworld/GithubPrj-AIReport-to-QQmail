# 定时快报（Scheduled Delivery）增量规格

## ADDED Requirement: 摘要生成

系统 SHALL 从榜单结果生成 Markdown 摘要，内容包含统计窗口、生成时间、各领域的趋势 Top 3 与热度 Top 3，以及数据覆盖与估算说明。

### Scenario: 正常摘要
- **GIVEN** 三个领域均有两榜数据
- **WHEN** 生成摘要
- **THEN** 摘要含 3 个领域 ×（趋势 3 条 + 热度 3 条），每条含项目名、star 总量、窗口增量

### Scenario: 无趋势数据
- **GIVEN** 某领域当前无可用趋势数据
- **WHEN** 生成摘要
- **THEN** 该领域标注"趋势数据待生成"，不省略该领域

### Scenario: 长度约束
- **WHEN** 摘要渲染后超过邮件正文的合理长度（每领域条目展开 ≥ 8 行）
- **THEN** 按领域截断至每条目一行，并追加"完整榜单见附件报告"提示，不丢弃任何领域

## ADDED Requirement: 邮件推送（SMTP 单通道）

系统 SHALL 通过 Python 标准库 SMTP 将摘要与完整 HTML 报告送达指定邮箱，不引入第三方依赖；推送失败不得导致整体运行失败。

### Scenario: 摘要 + 报告附件送达
- **GIVEN** 配置了 `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` / `MAIL_TO`，且已生成 `report.html`
- **WHEN** 执行推送
- **THEN** 以 `smtplib.SMTP_SSL`（端口 465）登录并发送 `EmailMessage`：正文为**移动优先的 HTML 邮件**（单列、内联样式、零 JS、零外部资源）＋ 纯文本分支，`report.html` 作为 MIME 附件（`add_attachment`，`maintype='text'`、`subtype='html'`），主体编码 UTF-8，主题形如 `[AI 开源雷达] 周榜 · 2026-10-14`

### Scenario: 手机端不点附件也能看全内容
- **GIVEN** 用户在 iOS 邮件客户端打开快报，且不点开附件
- **WHEN** 渲染邮件正文
- **THEN** 正文自带每领域「趋势 Top N + 热度 Top N」的完整列表（项目名可点、star、窗口增量、增速、一句话简介），单列排版不做横向溢出；不得依赖 JavaScript、flex/grid、position:sticky 或任何外部资源（iOS 预览 HTML 附件**不执行 JS**，靠 JS 渲染的页面在手机上就是空壳）

### Scenario: 摘要只覆盖一个窗口
- **GIVEN** 报告内含周/月/季三个窗口
- **WHEN** 生成邮件摘要
- **THEN** 正文只呈现一个窗口（默认周榜）的 Top N，并在页脚说明"完整榜单见附件"，避免正文过长

## ADDED Requirement: 项目简介

系统 SHALL 为出现在榜单上的仓库生成一句话简介，并同时呈现在邮件正文与报告中。

### Scenario: 规则版简介（默认，零依赖）
- **GIVEN** 未配置 `LLM_API_KEY`
- **WHEN** 生成简介
- **THEN** 用「主要语言 · 所属领域方向。＋ 描述首句（英文原文）」拼出一句中文骨架，截断至 `SUMMARY_MAX_CHARS`，不调用任何外部接口

### Scenario: LLM 版简介（配置后自动启用）
- **GIVEN** 配置了 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`
- **WHEN** 生成简介
- **THEN** 调用 OpenAI 兼容的 `/chat/completions` 把项目信息压成一句不超过 40 字的中文简介

### Scenario: 简介缓存（控制花费）
- **GIVEN** 某仓库的描述/主题/语言均未变化
- **WHEN** 再次运行
- **THEN** 命中内容指纹缓存，不重复调用 LLM；缓存字段随快照 CSV 一起持久化，云端重建库后依然命中

### Scenario: 简介只覆盖会展示的仓库
- **GIVEN** 候选池有 900+ 个仓库，但报告只展示各领域 Top N
- **WHEN** 生成简介
- **THEN** 只对榜单实际出现的仓库生成（约数十条），不为整个候选池生成

### Scenario: 写入自校验
- **WHEN** 写入简介
- **THEN** 记录**实际生效行数**并与待写条数比对，若待写 >0 而生效 =0 则显式报错，不得静默成功

### Scenario: 只发摘要，不内联报告
- **WHEN** 构造邮件
- **THEN** 正文不内联 `report.html` 的内容（邮件客户端会剥离内联 JS，交互筛选会失效），完整报告只以附件形式提供

### Scenario: 报告文件缺失时不阻断
- **GIVEN** 本次运行未生成 `report.html`（例如 `--no-render`）
- **WHEN** 执行邮件推送
- **THEN** 仅发送摘要正文，跳过附件，不报错

### Scenario: 多个收件人
- **GIVEN** `MAIL_TO` 为逗号分隔的多个地址
- **WHEN** 执行推送
- **THEN** 全部地址进入 To 头（去空格、忽略空项）

### Scenario: provider 降级
- **GIVEN** 缺少任一必需的 SMTP 变量
- **WHEN** 管道运行到推送环节
- **THEN** 降级为 ConsoleProvider（把摘要打印到标准输出），流程正常结束并提示所需变量名，不中断

### Scenario: 认证失败
- **GIVEN** `SMTP_PASS` 填的是登录密码而非授权码
- **WHEN** 登录
- **THEN** 捕获 `SMTPAuthenticationError`，日志提示"QQ 邮箱需使用 16 位授权码（在邮箱设置中开启 IMAP/SMTP 服务后生成）"，流程仍以成功退出

### Scenario: 推送失败不阻塞
- **WHEN** SMTP 返回非成功状态或连接超时
- **THEN** 重试 2 次（指数退避），仍失败则记录错误，但**整体退出码仍为成功**（报告与快照已落盘）

### Scenario: 密钥不泄露
- **WHEN** 输出日志与产物
- **THEN** 任何位置都不出现密钥明文（仅在存在性检查时输出"已配置 / 未配置"）

## ADDED Requirement: 云端定时调度

系统 SHALL 提供 GitHub Actions workflow，实现每日定时运行与手动补跑，并将快照持久化回仓库。

### Scenario: 定时触发
- **GIVEN** workflow 配置 `cron: '0 1 * * *'`（UTC，对应北京时间 09:00）
- **WHEN** 到达触发时间
- **THEN** 执行完整管道（导入快照 → 采集 → 指标 → 渲染 → 摘要 → SMTP 邮件推送）

### Scenario: 手动触发
- **WHEN** 在 Actions 页面点击 Run workflow
- **THEN** 立即执行一次完整流程

### Scenario: 快照持久化
- **GIVEN** 仓库中已存在快照 CSV
- **WHEN** workflow 运行
- **THEN** 先检出并导入 CSV，运行后导出新 CSV 并提交，提交信息包含日期与新增记录数

### Scenario: 无历史快照的首次运行
- **GIVEN** 仓库中不存在快照 CSV
- **WHEN** 首次运行
- **THEN** 正常采集并生成首份 CSV，趋势榜按既有降级规则处理，不报错

### Scenario: 报告留存
- **WHEN** 流程结束
- **THEN** HTML 报告作为 workflow artifact 上传，保留期 ≥ 30 天

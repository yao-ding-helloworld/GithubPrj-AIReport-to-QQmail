# 数据采集（Data Collection）增量规格

## ADDED Requirement: 候选池发现

系统 SHALL 按领域配置（一组 search 查询）调用 GitHub Search API，构建去重后的候选池，并记录当日 star 快照。

### Scenario: 首次采集
- **GIVEN** 空数据库，已配置 3 个领域共约 9 个查询
- **WHEN** 执行采集
- **THEN** 每个领域命中仓库数 ≥ 20，全部写入 `repos` 与 `snapshots`（当日快照）

### Scenario: 采集幂等
- **GIVEN** 数据库已有仓库 R 与当日快照
- **WHEN** 同日再次执行采集
- **THEN** 不产生重复仓库行，当日快照被更新为最新值

### Scenario: 限速退避
- **WHEN** API 返回 403/429 且 `X-RateLimit-Remaining` 为 0
- **THEN** 依据 `Retry-After`（缺省 60 秒）等待后重试，单查询最多重试 3 次；仍失败则跳过该查询并记录日志，不中断整体流程

### Scenario: 新晋项目发现
- **GIVEN** 某领域配置了"新晋查询"（`topic:X created:>{created_after} stars:>Y`）
- **WHEN** 执行采集
- **THEN** `{created_after}` 被替换为 `今天 − NEWCOMER_DAYS` 的绝对日期（如 `2026-04-17`），近 N 天创建但 star 已达阈值的新项目进入候选池，即使总 star 低于热度榜门槛

### Scenario: 相对日期查询会被平台拒绝
- **GIVEN** 查询被误写成 `created:>180d`
- **WHEN** 调用 Search API
- **THEN** GitHub 返回 **422 Validation Failed**（`created:` 只接受绝对日期）
- **AND** 该查询被跳过并记入日志，其余查询照常执行，流程不中断

### Scenario: 查询失败不阻断整体
- **GIVEN** 15 个查询中有 3 个返回 4xx（不可重试错误）
- **WHEN** 采集结束
- **THEN** 候选池由剩余 12 个查询的结果构成，日志输出"跳过的查询：3"，流程继续走分类/指标/报告

### Scenario: 超长描述截断
- **GIVEN** 某仓库的描述长达 64773 字符（真实案例）
- **WHEN** 写入 `repos`
- **THEN** 描述被折叠空白并截断到 `DESC_MAX`（300 字符），避免撑大报告体积、污染关键词匹配

### Scenario: 非技术内容过滤
- **GIVEN** 仓库名称或描述命中内容黑名单（政治宣传等非技术内容）
- **WHEN** 筛选候选池
- **THEN** 该仓库不写入 `repos`，不出现在任何榜单与报告中

## ADDED Requirement: 趋势冷启动回溯

系统 SHALL 在配置了 `GITHUB_TOKEN` 时，对候选池仓库调用 stargazers API（`Accept: application/vnd.github.star+json`），从末页向前回溯统计窗口内 star 增量。

### Scenario: 正常回溯
- **GIVEN** 仓库 R 有 5,300 star，窗口起点为 7 天前，token 已配置
- **WHEN** 执行回溯
- **THEN** 从第 53 页起向前翻页，直到某页最小 `starred_at` 早于窗口起点；统计值写入 `windows(stars_delta, source='backfill')`

### Scenario: 超大仓库分页受限
- **GIVEN** 仓库 R 的 star 数超过平台分页上限（约 40,000）
- **WHEN** 回溯无法覆盖完整窗口
- **THEN** 保留已得数值并标记 `estimated=1`，不报错

### Scenario: 匿名模式跳过
- **GIVEN** 未配置 `GITHUB_TOKEN`
- **THEN** 跳过回溯，`windows` 无 `backfill` 记录，报告页显示"趋势自下次采集起精确"

### Scenario: 额度保护
- **GIVEN** 剩余额度不足（`X-RateLimit-Remaining` < 预留阈值）
- **WHEN** 准备继续回溯
- **THEN** 中止回溯并保留已采集数据，日志提示补充额度或稍后重跑

# 指标计算（Metrics）增量规格

## ADDED Requirement: 双榜计算

系统 SHALL 对每个 period（`weekly`=7 天 / `monthly`=30 天 / `quarterly`=91 天）产出每个领域的热度榜与趋势榜。

### Scenario: 热度榜
- **GIVEN** period = monthly，某领域有 45 个仓库
- **WHEN** 执行指标计算
- **THEN** 热度榜 = 该领域按当前 star 降序的 Top N（默认 10）

### Scenario: 趋势榜（快照差值）
- **GIVEN** 仓库 R 在窗口内有 ≥2 个快照，窗口起点快照 1,200、当前快照 1,560
- **WHEN** 执行指标计算
- **THEN** 窗口增量 = 360，`source='snapshot'`

### Scenario: 趋势榜（回溯兜底）
- **GIVEN** 仓库 R 只有 1 个快照，但存在 `backfill` 值 280
- **WHEN** 执行指标计算
- **THEN** 窗口增量 = 280，`source='backfill'`

### Scenario: 优先级
- **GIVEN** 仓库 R 同时存在 `snapshot` 与 `backfill` 两种窗口值
- **WHEN** 执行指标计算
- **THEN** 采用 `snapshot` 值（`backfill` 仅用于补齐缺失）

### Scenario: 数据覆盖不足
- **GIVEN** 请求窗口为 91 天，但最早快照距今仅 40 天
- **WHEN** 执行指标计算
- **THEN** 以最早可得快照为起点计算，并在结果中标注 `coverage_days=40`

### Scenario: 无趋势数据
- **GIVEN** 仓库 R 既无多快照也无 `backfill` 值
- **WHEN** 执行指标计算
- **THEN** R 不出现在趋势榜（仍可出现在热度榜），不报错

## ADDED Requirement: 榜单排序稳定性

系统 SHALL 保证相同输入产生相同排序（并列时按 `repo_id` 升序），避免多次运行结果抖动。

### Scenario: 并列处理
- **GIVEN** 两个仓库窗口增量均为 120
- **WHEN** 排序
- **THEN** 按 `repo_id` 升序排列，结果可复现

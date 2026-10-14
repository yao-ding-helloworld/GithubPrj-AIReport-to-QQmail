# 报告生成（Report Generation）增量规格

## ADDED Requirement: 单文件交互报告

系统 SHALL 生成自包含的单文件 HTML：不引用任何外部资源，支持时间窗、榜单类型、领域三类筛选与关键字搜索，并响应式适配。

### Scenario: 零依赖离线可用
- **WHEN** 在无网络环境下用浏览器打开报告
- **THEN** 页面完整渲染，全部交互（切换、筛选、搜索）可用，无任何加载失败

### Scenario: 三维筛选联动
- **WHEN** 用户选择 period=weekly、榜单=趋势、领域=agent-frameworks
- **THEN** 仅显示该领域该窗口的趋势 Top N，含 star 增量与外链

### Scenario: 关键字搜索
- **WHEN** 用户在搜索框输入 `lang`
- **THEN** 实时过滤出名称或描述包含该串的项目（跨领域，与当前筛选叠加）

### Scenario: 回溯标记透出
- **WHEN** 某条目趋势值 `source='backfill'`
- **THEN** 该行显示"估算"角标，页脚说明计算口径

### Scenario: 数据覆盖提示
- **GIVEN** 计算结果标注 `coverage_days < 请求窗口`
- **THEN** 页面顶部显示"本窗口数据实际覆盖 N 天"

### Scenario: 空趋势榜
- **GIVEN** 某领域当前无可用趋势数据（首次运行且无 token）
- **THEN** 该领域趋势榜显示空状态提示"趋势数据将在下次采集后生成"，不报错

## ADDED Requirement: 图表实现

系统 SHALL 以手写 SVG 实现榜单条形与 sparkline，不引入任何图表库。

### Scenario: 榜单条形
- **WHEN** 渲染榜单
- **THEN** 每行按数值比例绘制水平条形，最大值占满可用宽度

### Scenario: sparkline 降级
- **GIVEN** 某仓库的快照数少于 3
- **THEN** 不渲染该行的 sparkline 占位，不影响其他行

### Scenario: 浅色主题与可读性
- **WHEN** 在任意浏览器打开
- **THEN** 使用浅色背景 + 深色文字，文本对比度可读，无暗色面板

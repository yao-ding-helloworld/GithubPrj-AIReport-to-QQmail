# 领域分类（Domain Classification）增量规格

## ADDED Requirement: 规则分类

系统 SHALL 依据仓库的 `topics` 与名称/描述关键词，将其归入唯一主领域。

### Scenario: topics 命中
- **GIVEN** 仓库 topics 含 `langgraph`，且 `agent-frameworks` 的 topic 规则含 `langgraph`
- **WHEN** 执行分类
- **THEN** 该仓库归入 `agent-frameworks`

### Scenario: 关键词兜底
- **GIVEN** topics 未命中任何领域规则，但描述含 `multi-agent framework`
- **WHEN** 执行分类
- **THEN** 归入 `agent-frameworks`（关键词命中）

### Scenario: 无命中进入 other
- **GIVEN** 仓库仅由"新晋查询"发现且无任何规则命中
- **WHEN** 执行分类
- **THEN** 归入 `other`，在报告中单独成组展示

### Scenario: 多领域命中
- **GIVEN** 仓库同时命中两个领域的规则
- **WHEN** 执行分类
- **THEN** 按"先 topics 后关键词、再按规则声明序"取首个为主领域，其余写入 `secondary_domains`，不参与榜单计算

### Scenario: 超长描述不造成误分类
- **GIVEN** 某仓库描述极长（6 万字符以上）并堆砌大量技术关键词
- **WHEN** 执行分类
- **THEN** 关键词只在**截断后**（`DESC_MAX`）的描述上匹配，避免被长文本"关键词轰炸"拉到不相关领域

### Scenario: 黑名单项在重算时被清除
- **GIVEN** 早期版本已把命中内容黑名单的仓库写进了库
- **WHEN** 执行分类（含 `--reclassify`）
- **THEN** 该仓库连同其 `snapshots` / `windows` 记录一并删除，日志输出"内容过滤：清除 N 个命中黑名单的仓库"

## ADDED Requirement: 配置即扩展

系统 SHALL 允许通过仅修改配置文件新增或调整领域，无需改动采集、指标、渲染代码。

### Scenario: 新增领域
- **GIVEN** 在 `config.py` 的 `DOMAINS` 中追加一个领域（查询组 + 归类规则）
- **WHEN** 重新执行采集与渲染
- **THEN** 新领域出现在候选池、榜单与报告筛选器中，无需修改其他模块

### Scenario: 重算历史标签
- **GIVEN** 调整了归类规则
- **WHEN** 执行 `--reclassify`
- **THEN** 已有仓库按新规则重新打标，不重新采集

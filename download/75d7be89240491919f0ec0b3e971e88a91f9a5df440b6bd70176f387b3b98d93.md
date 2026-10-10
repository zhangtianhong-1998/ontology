# 路线3：开发顺序与完成标准

本文中的代码、配置路径及命令均以仓库的 `route3/` 目录为基准；文档链接可直接点击。

前置条件：路线2的通用流程及适用提取器已验证，并保存实际运行产物；不要求真实输入具备任何示例业务模式。先保持同一输入、本体和证据，验证路线3本身的作用。

| 任务 | 关联 US | 最小实现 | 完成标准 |
|---|---|---|---|
| R3-T01 | R3-US01 | 本体版本锁、父运行引用和映射数据模型 | 目标类型哈希在运行中不变；新关系进入 ontology_gap |
| R3-T02 | R3-US02、R3-US07 | MappingRule 验证/编译、显式引用与字面值执行 | selector false 不生成；unknown 未决；缺字段不删条件 |
| R3-T03 | R3-US02、R3-US04 | 公式/维度规则复用、规则重叠检查、留出执行 | 正确展开每个记录自己的引用；不能将示例 M17 应用到所有行 |
| R3-T04 | R3-US03、R3-US04 | canonical 映射、带条件断言去重和冲突表 | C08/C09/C12/C13；源 ID 保留；无相似度传递误合并 |
| R3-T05 | R3-US05 | 本地依赖索引和分区重算 | C14；增量与全量一致；删除来源后证据和关系正确撤回 |
| R3-T06 | R3-US07、R3-US08 | 批量规模实验、固定 E2F 对照 | C15/C16；报告覆盖/未决/成本，不重生成目标本体 |
| R3-T07 | R3-US06 | 可选 SAND 接口试验与往返核对 | 明确使用算法；有无 UI 均可批运行；条件损失可检测；不作为前六项依赖 |

计划命令，待实现：

```bash
python -m r3 map --config config/example.yaml --run-id e3-001
python -m r3 evaluate --run runs/e3-001 --baseline ../route2/runs/e2f-001
python -m r3 update --previous runs/e3-001 --input-manifest fixtures/changed_snapshot.yaml
python -m r3 sand-spike --config config/example.yaml
```

最小完成顺序：冻结一个模型 → 编译一条参数引用规则 → 全量执行 → 加条件规则 → 加身份/断言融合 → 做来源变化实验。不要先实现复杂图搜索或全局分布式调度。

代码要求：继续保持简洁，批处理调用已有函数；规则用小型 DSL 与固定操作枚举；canonical 映射用显式表；失效依赖用普通关系表；只有 SAND 适配器隔离额外依赖。

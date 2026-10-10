# 关联重构验收核对

截至 2026-09-28，以下核对基于生产代码 `261f9ecc`。全量 v8 已正常结束，状态为 partial；结果已归档，统一语义分析尚未进行。表中的工程回归验证程序约束；来源引文、测试通过、图大小和模型调用成功均不代表业务语义准确率。

| 用户问题 | 已实现 | 当前边界 | 实际验收证据 |
| --- | --- | --- | --- |
| 1. DataHub 作用与元数据建图 | 本地 YAML/CSV 生成表、列、声明约束和已核验技术关联图；组包实际查询图中的相关字段。见 [pipeline.py](../../route2/code/ontology_r2/pipeline.py#L253)、[instance_bundles.py](../../route2/code/ontology_r2/instance_bundles.py#L256)。 | 本地适配器采用 DataHub 兼容 URN 与导出格式。输入读取、关联核验和记录反查均由本地代码执行，没有运行 DataHub 服务。导出只含表属性、列定义及声明主键，技术关联没有导出为 DataHub 血缘。 | [元数据图回归](../../route2/tests/test_metadata_graph.py)检查声明、条件、转换、风险与组包投影；只能证明本地构图及格式契约，不能证明 DataHub 服务集成。 |
| 2. 关联搜索与减少重复访问 | 引用先去重，目标键先聚合，再核验匹配；按已核验规则反查实际记录。相同契约的同快照恢复跳过已接受和 no_change 包。跨快照可导入事实字段模板，重新核验契约后复用。见 [discovery.py](../../route2/code/ontology_r2/discovery.py#L718)、[group_incremental.py](../../route2/code/ontology_r2/group_incremental.py#L1121)、[pipeline.py](../../route2/code/ontology_r2/pipeline.py#L584)。 | 恢复仍导入、统计及扫描输入，没有实现已有关系数据零访问或通用 CDC。未知包和未决包仍需处理；实例名称与复杂配置变化也没有通用语义模板复用。 | [关联规则回归](../../route2/tests/test_association_rules.py)覆盖重复值连接和核验复用；[字段模板回归](../../route2/tests/test_fact_schema_induction.py#L108)覆盖新数值复用及当前快照证据重建。这些不等于百万行端到端复用验收。 |
| 3. 业务语义列依赖启发式 | 模型候选补充未知列；明确 ID/键与 name、alias、formula 冲突时保留为引用并记录原因；运算片段保留为参数，完整来源字段不再受固定角色槽数限制。见 [concept_candidates.py](../../route2/code/ontology_r2/concept_candidates.py#L70)。 | 固定规则仍存在。模型主要检查尚未分配角色的列，不会全面复核所有既有角色；来源样本正确也不保证业务角色正确。 | [列角色回归](../../route2/tests/test_column_role_inference.py)检查真实样本、未知列入口及错误引文；已完成对照 v3 验证了部分定义与计算契约，尚无独立业务准确率。 |
| 4. 无注释识别指标、维度和度量 | 无注释时可使用列名、真实去重元组、列角色及已核验关联上下文提出定义或绑定。见 [fact_schema_induction.py](../../route2/code/ontology_r2/fact_schema_induction.py#L59)。 | 匿名数值不能凭分布编造指标，缺公式或单位保持未知。不能保证任意无注释结构都能正确区分三类业务对象；范围成员、粒度与事实坐标的兼容仍有缺口。 | [无注释字段回归](../../route2/tests/test_fact_schema_induction.py#L56)覆盖有语义名称正例与匿名数值反例，使用受控模型响应。真实对照 v3 的事实字段绑定和业务事实实例仍均为 0。 |
| 5. 新结构的列角色泛化 | 按列补缺；已有 name 列不会阻止其他未知列进入有界模型判定。见 [column_role_inference.py](../../route2/code/ontology_r2/column_role_inference.py#L148)。 | 受支持角色、列数、样本和包预算限制；混合用途表仍缺更细的行级分流。没有证明对所有未知结构都能泛化。 | [已有名称与匿名语义列共存回归](../../route2/tests/test_column_role_inference.py#L124)验证入口不再整表跳过；真实新结构的分类质量仍需独立评估。 |
| 6. 异名字段值匹配 | 完整 distinct 倒排按值召回，不要求列名一致；支持低基数条件分支、别名及规范化转换，并在完整输入上核验。纯数字重合单独标记风险。见 [discovery.py](../../route2/code/ontology_r2/discovery.py#L475)、[association_rules.py](../../route2/code/ontology_r2/association_rules.py#L345)。 | 高扇出值、候选、条件探测和语义调用均有预算。全输入扫描不等于全部潜在关联召回；技术匹配不能直接确立业务谓词或实体相同。 | [异名、稀有键及数字碰撞回归](../../route2/tests/test_discovery.py)与[条件、别名全量核验回归](../../route2/tests/test_association_rules.py)验证已知反例。全量 v8 的实际覆盖与业务关系接受情况待统一分析时核对。 |
| 7. 元数据图展示 | 本体/元数据双视图，圆形节点、方向标签、缩放平移；默认按表展示，按需查看列、条件、转换、继承属性和证据。见 [viewer.html](../../route2/code/ontology_r2/viewer.html#L16)。 | 展示有界预览。数字重合候选默认收起，完整技术规则仍在产物中；页面不能证明关系语义或血缘成立。 | 对照页面已人工检查圆形节点、缩放、参数、公式与证据可读性；这只验证展示行为。全量结果的显示与业务语义仍需分别验收。 |

已完成的独立对照 v3 保留 8 条业务定义和 8 条计算依赖；事实字段绑定与业务事实实例均为 0，9 个观测值候选仍未决。当前不能据此宣布事实实例化或七项需求整体通过。结果边界见 [STATUS.md](STATUS.md#尚不能声称解决的问题)。

后续统一分析时，应核对实际 `manifest.yaml`、阶段 coverage、未决/拒绝原因及来源证据，再补充结论。固定来源核对方法见 [SEMANTIC_SAMPLE_AUDIT.md](SEMANTIC_SAMPLE_AUDIT.md)：C1–C3 仅用于对照输入，F1–F9 仅用于全量输入；这 12 个样本不是准确率基准。

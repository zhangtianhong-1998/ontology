# 路线2：记录关联与企业知识增强原型

当前已实现可运行原型：本地扫描先核验技术关联，再从有界跨表记录包抽取业务类型和关系；旧的 RIGOR 式逐表增量模式仍可选。AgentScope ReAct、stdio MCP 和本地结果可视化也保留。具体对象和关联由输入证据决定，不要求真实数据包含任何预设业务例子。

- [当前完成与待实现范围](../docs/route2/STATUS.md)
- [本轮重置、整改及实验验证](../docs/route2/OPTIMIZATION_AND_VALIDATION_20260925.md)
- [流式接口与关闭思考配置](../docs/route2/LLM_CONFIGURATION.md)、[环境变量模板](env.example)
- [安装、模拟运行、LLM 环境变量与实现边界](../docs/route2/IMPLEMENTED.md)
- [RIGOR 源码对照与本轮改造](../docs/route2/RIGOR_COMPARISON.md)
- [当前可运行配置](config/runtime.mock.yaml)
- [验证结果与未覆盖范围](../docs/route2/VALIDATION_REPORT.md)
- [LLM 环境变量示例](.env.example)
- [原型代码](code/ontology_r2/cli.py)
- [需求与 US](../docs/route2/requirements.md)、[SDD](../docs/route2/SDD.md)、[开发任务](../docs/route2/implementation.md)
- [字段统计与关联发现详细设计](../docs/route2/FIELD_PROFILING_DESIGN.md)、[方案比较与合成验证](../docs/route2/FIELD_ASSOCIATION_EVALUATION.md)
- [百万记录下的实例检索与增量组包设计](../docs/route2/INSTANCE_BUNDLE_DESIGN.md)
- [水果经营合成输入生成与验证](scripts/FRUIT_FIXTURE.md)、[本机实验记录](../docs/route2/EXPERIMENT_FRUIT.md)
- [Windows 11 无 Docker 离线运行与 DataHub 文件导出](../docs/route2/DATAHUB_OFFLINE.md)
- [上游代码与模型版本](resources.yaml)、[本体文件入口](ontologies/sources.yaml)

`code/RIGOR`、`code/AgentScope` 是未修改上游源码的 Git 子模块指针，查看源码时需在线初始化或提前转移；运行 `code/ontology_r2` 原型不需要它们。此路线没有 SAND、GRAMS 或 Steiner Tree 依赖。`config/example.yaml` 是较完整设计的配置草案，实际运行请用 `runtime.mock.yaml`。

当前 `profiling.py` 已按列组统计 CSV 快照，`discovery.py` 可对合格字段建立完整 distinct 值索引，并在预算内对候选做全输入精确核验。`association_rules.py` 可选用受限 ReAct 探索条件化物理匹配，`semantic_cards.py` 与 `instance_bundles.py` 对记录值做有界检索和组包，再由 `group_incremental.py` 进行证据约束下的增量抽取。技术匹配不自动成为业务关系；候选遗漏、未决结果和预算外范围均单独报告。一般化 JSON 路径、异名值转换和真实数据的语义质量验证仍未完成。`config/profiling.design.yaml` 仍是设计草案，不是运行配置。

无 Docker 的目标电脑继续运行本地原型。`ontology-r2 export-datahub` 可将已有 `meta_graph.yaml` 中的表/列元数据导出为 DataHub metadata-file；需要本地查询时，可另装 DataHub Lite，但 Lite 不提供图遍历或血缘。

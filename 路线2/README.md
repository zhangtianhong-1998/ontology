# 路线2：记录关联与企业知识增强原型

当前已实现可运行原型，采用 RIGOR 式逐表增量、AgentScope 原生 ReAct、真实 stdio MCP 协议和本地结果可视化；提供模拟服务及数据。具体对象和关联由输入发现，不要求真实数据包含任何业务例子。

- [安装、模拟运行、LLM 环境变量与实现边界](IMPLEMENTED.md)
- [RIGOR 源码对照与本轮改造](RIGOR对照与本轮改造.md)
- [当前可运行配置](config/runtime.mock.yaml)
- [测试说明与未覆盖范围](测试说明.md)
- [LLM 环境变量示例](.env.example)
- [原型代码](code/ontology_r2/cli.py)
- [需求与 US](requirements.md)、[SDD](SDD.md)、[开发任务](implementation.md)
- [字段统计与关联发现详细设计](字段统计与关联发现设计.md)、[方案比较与合成验证](字段关联方案评估.md)
- [上游代码与模型版本](resources.yaml)、[本体文件入口](ontologies/sources.yaml)

`code/RIGOR`、`code/AgentScope` 是未修改的上游源码；`code/ontology_r2` 是本项目原型。此路线没有 SAND、GRAMS 或 Steiner Tree 依赖。`config/example.yaml` 是较完整设计的配置草案，实际运行请用 `runtime.mock.yaml`。

字段统计升级仍处于设计阶段，参数草案为 `config/profiling.design.yaml`；现有 CLI 不支持该文件。`design/field_association` 仅验证统计口径，不替代正式候选发现模块。

# 文档目录

需求、设计、开发任务和使用说明按路线归档。代码、配置、模型契约和测试仍放在仓库根目录下的各路线工程中。

## 三条路线

| 路线 | 工程入口 | 需求与用户故事 | SDD | 开发任务 |
|---|---|---|---|---|
| 路线1：元数据构建 | [README](../route1/README.md) | [需求](route1/requirements.md) | [设计](route1/SDD.md) | [任务](route1/implementation.md) |
| 路线2：记录关联与知识增强 | [README](../route2/README.md) | [需求](route2/requirements.md) | [设计](route2/SDD.md) | [任务](route2/implementation.md) |
| 路线3：映射复用与融合 | [README](../route3/README.md) | [需求](route3/requirements.md) | [设计](route3/SDD.md) | [任务](route3/implementation.md) |

## 路线2专题

- [完成状态与待实现范围](route2/STATUS.md)
- [安装、运行与实现边界](route2/IMPLEMENTED.md)
- [LLM 流式接口与思考模式配置](route2/LLM_CONFIGURATION.md)
- [RIGOR 源码对照](route2/RIGOR_COMPARISON.md)
- [字段统计与关联发现设计](route2/FIELD_PROFILING_DESIGN.md)
- [字段关联方案评估](route2/FIELD_ASSOCIATION_EVALUATION.md)
- [实例检索与增量组包设计](route2/INSTANCE_BUNDLE_DESIGN.md)
- [测试说明](route2/TESTING.md)
- [Windows 无 Docker 离线运行与 DataHub 导出](route2/DATAHUB_OFFLINE.md)
- [验证报告](route2/VALIDATION_REPORT.md)

字段统计的 SQL 验证代码及就地说明保留在 [route2/design/field_association](../route2/design/field_association/README.md)。P01、P03、P04 已有首版代码，其他范围见[完成状态](route2/STATUS.md)。

## 公共资料

- [模型与数据契约](shared/contracts.md)
- [实验与评价设计](shared/evaluation.md)
- [报告及上游来源](shared/sources.md)
- [公开参考模型、许可与同步范围](PUBLIC_ONTOLOGIES.md)
- [Git 管理与资源获取](GIT_GUIDE.md)
- [仓库交付范围](DELIVERY_SCOPE.md)
- [内部一级模型](../shared/internal_model.yaml)、[YAML 合成示例](../shared/example_result.yaml)

运行命令在对应路线的工程目录执行，例如 `route2/`；不要在 `docs/route2/` 下运行。

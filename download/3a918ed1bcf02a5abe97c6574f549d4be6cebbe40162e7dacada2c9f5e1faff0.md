# 路线 2 元数据图契约

`meta_graph.yaml` 是从本地 YAML 元数据与 CSV 快照生成的源元数据图。DataHub 不参与建图或本体抽取；表节点上的默认 `postgres/PROD` dataset URN 仅供可选的 `export-datahub` 文件互操作。构建过程不运行 DataHub 服务、SDK 或 DataHub Lite。

## 节点与边

| 节点 | 稳定标识 | 含义 |
|---|---|---|
| `DatasetSnapshot` | `snapshot:<输入文件哈希>` | 本次输入快照；文件变化后身份变化 |
| `Source` | `source:<相对路径>` | 元数据 YAML 或 CSV 文件及哈希 |
| `Table` | `<schema>.<table>` | 源表；含注释、观测行数、主键列、默认 DataHub URN |
| `Column` | `<schema>.<table>.<column>` | 源列；含类型、注释和列序号；分析角色另存 `column_roles.yaml` |
| `Constraint` | `<表>:constraint:<定义哈希>` | 源约束；重排约束列表不改变标识 |
| `SourceRecordType` | `source_record_type:<表>` | 本地确定性生成的源行结构映射；不是原始声明或业务概念 |

`table_has_column`、`has_declared_constraint`、`declared_key_column`、`documented_by`、`sample_from` 保留源元数据及快照来源。`mapped_as_source_record_type` 是本地源结构映射；`declared_fk` 只来自原始外键声明。缺失于输入范围的外键目标标为 `ExternalColumnReference`。字段召回和核验另存 `field_candidates.yaml`、`association_checks.yaml`、`association_rules.yaml`，**不写成 `meta_graph.yaml` 的边**；即使技术匹配已核验，也不能据此断言业务对象关系、血缘或全局外键。

## 本体和实例的边界

每张表的 `SourceRecordType` 同时写入 `ontology.yaml`，全列直接映射见 `direct_mapping.yaml`。这些结构类型解决“源表及其行如何表示”，不解决“利润、收入等指标以及 SUM 等聚合操作是什么”。业务派生类型及关系在 `ontology.yaml` 中另行标记为 `business_type` 和 `business_relation_type`，接受证据存入构建步骤。记录实体写入 `objects/part-*.yaml`，使用源表、快照和行位置构造源局部 ID；具体物化数量受运行配置限制。页面只能展示有界预览，不能用预览节点数量推断全量实体数。

元数据图不复制所有记录，也不把业务实例关系混进 DataHub 导出文件。记录到业务概念的对齐、记录间断言及其证据保存在各自的输出分片。增量抽取可按表 ID、源记录类型 ID、字段 ID 连接这些产物；只有另存的已核验技术关联能作为后续组包候选输入，其选择条件、作用域和核验状态必须保留。

如果合法源表名无法转换为当前导出器支持的 DataHub URN，本地构图仍继续，表节点的 `datahub_urn` 为 `null`；可选的 `export-datahub` 则明确报错。使用非默认平台或环境导出时，导出文件按参数重新生成 URN，图中默认 URN 不会被冒充为接收端标识。

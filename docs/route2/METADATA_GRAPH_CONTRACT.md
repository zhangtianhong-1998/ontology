# 路线 2 元数据图契约

`meta_graph.yaml` 是从本地 YAML 元数据与 CSV 快照生成的技术图。它借用 DataHub 的 Dataset/Schema 字段组织方式，表节点保存一个默认 `postgres/PROD` dataset URN，便于对照 `export-datahub` 的离线交换文件；构建过程没有运行 DataHub 服务或 DataHub Lite。

## 节点与边

| 节点 | 稳定标识 | 含义 |
|---|---|---|
| `DatasetSnapshot` | `snapshot:<输入文件哈希>` | 本次输入快照；文件变化后身份变化 |
| `Source` | `source:<相对路径>` | 元数据 YAML 或 CSV 文件及哈希 |
| `Table` | `<schema>.<table>` | 源表；含注释、观测行数、主键列、默认 DataHub URN |
| `Column` | `<schema>.<table>.<column>` | 源列；含类型、注释、列序号和本地分析角色 |
| `Constraint` | `<表>:constraint:<定义哈希>` | 源约束；重排约束列表不改变标识 |
| `SourceRecordType` | `source_record_type:<表>` | 表中一行的物理结构类型；不是业务概念 |

`table_has_column`、`has_declared_constraint`、`declared_key_column`、`documented_by`、`sample_from` 和 `mapped_as_source_record_type` 都保留原始元数据或确定性映射。`declared_fk` 只来自原始外键声明。`inferred_technical_match` 来自字段值的核验，附条件、作用域、状态和计数；不能据此断言业务对象关系、血缘或全局外键。缺失于输入范围的外键目标标为 `ExternalColumnReference`。

## 本体和实例的边界

每张表的 `SourceRecordType` 同时写入 `ontology.yaml`，全列直接映射见 `direct_mapping.yaml`。这些结构类型解决“源表及其行如何表示”，不解决“利润指标、收入度量等业务概念是什么”。业务派生类型及关系在 `ontology.yaml` 中另行标记为 `business_type` 和 `business_relation_type`，接受证据存入构建步骤。记录实体写入 `objects/part-*.yaml`，使用源表、快照和行位置构造源局部 ID；具体物化数量受运行配置限制。页面只能展示有界预览，不能用预览节点数量推断全量实体数。

元数据图不复制所有记录，也不把业务实例关系混进 DataHub 导出文件。记录到业务概念的对齐、记录间断言及其证据保存在各自的输出分片。增量抽取可按表 ID、源记录类型 ID、字段 ID 连接这些产物；只有核验后的字段边能作为后续候选输入，且其技术状态必须保留。

如果合法源表名无法转换为当前导出器支持的 DataHub URN，本地构图仍继续，表节点的 `datahub_urn` 为 `null`；可选的 `export-datahub` 则明确报错。使用非默认平台或环境导出时，导出文件按参数重新生成 URN，图中默认 URN 不会被冒充为接收端标识。

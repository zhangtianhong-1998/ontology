# 共同模型与数据契约

本文中的对象、字段、公式和地区等均为假设性例子，不说明真实输入包含这些内容。运行时先检测证据和适用性；没有相应内容就不启用该提取器。目标模型的五类根也不要求输入覆盖全部类别。

## 1. 内部模型

[internal_model.yaml](../../shared/internal_model.yaml) 中的五类对象和五类关系来自用户确认。以下类型派生、文件布局及字段约定是本原型的实现设计。

| 层次 | 内容 | 例子 |
|---|---|---|
| 一级模型 | 固定的对象根和关系根 | `Metric`、`depends_on` |
| 派生类型 | 从一个根或已有派生类型继承 | `API: GeneralObject`、`calculation_depends_on: depends_on` |
| 业务对象 | 有身份的定义或业务实体 | 指标 `M17`、地区维度 `REGION`、API `A01` |
| 关系断言 | 两个对象的关系，或对象具有的字面值 | M17 计算依赖 Revenue；M17 具有公式字符串 |
| 来源与映射 | 业务对象及关系如何对应原始表、字段、记录 | `metric.code=M17`，公式字段字符区间 |

若输入实际存在指标定义记录，它通常形成一个 `Metric` 对象，不能因为多了一行就新增一个对象类型。记录定位符也不等于跨系统业务身份。表、列等技术节点保存在元数据图，不强行加入业务本体。

`GeneralObject` 与另外四种根并列。原型采用单父类型继承，每个对象沿类型链归到一个根；这是一项存储和类型检查约定，**不宣称五类对象在所有业务语境下构成逻辑互斥公理**。不同语义角色可由独立对象和关系表达。无法分类时进入未决列表，不增加第六个根。

### 1.1 派生规则

- 派生类型必须有 `parent`、定义、来源或设计依据；继承链不能成环。
- 派生关系保留父关系的 `kind`；对象关系连接对象，数据关系连接字面值。
- `calculation_depends_on` 可以派生自 `depends_on`。其定义域和值域可收窄，不能越过父级限制。
- `has` 使用 `attribute` 区分名称、单位、公式等字面值，也允许派生 `has_unit` 等数据关系；禁止把对象 ID 填进字符串来规避对象关系检查。
- `is_a`、`equivalent_to`、`mapped_to` 不新增为一级业务关系。类型继承由 `parent` 表达；模型对齐和来源映射分别存专门的记录。
- 不自动赋予 `related_to` 对称性、`contains` 传递性或一般 `depends_on` 无环性。仅对已声明的具体约束检查，例如特定公式依赖图无环。

`API`、`Parameter`、`DimensionMember` 和“允许某成员”等派生定义是本文示例，须由来源证据支撑后才能进入一次实验结果。根的中文标签不直接变成全局业务定义；`Measure` 与 `Metric` 的企业判别口径由元数据、记录和文档补充。

### 1.2 YAML 替代的范围

使用 YAML 保存上述模型及语义结果。采用 Pydantic 等模型定义生成结构化输出约束，LLM 可以返回受约束 JSON，由程序统一写 YAML；无需强迫模型直接拼接 YAML 文本。

程序校验类型继承、引用、字面值类型、已声明基数、身份作用域、条件和支持的局部规则。它不实现完整 OWL 描述逻辑，不声称与 OWL/HermiT 等价。外部 OWL 中无法表达的限制放入 `unsupported_semantics`，不能静默丢弃后再宣布等价。

## 2. 输入及身份

| 输入 | 必须保留 | 检查及解释 |
|---|---|---|
| tables YAML | schema、table_name、注释、关系类型、行数和大小估计、全部列定义 | 合并键为 `(schema, table_name)`；列按原序保留；`nullable = not is_not_null`；注释原文保留 |
| constraints YAML | 约束名称、代码、名称解释、原始 definition | 支持 PK/UNIQUE/简单 CHECK；复合键保留顺序。解析不了时保留原文和 unsupported 状态 |
| foreign_keys YAML | 空列表或完整外键定义 | 空、缺文件、解析失败分开；保留复合外键组，未能恢复列组时不拆成多个单列外键 |
| CSV（路线2、3） | 表名、原始值、列头、导入行号、文件哈希 | CSV 字段先以字符串保留；按 schema 创建辅助类型列，不损坏 `001` 这类编码 |

用 manifest 声明输入是 `sample`、`full_export` 或 `unknown`，默认 `sample`。`estimated_rows` 是估计，不能当作 CSV 实际记录数，也不能用样本未出现的值推断全库不存在。

YAML 使用安全解析，拒绝重复键和不支持的自定义标签；避免自动把编码、日期文本或 `yes/no` 改成另一类值。同名文件属于不同 schema 时使用显式 manifest 消除歧义，不覆盖其中一个。目录数量不一致必须记录具体缺项。

身份分两层：

1. `record_ref`：快照内定位。优先使用完整主键元组；无可靠键时使用文件哈希、行号等，注明 `identity_scope: snapshot_only`。重复行不能仅靠内容哈希合并。
2. `object_id`：语义对象身份。命名空间、业务编码、版本/有效期、所属 API 和完整参数路径按对象类型组合。无足够证据时保留来源局部 ID，不按名称跨表合并。

## 3. 共同结构

以下是接口字段约定，实际类型定义由路线1的首个开发任务实现。

| 结构 | 核心字段 |
|---|---|
| `SourceRef` | source_id、snapshot_id、kind、file/doc_id、table、record_key、columns、locator、content_hash |
| `Evidence` | id、claim_kind、source_ref、raw_fragment、scope、origin、supports、contradicts、derived_from |
| `ObjectType` | id、parent、label、definition、evidence_ids |
| `RelationType` | id、parent、kind、label、definition、domain、range、constraint_ids、evidence_ids |
| `AttributeDefinition` | id、label、definition、relation_type、domain、literal_type、constraint_ids、evidence_ids |
| `SemanticObject` | id、type、label、identity_scope、source_refs、evidence_ids、decision |
| `Assertion` | id、subject、predicate、object 或 literal、attribute、scope、condition、evidence_ids、decision |
| `Alignment` | internal_id、external_uri、mapping_kind、scope、evidence_ids、decision、external_version |
| `Delta` | add_types、add_relations、add_attributes、add_objects、add_assertions、add_alignments、unresolved |
| `Decision` | status、method、reason_codes、supporting_evidence、contradicting_evidence、scores |

断言必须且只能提供一个 `object` 或 `literal`。`literal` 采用 `{type, value}`，小数用十进制字符串序列化，避免浮点精度改变口径。数据关系应有 `attribute`，对象关系不使用该字段。

属性定义保存在 `ontology.yaml` 的 `attributes` 下，例如 `formula` 指定关系 `has`、适用对象 `Metric`、字面值类型 `string`。它是数据关系的字段定义，不是第六类一级对象。校验数据断言时同时检查 attribute 是否存在、是否适用，以及字面值类型是否一致。

`Evidence.origin` 区分 `declared_metadata`、`observed_record`、`parsed_formula`、`enterprise_document`、`external_model`、`model_inference`。保留原始来源引用和派生路径，摘要与原文不算两份独立证据。已有本体内容只能提供上下文，不能循环引用自身成为新增事实的证据。

`Decision.status` 使用 `accepted`、`rejected`、`unresolved`、`conflict`。`accepted` 表示满足当前自动接受策略，仍是需要实验评估的机器结论；`scores` 是排序或模型评分，不是已校准正确率。格式错误和工具异常写入运行错误记录，不能伪装为语义上的 `rejected`。

## 4. 条件与部分记录关联

把“成立范围”作为断言的一部分。`scope` 表示业务域、命名空间、API、版本和有效期；`condition` 表示记录选择条件。禁止只有一条裸边却丢掉条件。

```yaml
condition:
  status: known
  expression:
    op: in
    field_ref: catalog.api_region_rule.region_code
    values: [EAST, SOUTH]
  evidence_ids: [ev_region_rule]
```

条件还可为 `not_applicable` 或 `unknown`；`unknown` 不等于恒真。条件 DSL 初版支持 `eq/in/and/or/range`，值采用 schema 类型。程序执行采用三值逻辑：true 适用，false 不适用，unknown 输出未决；不运行 LLM 生成的任意 Python/SQL 表达式。

需要区分三类事实：API 指向地区维度；API 明确允许华东和华南；样本只观察到华东和华南。第三类不能自动推出第二类。范围规则须有明确配置、完整约束或适用文档；仅有地区名称时可以链接成员，不能推出成员集合完整。

断言 ID 根据主体、关系、客体或字面值、作用域和规范化条件生成。同一边有不同条件时分别保留。关系自身有独立属性和身份时，可用 `GeneralObject` 的派生类型表示关联对象，不增加新的一级对象。

## 5. 自动接受和合并

统一顺序：结构校验 → 源引用校验 → 身份和作用域检查 → 关系证据检查 → 冲突检查 → 接受或未决。

- 列值相等只支持匹配候选；同名、相似度高和高包含率都不能独立建立业务关系。
- 完整的来源引用字段、目标唯一身份、相容作用域和明确字段用途共同支持确定性关联。目标多义时不随意选第一条。
- 公式依赖要求公式解析成功并将符号绑定到具体定义；只抽到词不能断言完整计算依赖。
- 名称/说明匹配由 LLM 判别时，必须逐项引用候选记录中的证据并检查反例；不足时未决。同一 LLM 的 Judge 同意不构成独立事实证据。
- 外部模型只支持语义建模和对齐，不证明内部某条记录的身份、单位或公式。
- 合并以 ID 和条件为键。冲突并列保存，不按“最后一次生成”覆盖，也不因为同名自动做对象融合。

## 6. 产物布局与规模

约定的运行目录如下；引擎实现后生成，当前工程没有这些实验结果。

```text
runs/<run_id>/
  manifest.yaml              # 输入、代码/模型/提示词/配置哈希、预算、范围、状态
  ontology.yaml              # 五个根、派生类型、关系类型、受支持约束
  objects/part-*.yaml         # 业务定义对象；路线1为空
  assertions/part-*.yaml      # 带条件和证据的事实；路线1无记录级事实
  mappings/part-*.yaml        # 源结构或条件映射
  alignments.yaml            # 内部术语对齐外部模型
  evidence/part-*.yaml
  unresolved/part-*.yaml
  validation.yaml
  metrics.yaml
  trace.jsonl                # 工具请求/返回、可见解释、异常和耗时
  work/                      # DuckDB、Parquet、检索索引和缓存
```

YAML 是最终语义内容格式；Parquet/DuckDB 用作大批量计算中间文件，JSONL 用作运行日志。默认每片最多 5,000 条语义记录，manifest 保存分片哈希和计数；流式导出与校验，不生成百万行单文件。不要把所有原始数据复制进 YAML 本体。

运行状态为 `complete`、`partial`、`failed`：complete 表示已执行全部计划，不表示发现了所有真实关系；预算不足、来源失败或候选尚未处理时 partial，并保留数量和原因。输入契约错误等使流程无法执行时 failed。

冻结快照、排序、配置、模型版本、提示词、检索结果和响应缓存可以复现程序行为；模型在线重跑仍可能变化。分别报告响应回放的一致性与新调用的语义稳定性，不承诺 LLM 每次给出同一个正确答案。

## 7. 配置解释

配置中相对路径均相对于配置文件所在目录；`${NAME}` 只做环境变量替换，不执行 shell。只有启用的模块才解析其所需配置，故 E2L 无需设置 MCP 或外部模型目录。模块启用但缺必要配置时明确报错，不能静默禁用。

示例的 DATASET_DIR、LLM 服务和 MCP 映射由实际运行环境提供，未写入真实地址或密钥。配置加载、模型请求和文档取回结果的版本/哈希都写入 manifest；日志中的凭据脱敏。

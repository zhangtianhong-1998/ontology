# 路线2：软件详细设计

本文中的代码、配置路径及命令均以 `prototypes/route2/` 为基准。

本文中的对象、字段、公式和地区等均为假设性例子，不说明真实输入包含这些内容。运行时先检测证据和适用性；没有相应内容就不启用该提取器。目标模型的五类根也不要求输入覆盖全部类别。

## 当前原型与完整设计的对应

本轮已按 [RIGOR 对照与本轮改造](RIGOR_COMPARISON.md)实现确定性直接映射、逐表增量与回滚、AgentScope 原生 ReAct 检索及本地结果浏览。运行代码位于 `code/ontology_r2`，配置入口为 `config/runtime.mock.yaml`。

`storage.py/profiling.py` 导入并统计；`discovery.py` 召回和核验字段候选；`pipeline.py` 调度和实例校验；`incremental.py` 建立直接映射、选择 core 邻域并处理 delta；`models.py/validation.py` 定义计划及校验；`relations.py` 执行记录关联；`knowledge.py` 使用 AgentScope `Agent` + `ReActConfig`，`llm.py` 管理共享模型预算；`external.py` 导入参考模型；`visualization.py/viewer.html` 生成本地浏览页面。`datahub_adapter.py` 可把现有技术图导出为 DataHub metadata-file。下文未落地的 `r2/*` 接口仍为职责草案。

2026-09-23 新增[字段统计与关联发现详细设计](FIELD_PROFILING_DESIGN.md)及[字段关联方案评估](FIELD_ASSOCIATION_EVALUATION.md)。P01、P03、P04 已实现首版，P05 已把有限的已核验候选注入增量上下文；分层取样、结构化引用/文本候选、条件自动发现和自动计划编译仍未实现。准确范围见[状态表](STATUS.md)。

## 1. 总体执行

```text
YAML → 元数据图、键/注释/约束索引
CSV → 分批导入、快照定位、全字段基础统计
  → 选定字段频次/结构、分层样本、局部定义索引
  → 元数据/值域/文本通道召回字段候选
  → 原值、复合键、作用域及条件的精确验证
  → LLM 补语义判断并形成条件抽取计划
  → 标识关联 | 公式绑定 | 文本候选 | 维度条件解析
  → 有来源的候选对象和记录关系
  → 对未决知识问题：企业 MCP 检索 Agent（有界）
  → 对建模术语：外部模型召回与内部术语对齐
  → 按业务证据包迭代生成 Delta、Judge、程序校验
  → 定义对象、带条件关系、本体、证据与未决项
```

输入契约、根模型、状态和 YAML 分片遵守[共同设计](../shared/contracts.md)。当前增量构建工作单元仍按表组织；每个单元另外接收最多三条已核验字段候选摘要。将跨表定义/关联问题组装为完整证据包仍是后续设计，不能声称已避免按表切分的引用链遗漏。

## 2. 模块与接口

复用当前 `ontology_r2` 的输入、模型和执行器。下表中的 `r2/*` 是原职责草案，新增统计与字段发现集中到 `profiling.py` 和 `discovery.py`，不要求先实现路线1，也不为每种字段建立一个 Agent。

| 模块 | 接口草案 | 责任 |
|---|---|---|
| `r2/ingest.py` | `ingest_csv(snapshot, config) -> ImportManifest` | CSV 流式导入、源行定位、类型辅助列、DuckDB 索引 |
| `r2/meta_graph.py` | `build_graph(snapshot) -> MetaGraph` | 技术节点、声明边、来源与邻居查询 |
| `ontology_r2/profiling.py`（首版） | `profile_fields` | 分组 SQL 聚合、空值/格式计数及有限取值样本；精确频次和分层取样待做 |
| `ontology_r2/discovery.py`（首版） | `propose_candidates`、`validate_candidate`、`discover_and_check` | 声明/名称/原值候选及单列精确核验；结构化引用、条件提案与计划编译待做 |
| `ontology_r2/datahub_adapter.py`（可选） | `export_datahub_metadata` | 将表、列、声明主键和来源写入 DataHub metadata-file；不替代本地图或实现血缘 |
| `r2/plans.py` | `propose_plans(schema, summaries) -> list[ExtractionPlan]` | LLM 有界角色判别、计划验证、条件覆盖检查 |
| `r2/relations.py` | `discover(plans, store) -> CandidateIterator` | 标识匹配、候选召回、公式/条件解析和关联统计 |
| `r2/knowledge.py` | `retrieve(question, adapter, budget) -> KnowledgeResult` | MCP Agent 循环、摘要和证据定位 |
| `r2/pipeline.py` | `build(config) -> RunManifest` | 分阶段执行、证据包、改造 RIGOR、预算、输出 |

公式和条件解析先作为 `relations.py` 的独立纯函数；复杂到难以维护时再拆文件。DuckDB 存批量记录与中间表；技术图先采用节点表、边表加 Python 邻接索引。元数据表数量小时不需要专门图数据库。

## 3. 元数据图

当前技术节点：`DatasetSnapshot`、`Table`、`Column`、`Constraint`、`Source`。当前技术边包括 `table_has_column`、`has_declared_constraint`、`declared_fk`、`documented_by`、`sample_from`、`includes_source`。这些名字属于元数据层，不是新增内部一级业务关系。

每个节点和边都有来源、提取方法和状态。`declared_fk` 只能来自声明外键；候选引用放在 `candidate_record_reference` 中，不能回写成声明。无外键时根据表列角色和检索建立工作邻居，它只表示“值得一起分析”。

CSV 记录不全部变成内存图节点。元数据图引用 DuckDB 中的记录集合、统计和候选边；仅选定证据包取少量完整相关记录。

目标电脑为无 Docker 的 Windows 11，因此运行时保留本地技术图与 DuckDB。DataHub 仅作为可选互操作格式：导出表/列 metadata-file，另用隔离环境中的 Lite 做本地存储和读回。Lite 不支持关系图遍历或血缘；完整 DataHub 服务不属于此离线原型的运行依赖。安装与验证命令见[离线说明](DATAHUB_OFFLINE.md)。

设计目标是增加 `candidate_field_association` 技术边，保存候选通道、统计范围和检查状态。首版先把候选与核验写在 `field_candidates.yaml`、`association_checks.yaml`，未并入 `meta_graph.yaml`，更未直接变成 `points_to` 或声明外键。

### 3.1 字段统计路径

P1 对全部字段做分组 SQL 聚合，区分 NULL、空字符串、空白、可用值、近似基数、长度与格式。P2 只对候选标识/范围列建立精确频次和键多义统计。P3 把选定 JSON 路径、列表元素与公式符号转换为带来源的虚拟字段。P4 用固定内容哈希、高频、稀有结构及反例记录代替前几行样本，并报告未覆盖模式。

计数范围区分“原库导出是否全量”和“本次是否扫描完输入”；所有近似值、采样值和未完成统计明确标记。空值策略、规范化碰撞及逐项 SQL/样本口径见[详细设计第3节](FIELD_PROFILING_DESIGN.md)。

## 4. 记录关联算法

### 4.1 有界计划生成

输入表列注释优先；没有必要时不重新解释注释。程序先以元数据、值域倒排、结构引用和文本检索产生有限候选，再测原值交集、目标多义、作用域和条件覆盖。LLM 读取这些证据与完整代表记录，补充引用用途和业务关系含义，生成受限计划。模型新增的候选也必须重新经过程序验证。

```yaml
plan_id: output_metric_reference
source_table: catalog.api_output
selector: {op: eq, field_ref: catalog.api_output.semantic_kind, value: metric}
subject:
  key_fields: [api_id, api_version, parameter_path]
  proposed_type: Parameter
extractor: identifier_lookup
reference_field: metric_code
target:
  table: catalog.metric_definition
  key_fields: [namespace, metric_code, version]
  bindings:
    namespace: {source_field: namespace}
    metric_code: {source_field: metric_code}
    version: {source_field: metric_version}
proposed_relation: points_to
evidence_fields: [metric_code, semantic_kind, description]
```

此例字段是合成字段，实际计划从真实 schema 生成。计划只允许已有字段、合法比较、允许的提取器和类型；程序编译成参数化查询，不执行自由代码。缺版本字段时不能复制 API 版本充当指标版本，必须另有证据或保留歧义。

首批样本按字段角色、实际类型标记、引用格式、结构和空值模式分层。角色不明确时仍保留有限值域/文本候选；最多一次定向补样。未覆盖组、未处理候选和来源范围进入报告。数据驱动的条件先记为观察子集，不能通过枚举行 ID 或“匹配成功”本身构造业务规则。

### 4.2 标识与引用匹配

1. 建立目标索引：`(target_role, namespace, business_code, version, parent_scope)`。保留原始编码，规范化值作为辅助列，不能把 `001` 无条件转成 `1`。
2. 从显式引用列、列表、JSON 路径抽取候选值。列表解析依 schema 和样式；无法明确分隔符时不乱拆。
3. 在作用域相容的目标索引中查找。复合键用整组；字段名相同不代表身份相同。
4. 输出匹配记录、覆盖率、目标多义数、作用域一致性、来源说明；目标唯一性标记 `declared` 或 `observed_in_snapshot`。
5. 同时满足引用用途明确、目标可定位、范围相容时，可接受快照内记录关系。只有样本唯一时，不能顺带声明全库唯一键或全表 FK。

包含率用于候选排序和定位缺失，不用统一阈值宣布外键成立。对“只有 2% 出参引用指标”的列，2% 可能就是正确适用子集。分母须分别报告全体源记录和命中 selector 的源记录。

```text
条件内匹配覆盖率 = 命中 selector 且成功定位目标的非空引用数
                 / 命中 selector 的全部非空引用数
```

上述比率还需拆成“命中任意目标”和“唯一定位目标”，并与不同键包含率分别报告；列表/公式的引用数与源记录数分开。NULL、空引用、缺作用域、解析失败和预算未处理不混入同一分母。精确口径以[详细设计第4节](FIELD_PROFILING_DESIGN.md)为准。

### 4.3 指标公式与度量绑定

首版支持一套明确的小语法：标识符/带引号名称、数字常量、括号、`+ - * /`、已登记函数及参数。使用解析器建立 AST，不以正则抽词代替整个公式含义，也不直接执行公式。

每个符号保存原文区间、候选对象及绑定方法。按“命名空间/版本内精确编码 → 明确别名 → 名称/定义候选”绑定。一个公式可关联多个目标；参数位置和重复引用保留。只有所有相关语法可解释、所需符号可定位时，才能声称公式依赖完整。支持部分 AST 时可导出已经有独立证据的依赖，但 `dependency_completeness: partial`。

`SUM(x)` 的函数节点不成为度量；数字不成为对象；同名但粒度、单位、期间不同的度量保留歧义。解析出依赖不等于已证明公式数值正确、可加性或运行血缘；相应结论需要额外规则和证据。

### 4.4 名称、定义与上下文候选

目标文本卡由名称、定义、单位、公式说明、业务域、维度体系、层级、版本组成，保留字段出处。候选集合取以下通道并集：精确标识、名称/别名词法召回、定义语义召回；词法可用 BM25/字符 n-gram，语义用可配置 embedding。

仅对去重后的疑难查询组做 top-k 检索，默认词法和语义各 10 个；精确引用已发现的多个真实目标不受 top-k 截断。合并可先用倒数排名融合 `sum(1/(60+rank))` 排序，常数 60 是初始实验参数。分数只排序候选，不是接受阈值或事实概率。

程序先检查确定冲突：目标角色不相容、命名空间冲突、已明确的单位/版本不符。缺信息表示 unknown，不当作反证。LLM 对剩余候选输出 `reference/same_concept/calculation_dependency/related/unresolved` 等任务结论与逐字段依据；这些结论再映射到内部根/派生关系，不能自由增加一级关系。

LLM 同意 `same_concept` 先保存为同一概念候选，路线2不据此跨来源改写对象 ID；全局身份融合交给路线3。

### 4.5 API 的维度和成员条件

分开执行三种提取器：

- `dimension_reference`：参数/API 明确指向某维度定义或编码体系。
- `member_reference`：具体值绑定到该维度下的成员；同名成员必须带维度、层级、版本定位。
- `allowed_member_rule`：解析显式的允许列表、过滤配置或有效文档规则，输出范围条件与完整性。

例：API 说明为“查询地区列表”，配置为 `region in [EAST,SOUTH]`。维度关系、两个成员关系和该范围规则分别有来源；配置仅适用于 A01/v2 时，不扩展到 A01/v1 或同表其他 API。

如果原始值只是返回样本，保存 `observed_member` 观察证据；不生成允许范围。若规则写“除特例……”，当前 DSL 不能表达时，保留原文和 unsupported/unknown，不删除否定后当作正向规则。

### 4.6 接受策略

算法发现候选后统一调用共同校验器。明确引用、成功符号绑定等走确定性路径；语义模糊走有界 LLM 判别。关系只对 selector 为 true 且绑定成功的记录生成。不得将一个样本被判对推广到全表。

每次批量复用计划都检查选择条件、身份范围、必需字段和歧义数。匹配多目标允许合法一对多，如指标依赖两个度量；规则本来要求单目标而实际多义时未决。

## 5. 百万记录的执行设计

设记录数为 N、目标定义数为 M、需要语义判断的不同组数为 U、每组候选数为 k。全量工作由流式扫描、索引构建、哈希/排序关联和 AST 解析完成；不建立 N×M 候选对。内存不足的 join 和排序允许 DuckDB 落盘。

组键包含提取器、源角色、规范化定义或公式结构、候选上下文、单位/口径、业务域和版本。内容相同但作用域不同不能共享语义结论。分组只复用提取规则或判断，不合并记录身份；每条展开结果仍有自身 SourceRef。

- 当前执行器每次读取 1,000 行，运行内存默认 1 GB；新统计按最多 32 列聚合，昂贵频次与 JOIN 按候选执行并允许落盘。这些是实验起点，不是性能承诺。
- 原始 ID 关联与可解析公式尽量不调用 LLM；已解析公式结果按原文、语法版本和绑定作用域缓存。
- 每个疑难组最多送 10 个候选、6,000 个输入 token；完整事实单元装不下则拆成独立问题或未决，不能静默裁掉条件。
- 实际全局上限沿用运行配置；当前模拟配置为 30 次模型调用和 100 个疑难组，新发现阶段共享同一预算。`config/example.yaml` 中较大的数值属于旧实验草案，不自动覆盖运行值。
- 未处理组与记录用计数、分区条件和原因保存；可以不逐行写一份重复未决文本，但必须能枚举具体范围。

若百万条记录都是全新定义，U 可接近 N。框架只能保证成本受控和未处理范围透明，不能保证固定预算内完成所有语义判断。

## 6. 企业 MCP Agentic RAG

### 6.1 触发与问题

仅对本地证据缺口触发，例如“收入指标的统计口径是否包含退款”“该 API 的区域限制在哪个版本生效”。问题由原始术语、已有说明、命名空间、已知条件和缺失项组成。不要用整份生成本体作为查询，也不要把模型猜出的同义词写成已知事实。

当前调用输入为 `{question, unit, tables, evidence, table_catalog, current_core, root_model}`；Agent 根据已有信息判断缺口。`max_units` 限制检索单元数。更细的 KnowledgeQuestion 契约可随后引入，不伪装成当前接口。

### 6.2 MCP 适配契约

```python
search(query, filters, limit) -> list[SearchHit]
fetch(document_id, locator) -> DocumentFragment
```

这是内部适配器约定，**不是已经发现的企业 MCP 工具名**。实际实现时从可用工具映射名称和参数。SearchHit 至少含稳定文档 ID、标题、摘要/片段、可用定位与来源元数据；fetch 返回原文及版本/更新时间（如来源提供）。如果 MCP 只有检索摘要，记录 `snippet_only`，不假称取到了原文。

工具限定为查询和读取。返回文档中的指令只作为资料，不改变工具权限、预算、根模型或输出格式。

### 6.3 轮次与停止

当前实现由原生 ReAct 控制查询与读取次序，默认最多 3 次迭代、可配置到 5 次，另有 1 次结构化收尾。每个查询最多返回 5 条命中；每个单元的 MCP 调用默认最多 20 次、原文最多 10 份。下表是提示中的建议策略，不是程序写死的步骤。

| 轮次 | 行为 |
|---|---|
| 第1轮 | 用源术语、定义问题和业务范围检索；读取候选原文 |
| 第2轮 | 根据实际结果补充同义表达、编码或文档中出现的关联概念；仍围绕原问题 |
| 第3轮 | 检查缺失范围、版本和相互矛盾的说法；必要时找反证 |
| 第4—5轮 | 仅当已有有效结果暴露明确缺口且预算足够时扩展 |

提前停止条件：缺口已得到范围明确的证据；所有查询完成但连续两轮没有新的相关证据；来源确定无匹配；剩余预算不足。工具故障按独立重试策略处理，不解释为知识不存在。每次取回都按文档 ID、版本和片段哈希去重。

### 6.4 相关性、摘要及输出

先检查主题相关、来源可定位、业务范围和版本是否相容；元数据缺失记 unknown，不捏造文档权威等级。对冲突资料保留各自范围和原文，不能用一篇概括性总结盖过具体限制。

`KnowledgeResult.status`：`useful/no_evidence/conflict/unavailable/error/budget_exhausted`。字段：`claims, contradictions, source_fragments, rounds, queries, stop_reason, cost`。无有效知识时 `claims: []`，提供给本体生成器的知识上下文为空；错误原因保留在轨迹和 manifest。

摘要以原子 claim 组织，每条含 `statement, scope, source_id, locator, excerpt, missing_fields`。摘录只保留足以核查的原文；数字、否定、例外、期间和适用条件必须保留。程序检查引用和数值，LLM 可做“摘要是否被原文支持”的局部检查，但不能把自检当作独立证据。若原始关键语义无法装入预算，输出 unresolved，而非截断后接受。

## 7. 外部本体与 RIGOR 改造

外部导入/对齐复用当前 `external.py`，可供后续路线1适配使用。内部根模型优先；先以本地定义产生内部候选，再在外部模型中寻找可能对应概念。建立翻译/别名索引但保留原 URI，不把外部标准的定义直接覆盖企业口径。

每个生成任务的 `EvidenceBundle` 分开提供：

1. 物理元数据及来源；
2. 已发现对象/记录关系、公式 AST、条件及反例；
3. 相关内部类型定义；
4. 企业文档 claims 与冲突；
5. 外部模型卡与对齐候选。

LLM 生成派生类型、关系类型及待接受断言；程序已确定的来源引用不需重新由 LLM“猜”一遍。分批任务按稳定次序合并；事实绑定仍由程序依据计划执行。已有 core 只用于类型一致性和复用，保留原始证据，不计作独立新来源。

## 8. 错误、验收和资源

未决原因至少包括 `missing_target`、`ambiguous_identity`、`scope_unknown`、`formula_unsupported`、`unbound_symbol`、`condition_unknown`、`insufficient_evidence`、`source_conflict`、`budget_exhausted`。运行错误另列 `mcp_timeout`、`input_error`、`invalid_output` 等。

公式解析失败不等于无依赖；没检索到目标不等于目标不存在；源样本缺成员不等于该成员非法。每类状态都需要测试和计数。

验收按 C01—C12、C15—C16 与 E2 系列执行，参见[共同实验设计](../shared/evaluation.md)。主要交付指标是带条件关系的正确率/召回、候选漏失、证据支持、未决范围及总成本。MCP 还需独立报告命中相关率、摘要支持率和工具错误率。

## 9. 增量记录与本地界面

当前单次构建内的 core 累积、事务式接受/回滚、Judge 修正复验、直接映射覆盖和 UI 规格见 [本轮详细设计](RIGOR_COMPARISON.md)。所有调用有可见输入、结构化输出和工具轨迹，不记录模型隐藏思维。

`ontology-r2 visualize --run <目录>` 从 SQLite 有界读取结果，生成无外部依赖的 `viewer.html`。实例关系、类型、本体构建步骤、知识增量和未决项均可浏览；证据详情显示原文、表/字段或文档来源及适用条件。源文本只按文本渲染。界面显示的是运行产物，不自动修改或确认本体。

## 10. 模型响应与思考控制

`StructuredLLM.complete` 是普通抽取和 ReAct 的统一响应入口，`transport.py` 使用固定版本 AgentScope 的解析与流式累积能力。JSON/SSE 共享超时、用量统计、完成状态检查和思考参数。有效参数进入 manifest 与缓存身份；不完整响应不进入成功缓存。配置契约及验收见 [LLM_CONFIGURATION.md](LLM_CONFIGURATION.md)。

三个路线的工程路径统一为 `route1/metadata_only`、`route1/metadata_with_ontology`、`route2`、`route3`，文档正文继续使用中文。上游子模块的原始文件名和源码保持原样。

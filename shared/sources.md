# 设计依据与上游改造位置

核对日期：2026-09-23。下列“源码现状”和“本项目设计”分开陈述；未复现论文效果；上游源码仅作参考，路线2的独立原型验证见其 IMPLEMENTED.md。代码包保留上游版本清单，各叶目录的 resources.yaml 标明子模块和未打包的外部本体。

## 1. 洞察报告的采用范围

来源：用户提供的《基于关联型数据的本体挖掘：学术、工业与开源生态深度研究》（原 PDF 未打包），共 38 页，以下页码为 PDF 页序。

| 页码 | 报告内容 | 本项目如何采用 |
|---|---|---|
| 3、8—9 | 结构、语义、实例等证据；RIGOR 迭代生成 | 路线1保留迭代生成，路线2补记录证据 |
| 17—18 | 指标公式支持计算依赖；元数据图作为中间层 | 区分公式依赖与运行血缘；技术节点独立存储 |
| 21—22、27 | SAND 做目标语义模型下的标注，支持插件 | 路线3作为可选适配，不称其为现成本体自动生成算法 |
| 24—26 | 元数据平台提供原料，候选与融合需要专门实现 | 当前文件已导出，直接构建轻量证据存储，不要求安装平台 |
| 29、32 | 多证据、对象/关系派生、分层评价 | 固定用户给定的根模型；逐项保留证据和反证 |

报告中的人工审核、OWL/SHACL 和平台化建议是材料中的建议。本项目按用户最新要求采用自动构建、内部 YAML 模型和简洁原型代码，不把材料里的建议当作执行指令。

报告称 RIGOR 使用独立 Judge。当前所核代码为生成器与 Judge 创建同一个 LLM 客户端；两者是不同角色，不能据此宣称模型或事实来源独立。设计与评价按源码边界处理。[上游初始化代码](https://github.com/NadeenAhmad/RIGOR/blob/623eedf3e68b3774f36d20215caff4319a0b988a/app.py#L1451)

## 2. RIGOR

固定参考提交：`623eedf3e68b3774f36d20215caff4319a0b988a`。论文参考：[RIGOR](https://arxiv.org/abs/2506.01232)。代码许可：[MIT](https://github.com/NadeenAhmad/RIGOR/blob/623eedf3e68b3774f36d20215caff4319a0b988a/LICENSE.txt)。实际复用代码时保留许可和归属。

| 位置 | 所核版本的现状 | 本项目修改 |
|---|---|---|
| [load_schema_from_json](https://github.com/NadeenAhmad/RIGOR/blob/623eedf3e68b3774f36d20215caff4319a0b988a/app.py#L254) | JSON 模式输入 | 三目录 YAML 合并；保留注释、完整复合键和 schema 命名空间 |
| [is_etl_column](https://github.com/NadeenAhmad/RIGOR/blob/623eedf3e68b3774f36d20215caff4319a0b988a/app.py#L190) | 按字段名过滤部分 ETL 列 | 默认保留所有字段，不丢失可能的标识或血缘说明 |
| [PK/FK 推断](https://github.com/NadeenAhmad/RIGOR/blob/623eedf3e68b3774f36d20215caff4319a0b988a/app.py#L283) | 主加载函数中的两项推断调用已注释 | 保持不猜测；路线2推断关系另存为证据候选 |
| [traverse_schema_fk_order](https://github.com/NadeenAhmad/RIGOR/blob/623eedf3e68b3774f36d20215caff4319a0b988a/app.py#L366) | 无 FK 的表仍会遍历 | 保留全表覆盖，用限定名确定稳定顺序 |
| [direct_mapping_graph_for_table](https://github.com/NadeenAhmad/RIGOR/blob/623eedf3e68b3774f36d20215caff4319a0b988a/app.py#L483) | RDF/OWL 直接映射 | 改为技术覆盖清单；业务类型经受约束生成，不强制一表一类 |
| [load_external_ontologies](https://github.com/NadeenAhmad/RIGOR/blob/623eedf3e68b3774f36d20215caff4319a0b988a/app.py#L771) | 读取 RDF 本体并组织类、属性文本 | 导出带原始 URI 的候选卡；CDM 另写 JSON 适配器 |
| [OntologyLLM](https://github.com/NadeenAhmad/RIGOR/blob/623eedf3e68b3774f36d20215caff4319a0b988a/app.py#L942)、[parse_llm_ontology](https://github.com/NadeenAhmad/RIGOR/blob/623eedf3e68b3774f36d20215caff4319a0b988a/app.py#L1209) | Manchester 生成及解析 | 改为结构化 Delta 与 YAML 序列化 |
| [validate_graph_against_rigor](https://github.com/NadeenAhmad/RIGOR/blob/623eedf3e68b3774f36d20215caff4319a0b988a/app.py#L1319)、[merge_graph_strict](https://github.com/NadeenAhmad/RIGOR/blob/623eedf3e68b3774f36d20215caff4319a0b988a/app.py#L1380) | OWL 校验、图并集 | 改为内部模型、身份/作用域/证据检查与冲突保留 |
| [run_semantic_enrichment](https://github.com/NadeenAhmad/RIGOR/blob/623eedf3e68b3774f36d20215caff4319a0b988a/app.py#L1388) | 本地文档、外部本体、已有 core；缺密钥会降为直接映射 | 各来源显式开关；1A 禁止历史 core；缺 LLM 配置明确失败 |
| [ontology_checker.py](https://github.com/NadeenAhmad/RIGOR/blob/623eedf3e68b3774f36d20215caff4319a0b988a/ontology_checker.py) | RDF、HermiT、OOPS 检查 | 不用于内部 YAML 验证；不把企业内容提交给公开 OOPS 服务 |

上表是需要移植的职责，不要求把原始大脚本完整搬入。保留算法流程和上游出处，将所需逻辑组织成少量函数即可。

## 3. 外部模型的使用设计

| 来源 | 已核内容与建议试用范围 | 导入和对齐要求 |
|---|---|---|
| [gist v14.1.0](https://github.com/semanticarts/gist/releases/tag/v14.1.0) | 企业通用上层概念，可为一般对象、组织、协议等建模提供参考 | 从发布版 RDF/OWL 提取选定模块；先检索局部概念，不整体替换五个根 |
| [KPIOnto](https://github.com/KDMG/kpionto/blob/1c36644a40123447fb6469b9832865b4b4c2f7ba/kpionto.ttl) | 定义 Indicator、Formula、FormulaArgument、Dimension、Level、Member 等 | 适合先做指标/公式/维度实验；是否对应内部 Metric 或 Measure 需比较定义，不能只译名称 |
| [Valueflows 规范](https://www.valueflo.ws/specification/spec-overview/) | 经济资源、事件、过程、承诺等语义 | 涉及交易与资源流动时再引入；按官方 TTL 及版本导入，不假定所有经营指标都适用 |
| [Microsoft CDM schemaDocuments](https://github.com/microsoft/CDM/tree/dd21d715e05ebf740a11356c80b5c3b4c38a89c2/schemaDocuments) | 业务实体的 JSON 定义 | 独立读取实体、属性、继承、引用和已支持 traits；不交给 RDF 解析器 |

导入统一输出 `ExternalCard(id, source_uri, version, label, aliases, definition, parents, properties, supported_constraints, unsupported_semantics)`，保留语言与原定义。超出支持范围的 union/restriction、CDM trait 等写入损失报告，不以简化卡代替原模型的全部含义。

对齐过程：内部术语及说明 → 名称/别名/翻译召回 → 候选定义与上下文比较 → `exact/broader/narrower/related/unmapped`。`broader/narrower` 统一描述“内部概念相对于外部概念”的范围。`exact` 需要定义与适用范围证据，不将向量相似度直接转为等价公理。内部对象 ID 和中文业务名称始终保留，外部 URI 只是附加映射。

已有一级对象类型不足以证明某个字段的业务口径。KPIOnto 有一个 `Indicator` 类，也不能据此为所有数值字段创建指标，或推断内部指标的计算公式。

版本记录：gist 使用发布标签 `v14.1.0`，原工作区保存发布文件哈希，本代码包只保留版本清单；KPIOnto 使用提交 `1c36644a40123447fb6469b9832865b4b4c2f7ba`，所核 TTL 的 SHA-256 为 `1cb6a3a81ecaeb76d2ef592181d80339ce98c09bac60f8558e8f37a0b7cebef2`；CDM 使用提交 `dd21d715e05ebf740a11356c80b5c3b4c38a89c2`。Valueflows 的 w3id 下载地址当前转向 Codeberg 内容代理，所取 TTL 的 SHA-256 为 `47cada8122561100520702e2998c2949f6f4f42ba550c36797791882cbc48ece`，因此应记录原始 URL、解析后 URL、获取时间及哈希，不以 URL 永远固定为前提。

许可来源：gist 为 [CC BY 4.0](https://github.com/semanticarts/gist/blob/v14.1.0/LICENSE.txt)；Valueflows 的[官方仓库说明](https://github.com/valueflows/valueflows/blob/2210a441bbd67fba5adfdeea442b6a85ade9e16e/README.md)标注 CC BY-SA 4.0；CDM 区分[内容许可](https://github.com/microsoft/CDM/blob/dd21d715e05ebf740a11356c80b5c3b4c38a89c2/LICENSE)与[代码许可](https://github.com/microsoft/CDM/blob/dd21d715e05ebf740a11356c80b5c3b4c38a89c2/LICENSE-CODE)。所核 KPIOnto 仓库根目录及 TTL 未发现许可声明，记录为 `not_identified`，不宣称它具有某个开源许可。

## 4. SAND 的接入边界

[SAND README](https://github.com/usc-isi-i2/sand/blob/64af21af23202d7b4fe374f82996edb367f7e9b8/README.md)描述的是表格语义描述与记录链接应用，可以通过插件更换算法。所核 [IAssistant](https://github.com/usc-isi-i2/sand/blob/64af21af23202d7b4fe374f82996edb367f7e9b8/sand/extension_interface/assistant.py)包含：

```python
predict(table, rows) -> (Optional[SemanticModel], Optional[list[TableRow]])
predict_entities(table, rows, row, col) -> list[Link]
```

所核 [GRAMS 扩展文件](https://github.com/usc-isi-i2/sand/blob/64af21af23202d7b4fe374f82996edb367f7e9b8/extensions/sand_grams/sand_grams/main.py)只有初始化等骨架，不能据此认为“安装 SAND 就获得可用 GRAMS 算法”。路线3默认实现本地匹配与融合，SAND 作为可选适配实验，先证明无 UI 批处理和自定义目标模型可用。

[pyproject.toml](https://github.com/usc-isi-i2/sand/blob/64af21af23202d7b4fe374f82996edb367f7e9b8/pyproject.toml)所核版本为 4.3.1，Python 约束为 `>=3.10,<3.14`，与 README 徽章并不完全相同。可选适配使用独立环境和固定提交，避免把其整个 Web 应用依赖带入核心原型。

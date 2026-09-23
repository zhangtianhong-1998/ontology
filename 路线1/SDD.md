# 路线1：软件详细设计

## 1. 执行流程

```text
配置与根模型校验
  → 合并三目录 YAML → 源结构覆盖清单
  → 按声明 FK 排序；无 FK 时按限定名排序
  → 每表组装元数据、当前 core 的相关类型
  → 1B 才导入/对齐/检索外部模型
  → 生成 Delta → 结构校验 → Judge → 业务契约校验
  → 按 ID 合并，冲突保留 → 下一个表
  → 全局引用检查 → 导出 YAML、未决项与实验报告
```

技术覆盖清单完整保存 schema；业务 core 只接收通过策略检查的结果。物理主键和非空约束默认留在源映射层，不能直接变成跨来源业务对象的全局唯一或必填公理。

## 2. 模块与接口

以下路径为待开发布局，不代表文件已存在。三条路线共用 `shared/src/ontology_core/`，各路线保留独立 CLI 与配置。

| 模块 | 建议接口 | 输入/输出与职责 |
|---|---|---|
| `ontology_core/contracts.py` | 数据模型及 `validate_model()` | 根、派生、对象/数据关系、Evidence、Delta；动态检查继承 |
| `ontology_core/io.py` | `load_schema(root) -> SchemaSnapshot` | YAML 归一化、重复键检查、全限定名、完整约束 |
| `ontology_core/llm.py` | `complete(request, budget) -> Response` | 可配置模型客户端、结构化输出、统一预算与响应缓存 |
| `ontology_core/validate.py` | `validate_delta(delta, core, evidence) -> Validation` | 结构、类型、引用、证据、冲突；不隐式新增事实 |
| `ontology_core/store.py` | `merge_delta(core, delta) -> MergeResult` | ID 去重、冲突记录、稳定排序、YAML 导出 |
| `r1/pipeline.py` | `build(config) -> RunManifest` | 按表遍历、修复次数、1A/1B 开关和运行状态 |
| `ontology_core/external.py` | `import_cards()`、`align_terms()` | 选定模型文件解析、损失报告、内部术语对齐与局部检索 |

依赖先用 Python 3.11/3.12、PyYAML、Pydantic、一个 LLM 客户端。RDFLib 仅供外部 RDF 导入；向量检索可后加，首个 1B 用名称/别名和定义的词法召回即可。实际版本在实现时锁定。无需 Java、HermiT、OOPS、Web 服务。

## 3. 按表处理

`SchemaSnapshot` 保存表、列、约束、declared FK、source refs。FK 只用于已知邻居上下文和排序；无 FK 时依然遍历全部表，不启用名称猜外键。

每个 `TableWorkItem` 包含：

- 完整表注释、列注释和原始数据类型；键与约束原文/解析结果。
- 根模型、相关已生成类型及其原始来源。core 中的推断仍是推断。
- 1B 的少量外部概念卡及对齐关系；1A 此槽为空。
- 允许输出的 Delta 字段、当前可引用 ID、证据 ID 和预算。

大表按列组拆分时，各组均携带完整身份键、表注释、相关约束和组覆盖信息。最后一次整合只合并已生成片段，不让模型凭不完整上下文猜补遗漏列。约束无法完整装入时记录 `context_limit`。

## 4. 生成、Judge 与合并

生成提示词的任务是从现有语义说明提出派生对象类型、关系类型和映射候选。明确禁止输出来源中未出现的实例 ID、公式、单位、血缘和业务规则。

```python
for item in stable_table_order(snapshot):
    context = build_context(item, core, external_if_enabled)
    for attempt in range(3):  # 初次 + 最多两次修复
        delta = generate(context, previous_errors, budget)
        errors = structural_check(delta)
        if not errors:
            review = judge(delta, context, budget)
            errors = contract_check(delta, review, core)
        if not errors:
            merge_without_overwrite(core, delta)
            break
    else:
        save_unresolved(item, errors)
```

上述是流程伪码。LLM 超时、输出截断和预算耗尽由外层异常策略处理，不能无界重试。语义信息不足无需重复追问到模型给出结论，直接未决。

合并对新增定义按内部 ID 处理。新名称匹配已有类型只形成归一候选；若定义冲突，保留双方证据。1A 不加载历史结果，1B 也不能让未对齐的外部类直接变成内部一级类。

## 5. 外部模型适配

1B 读取配置中列出的本地快照文件。下载/准备外部模型作为单独准备命令，运行本体构建时不自动跟随网络 import，以确保比较可重放。

RDF 适配器提取 URI、标签、定义、父类、属性和支持的限制；CDM 适配器处理实体 JSON、继承和属性，按需解析本地 imports，循环或缺失引用明确报告。两者都返回 ExternalCard 和解析损失清单。

外部检索先使用内部表列术语和解释；LLM 可提出翻译/别名，但不能用翻译成功替代对齐判定。每个术语最多取 5 个候选；保留未命中，不强制关联。四个来源的使用定位与版本见[来源说明](../shared/sources.md)。

## 6. 错误与可复现性

| 情况 | 行为 |
|---|---|
| 缺 LLM 配置或密钥 | failed；若显式选择 `structural_only`，单列实验名称，不能冒充 1A |
| 输入不合法、重复限定表名 | failed，列明文件与字段 |
| 某表生成失败或超预算 | 保留该表 coverage 与错误，运行 partial |
| 外部文件损坏/导入不完整 | 记录 source error；1B partial，不能静默变成 1A |
| 外部模型语义无关 | 返回 unmapped；不是工具故障 |
| 根模型越界、证据 ID 不存在 | 拒绝 Delta，修复额度耗尽后未决 |

缓存键含模型及参数、提示词哈希、输入片段哈希、根模型版本、core 摘要哈希和外部模型快照。相同 key 才能复用；不同实验的缓存必须记录来源。保存可见请求、响应、校验错误和成本，不要求记录模型隐藏推理。

## 7. 与上游的差异

具体修改函数见[源码核对表](../shared/sources.md)。路线1保留生成流程，但内部模型、直接映射产物、生成协议、合并和校验均有变化。因此先验证自己的 YAML 基线，再与上游论文描述比较能力边界，不沿用论文质量数字。

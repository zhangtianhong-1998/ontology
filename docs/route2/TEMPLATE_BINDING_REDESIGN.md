# 定义复用、实例绑定与关系构建

本轮分支：`codex/ontology-template-rebuild`。冻结的 v8 结果与源码仍在 `codex/fruit-full-v8-results`。本轮全量实验使用新的语义一致性版本输入，不能将前后全部差异归因于算法。

## 用户需求与实现

| 需求 | 实现要求 | 验收方式 |
| --- | --- | --- |
| 水果合格率联系水果对象与合格率度量 | 根类型仍为 GeneralObject、Measure、Metric、Dimension、Term；成分有独立来源定义，通过登记的派生关系连接 | 本体中存在有来源的 Metric→GeneralObject、Metric→Measure；SUM 等运算符不能成为独立度量 |
| 苹果、菠萝等具体对象不反复生成类型 | 模板保存共同定义、公式、单位及不变条件；具体取值保存为观察到的绑定 | 第二批相容记录增加绑定，不增加模板，也不调用 LLM；没有来源的组合不得生成 |
| 具体看板匹配已有看板类 | 用类定义、完整结构与用途匹配，名称/编号留在实例；相似度只用于召回 | 类型保持“看板”，具体名称、引用、坐标出现在绑定与右侧详情 |
| 配置与元数据支撑本体关系 | 来源记录与定义端点分别映射；结构配置不要求专门自然语言关系列 | 已核验引用路径、用途与最终执行路径一致；纯数字碰撞不能生成业务关系 |
| 图谱可读、能操作 | 层级布局、避让连线、像素尺度缩放、聚焦、拖动固定与重排 | 真实 258 节点拓扑及多种窗口尺寸测试；搜索能够定位目标 |

## 抽取流程

1. 导入、统计、distinct 值候选与完整输入核验保留。DataHub 是本地元数据交换适配器，不负责推断业务关系。
2. BM25/向量及已核验引用召回有界证据包。每个包先查询已接受的模板。
3. 完整字段、不变条件和槽位契约匹配成功时，直接保存 `template_instance` 绑定。存在多个不同类型匹配时返回判定队列，不按最高相似度强选。
4. 未覆盖记录交给 LLM。模型可以提出独立模板投影，也可以保留原有精确定义路径。投影必须有原文依据；不能靠删除名称前缀推断通用性。
5. 编译器核验字段模板、见证、成分、单位和公式，再由复核任务判断。模板认可后用于之后的包，并在已有定义索引中按实际记录批量应用。
6. 模板成分、结构化配置和公式分别编译关系。成分绑定不自动等于公式依赖；未知操作数生成定向发现任务。
7. 输出本体、模板、绑定、关系证据、覆盖范围和独立 HTML。

`template_projection.py` 使用字面占位模板，不执行模型提供的正则、Python 或 SQL。投影与来源记录身份分开：同一个模板的多个成员不意味着它们是同一实体。

公式、单位和真实计算参数不能作为通配值。具体期间与统计粒度要区分：2025Q1 可以是观测坐标，按季度求和可能属于口径。预算/实际、分母与过滤条件变化不因名称相同而被忽略。

## 模板与关系契约

- `template_projections.yaml`：共同定义、成分、字段匹配方式、不变条件、来源证据和契约哈希。
- `template_bindings.yaml`：实际来源记录到已接受类型的匹配，保存槽位取值和独立身份。
- `template_binding_coverage.yaml`：检查过的记录、未覆盖/歧义、遗漏及数量上限。
- `ontology_bindings.yaml`：指标与经营对象、度量、维度之间的结构化绑定及未决项。
- `calculation_contracts.yaml`：真实公式、操作数绑定及缺口。

对象根不变。新增派生谓词 `business_object_binding → related_to`、`measure_binding → depends_on`。已有 `scope_constraint → related_to` 表示维度约束，`calculation_dependency → depends_on` 表示有公式证据的计算依赖。具体成员选择器单独存储，不塞进类型名称或关系类型身份。

目前模板仍绑定输入快照和契约。跨快照直接套用、任意自然语言规则的可靠执行，以及全部企业结构的识别准确率不在本轮已验证范围内。批量复用仍需程序检查实际数据，减少的是模型重复判断，不意味着数据库零访问。

## 模拟数据修订

原 `fruit_data_v2` 保持不变。新脚本 `route2/scripts/generate_fruit_semantic_data.py` 沿用 23 表物理结构、零声明外键与 1,102,238 行规模，修复：

- 26 种量的英文参数名与公式符号对应，补充合格数量、受检数量、收入和成本等定义。
- 合格率引用正确的数量，不再误指向均价；吨与千克换算显式写入销售额公式。
- 指标定义、公共属性中的公式和单位一致。
- 具体看板声明为实例，维度成员不再附带随机冲突地区。
- 保留已声明的缺失引用反例，算法仍须报告未决。

生成器提供的是可控正例与数据一致性检查，不是模型抽取的业务准确率证明。抽取代码不读取生成器的指标清单或预期结果。

```sh
cd route2
.venv/bin/python scripts/generate_fruit_semantic_data.py generate --output local_data/fruit_data_v3
.venv/bin/python scripts/generate_fruit_semantic_data.py validate --input local_data/fruit_data_v3
```

## 实验与预算

全量配置：`route2/config/runtime.fruit-templates.yaml`；小规模配置：`runtime.fruit-templates-smoke.yaml`。

- 实际接口使用本地环境配置的 `deepseek-v4-flash`，非流式、关闭思考，工具选择维持 auto。
- 全量调用上限 800；组包阶段为后续阶段保留 200 次。重试仍计入同一预算，缓存不消耗真实调用。
- 模板模式每次提出一个概念包，使刚接受的模板能在后续包调用前生效。关系包可独立批量判断。
- 输入不裁剪；候选、证据包、实例化及调用预算分别报告。扫描全量输入不等于语义结果完整。
- 独立后台进程或终端运行，正常结束或报错后写 `process_exit.json`。没有后台分析任务，也没有循环监控程序；用户通知后再统一分析。

```sh
PYTHONPATH=route2/code route2/.venv/bin/python route2/scripts/run_local_experiment.py \
  --config route2/config/runtime.fruit-templates.yaml \
  --env-file /path/to/local/.env \
  --output route2/runs/fruit-templates-full-<date>
```

运行目录保存 `manifest.yaml`、`experiment_snapshot.json`、`process_exit.json` 及自动生成的 `viewer.html`。实现代码、配置、输入快照和提示词各有标识。运行前先提交代码，运行中不修改该版本。

## 代码要求

原型优先保持代码简洁：模板、关系编译与视图各自使用小模块；不引入服务集群、任意代码执行、全量逐行模型调用或隐藏业务字典。测试覆盖约束和实际失败案例，不用通过数代替语义验收。

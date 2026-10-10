# 独立语义对照数据

数据由 `generate_semantic_control.py` 生成至 `local_data/semantic_contract_control`，共 5 表、15 行；没有声明外键。数据与当前水果全量实验隔离。本文件仅供评估，不得作为模型上下文、外部知识或抽取输入。

## 可观测验收

1. `general_measure_definition` 的收入和成本是通用 Measure 候选。完整定义没有绑定水果、地区或年份。不得生成仅名为 SUM 的度量，也不能因为另一个表名包含 metric 就改变它们的分类。
2. `business_metric_definition` 的苹果经营利润和香蕉经营利润有明确经营对象，应归为 Metric。每条定义的公式、单位和适用范围均来自自身行，不需要从关联上下文借用。
3. 基准指标 `metric_key=apple_profit` 的公式是 `revenue - cost`。两个符号分别等于 Measure 表唯一主键 `measure_key=revenue/cost`，可回查来源。收入应是被减数，成本应是减数。不能颠倒操作数，不能把常数 0.9 变成对象。
4. 三条同名“苹果经营利润”分别是未扣费、扣专项管理费、仅华东三种定义。它们的公式或适用范围不同，不得当作一个 exact 定义，也不得将一条定义的关系直接复制给另一条。它们可以共享上位名称，但上位名称不能抹去差异。
5. 检查异名字段技术候选：`operating_fact.metric_ref_key → business_metric_definition.metric_key`、`operating_fact.location_key → dimension_member.member_code`、`dimension_member.catalog_pointer → dimension_definition.dimension_key`。值匹配仅支持这些技术连接；业务谓词必须另有本行定义或声明证据。
6. `obs_bj_2025` 的业务对象是苹果，地区为 BJ（维度成员名称北京），年份 2025，收入 1000 元、成本 600 元、利润 400 元。其 `metric_ref_key=apple_profit` 明确选择基准定义，不能绑定扣费或华东限定定义。
7. 只保留实际存在的三个地区—年份组合：北京2025、北京2026、上海2025。上海2026没有事实记录，不能用维度值做笛卡尔积生成该实例。
8. 各数据属性、公式、条件、关联和实例都应能回到原表、列、主键记录及原文。全部列名、数据行和 YAML 列顺序必须一致。

## 不能断言的内容

- 不能从“全国”自行生成完整行政区层级，也不能为年份补出季度或月份实例。本数据没有这类层级声明或明细。
- 基准利润数值符合收入减成本，但本原型若未实现数值计算，只能将 400 元列为有来源的观测值，不能宣称由系统完成计算验证。下面的独立本地算术核对也不是抽取器的计算能力。
- Measure 自身的 `sum(revenue_amount)` 与 `sum(cost_amount)` 明确引用事实金额字段。现有计算契约若只绑定已接受本体类型，物理字段操作数可以保持未决；不能为了消除未决，凭空生成 revenue_amount/cost_amount 本体。Metric 的 revenue/cost 类型绑定应单独验证。
- 通用 Measure 不因在苹果指标中被使用而改成苹果专属定义。技术连接和记录归属都不等于同一实体。
- 测试中的定义是人工构造的正反例，只验证列出的能力，不代表真实企业数据召回率或语义准确率。

## 本地生成与核查

在 `route2` 目录执行：

```sh
.venv/bin/python scripts/generate_semantic_control.py
```

生成器拒绝覆盖既有目录。抽取时只将 `local_data/semantic_contract_control` 设为 dataset；不得把本验收文件和生成脚本设为知识来源。保留原始观察记录后，可独立核对三个利润值均等于同记录收入减成本，并验证同名反例仍是独立定义。

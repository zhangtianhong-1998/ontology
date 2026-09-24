# 路线 2 实验重置与改造基线

2026-09-25，在修改关联、本体编译和页面代码前，从提交 `03a8000e` 建立独立工作树运行基线。这里只保存对照摘要；旧的完整抽取产物不再作为活动结果。所有列出的水果输入均为生成的合成数据，不是用户真实业务数据。

| 基线 | 输入及配置 | 可复核结果 | 解释边界 |
| --- | --- | --- | --- |
| 标准 mock 构建 | `route2/config/runtime.mock.yaml`，`fixtures/linked`，2 表 13 行；快照 `18b2fe74…` | `status=complete`，4/4 字段候选已核验，模型录制响应调用 8 次，自动生成 HTML | 仅验证小样本工程链路，不证明业务本体质量。旧运行保存在本机临时目录 `/tmp/ontology-r2-baseline-20260925`，不随仓库发布。 |
| 水果字段/规则基线 | `runtime.fruit.example.yaml` 的发现与规则预算，`local_data/fruit_smoke_v2`，23 表 1213 行；快照 `50067c14…` | 336 个候选字段中仅 64 个进入值索引；1017 个字段对中核验 50 个；60 条规则为 10 条 `checked_technical`、3 条 `observed_subset`、47 条 `unresolved`。扫描约 4.22 秒。 | 这次只运行程序发现和关联规则，没有进行模型语义判断。摘要保存在本机 `/tmp/ontology-r2-baseline-fruit-summary.json`。 |
| 历史端到端产物 | `fruit-volc-e2e-replay`，同为 23 表 1213 行的合成数据 | `partial`，445 个物理属性映射、1 个派生对象类型、0 个关系类型 | 此产物由此前代码和缓存生成，不能当作本次提交的全流程重跑；只用于定位旧失败。 |

水果字段/规则基线中的关键漏检：`fruit_metric_attr.measure_code → fruit_measure_def.measure_code`、`fruit_measure_def.metric_code → fruit_metric_detail.metric_code`、`fruit_dashboard_card.metric_code → fruit_metric_detail.metric_code` 均被提出但未核验；`fruit_param_ref_rule.source_field` 到度量/指标定义的两个条件分支未被提出。后续同一输入快照、同一基础预算对照候选、核验、技术连接、业务关系和实例化结果；候选数增加不能代替语义正确性。

重置范围：清空 `route2/runs/` 下 28 个旧实验目录，约 1.2 GiB；它们均未被 Git 跟踪。`route2/local_data/` 的合成输入、配置、模型源码、测试及文档保留。新实验使用带日期和阶段名的新输出目录，manifest 写入代码、配置与输入快照摘要，并保留失败、跳过及未决状态。

重置后的代码变更、实验数字和剩余缺口记录在[优化与验证记录](OPTIMIZATION_AND_VALIDATION_20260925.md)。

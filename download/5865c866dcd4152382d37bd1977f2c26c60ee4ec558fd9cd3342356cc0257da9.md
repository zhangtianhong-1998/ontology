# 字段关联核验边界

`discovery.value_index_mode: full_distinct` 对合格的键、编码、名称、别名和引用列建立 DuckDB distinct 值字典；`max_indexed_fields: 0` 表示不再按字段数截断。它先召回字段对，再用原始 CSV 行对选中的字段对做全量连接，分别统计唯一命中、多义目标、缺失目标、缺失作用域和条件外命中。取值过长、跨字段过于常见、字段不符合索引角色或候选超出核验预算，都在 `discovery_coverage.yaml` 中列出；因此“已召回”不等于“已核验”，也不等于业务关系成立。

`source_type/source_field`、`business_type/business_id` 的条件候选只使用当前快照真实出现的类型值。例如 `source_type='DIM0001'` 的 `source_field` 可与维度成员编码比对，并用 `source_type → dim_code` 限定范围。条件、目标字段和作用域与快照一起写入候选；规则生成时必须复用同一个条件的全量核验，不能把条件内的命中扩大成整列引用。

百万行水果合成数据中，`source_type=measure` 和 `source_type=metric` 各有 429 个缺目标行。这是生成器在 `i % 101 == 100` 时故意写入 `UNKNOWN_SYNTHETIC_REFERENCE` 的负例，不是关联算法误报。规则状态应为 `observed_subset`，保留缺失数和反例；只有全部合格引用唯一命中且没有缺失作用域时才标记 `checked_technical`。关系组包与编译只允许后者，且仍需独立的业务语义证据。若有真实的附加条件可以排除负例，应把条件写入规则并重新全量核验，不能改写原规则状态。

现阶段只验证原值相等；别名转换、JSON 路径和跨表概念同一需要另行提出并验证。尤其是两个表的自增 ID 完全重合时，`checked_technical` 仅表示这组值能连接，不能证明它们代表同一业务对象。

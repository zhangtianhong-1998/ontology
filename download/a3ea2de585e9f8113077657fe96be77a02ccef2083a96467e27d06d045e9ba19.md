# 受控组包实验

在 `route2/` 目录运行：

```bash
uv run python scripts/run_group_compilation_experiment.py \
  --output runs/group-compilation-controlled-<唯一编号>
```

脚本生成 6 条水果经营定义记录及对应的 YAML/CSV，建立语义卡和组包；一条指标到度量的编码候选先经过全输入技术核验。脚本使用**预设的结构化决策**驱动组级编译，随后用现有 `Sink/Extractor` 物化源记录和关系断言、运行结构校验，并自动生成可打开的 `viewer.html`。输出目录还保存证据、计划、本体、覆盖范围和校验报告；已有目录不会被覆盖。

预期结果是 `Metric`、`Measure`、`Dimension` 下各一个业务类型，以及两层 `depends_on` 关系：第一层以两张表的 `source_record_type` 为端点，关系计划**只物化已引证的正例记录对**；其他通过技术匹配的行仍停留在关联规则中，不自动成为业务断言。第二层以“水果销售利润”指标类型和“水果销售收入”度量类型为端点，**只对同一正例中双方都精确对齐到业务类型的概念对**生成断言。第二层没有全表执行计划，也不会推及未对齐的记录。

第一层关系的证据范围是 `sample_semantic_with_full_technical_check`；第二层是 `one_positive_pair_with_exact_type_alignments`。它们只说明本次合成样本的自动检查通过，不能证明其他数据行的业务关系，更不能计算具体年份、地区的利润数值。

这是工程链路验收，不是在线模型实验，也不测量 LLM 的本体抽取准确率。回归见 `tests/test_group_compilation_experiment.py`。

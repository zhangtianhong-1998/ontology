# 水果经营合成输入

`generate_fruit_data.py` 用固定规则生成路线 2 可读取的三组 YAML 和 CSV。它不读取真实业务数据，也不调用 LLM。输出默认保存在 Git 忽略的 `local_data/`，避免把百万行文件提交到仓库。

```bash
cd route2
python scripts/generate_fruit_data.py generate --output local_data/fruit_smoke --scale 0.001
python scripts/generate_fruit_data.py generate --output local_data/fruit_data --scale 1
python scripts/generate_fruit_data.py validate --input local_data/fruit_data
```

需要 Python 3.11+ 和 PyYAML；从路线 2 的 `uv` 环境运行可用 `uv run python` 替换上述 `python`。输出目录须事先不存在，避免覆盖已有数据。全量为 **23 表、1,102,238 行**；脚本流式写 CSV，但全量生成和校验仍需足够的磁盘空间与时间。

提示词列出的五个域实际合计 **22 张表**（6+5+4+3+4）。为了测试用户要求的 23 表输入，脚本明确增加了合成的 `fruit_import_staging_temp`（70 列、无主键）。这只是测试假设，不是对真实库结构的判断。21 张表含审计字段；词典和维度取值快照不含审计字段。所有声明外键为空；多态业务编号、维度/指标编码、规则引用保留为数据中的候选关联。

生成后会检查表数、列顺序、主键唯一性、无声明外键、`business_type` 与 `source_type` 覆盖、业务对象目标、参数引用、维度允许值、部分编码引用、公式可解析比例，以及苹果和香蕉价格范围。入参和出参使用不重叠的编号区间；每条引用规则的 `param_id` 都唯一指向其中一条参数记录，且 `business_id`、`business_type` 与该参数一致。维度规则的 `field_rule` JSON 列表仅包含所属维度中实际存在的成员编码。`synthetic_manifest.yaml` 记录行数与合成来源。验证通过只说明**输入形状和预设关联模式**符合测试要求，不能证明自动抽取的本体在真实业务上正确。

小样本的路线 2 试验可用 `config/runtime.fruit.example.yaml`：先把 `.env.example` 复制为 `.env`，填写 `ONTOLOGY_LLM_MODEL=deepseek-v4-flash`、火山引擎 API 地址及密钥，再执行：

```bash
uv run ontology-r2 build --config config/runtime.fruit.example.yaml --output runs/fruit-trial-001
```

此配置关闭企业 MCP、外部本体、向量通道和可选关联 ReAct Agent，使用本地 BM25、程序核验的物理关联规则及有界组包。`thinking.type=disabled` 用于该端点的关闭思考参数；模型的 `tool_choice` 为 `auto`，服务端标为 `length` 等未完整结束的回答会被拒绝。这个样例用于先验证确定性的扫描、索引、组包与组级抽取，不是 IB0—IB4 对照实验。

全量 CSV 只参加程序扫描、统计和索引；模型调用受逐表单元、`instance_bundles.max_llm_bundles` 和 `llm.max_calls` 限制，不会按每行调用。全量试验时把配置中的 `dataset` 改为 `../local_data/fruit_data`，仍需先根据资源预算调整卡片和关系上限。每次使用新 `--output` 目录；结果中的 `manifest.partial_reasons`、`semantic_card_coverage.yaml`、`instance_bundle_coverage.yaml` 和 `group_steps.yaml` 分别说明未处理原因、入卡范围、未选种子及组级判断。

记录级关系结论必须同时引述同一条已核验正例两端的非关联键原文；仅有编码相等、字段注释或物理表连接不能生成业务关系。同根定义表或锚点/定义表共享定义键时先视为概念对齐线索；长字段保留截断标记，截断的语义字段不能支持 `exact` 概念对齐，新概念至少要有一条完整的 `exact` 定义来源。这个防护仍不能证明自动结论在真实业务上正确。生成器验证和本机试验统计见[水果经营实验记录](../../docs/route2/EXPERIMENT_FRUIT.md)。
